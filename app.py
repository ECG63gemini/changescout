import asyncio
import hashlib
import logging
import os
import secrets
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.security import HTTPBasic
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from dotenv import load_dotenv
from PIL import Image, ImageChops, ImageStat
from playwright.async_api import (
    Browser,
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Route,
    TimeoutError as PlaywrightTimeoutError,
    WebSocketRoute,
    async_playwright,
)

import pages
from emailing import (
    EmailContent,
    EmailDeliveryError,
    EmailSettings,
    email_settings_from_environment,
    render_change_alert,
    render_inquiry_notification,
    send_email,
    validate_email_address,
)
from monitoring import (
    BUSINESS_CATEGORIES,
    ChangeAssessment,
    StructuredChange,
    URLValidationError,
    assess_change,
    deserialize_structured_changes,
    extract_visible_text,
    extract_structured_changes,
    serialize_structured_changes,
    validate_public_url,
)
from scheduling import (
    target_is_due,
    validate_frequency,
)
from ui import (
    STATIC_DIR,
    AttentionItem,
    FeedItem,
    MonitorView,
    disclosure,
    feed_item,
    friendly_error,
    h,
    monitor_attention,
    monitor_view,
    technical_list,
    utc_now,
)

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")


def _runtime_data_paths(base: Path) -> tuple[Path, Path, Path]:
    configured = os.getenv("DATA_DIR", "").strip()
    data = Path(configured).expanduser() if configured else base / "data"
    screenshots = data / "screenshots"
    data.mkdir(parents=True, exist_ok=True)
    screenshots.mkdir(parents=True, exist_ok=True)
    return data, screenshots, data / "changescout.db"


DATA, SHOTS, DB = _runtime_data_paths(BASE)
MAX_SCREENSHOT_HEIGHT = 12_000
MAX_SCREENSHOT_WIDTH = 1_440
NAVIGATION_TIMEOUT_MS = 60_000
NETWORK_IDLE_TIMEOUT_MS = 5_000
URL_VALIDATION_TIMEOUT_SECONDS = 5
CAPTURE_ATTEMPTS = 2
SAFE_BROWSER_SCHEMES = {"about", "blob", "data"}

logger = logging.getLogger("changescout")
_active_checks: set[int] = set()


def _scheduler_poll_seconds() -> int:
    value = os.getenv("SCHEDULER_POLL_SECONDS", "60").strip()
    try:
        return max(5, int(value))
    except ValueError:
        logger.warning("Invalid SCHEDULER_POLL_SECONDS; using 60 seconds.")
        return 60


SCHEDULER_POLL_SECONDS = _scheduler_poll_seconds()


@asynccontextmanager
async def lifespan(_: FastAPI):
    scheduler_task = asyncio.create_task(
        scheduler_loop(),
        name="changescout-scheduler",
    )
    try:
        yield
    finally:
        scheduler_task.cancel()
        with suppress(asyncio.CancelledError):
            await scheduler_task


app = FastAPI(
    title="ChangeScout",
    lifespan=lifespan,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
operator_basic = HTTPBasic(auto_error=False)


@app.middleware("http")
async def protect_operator(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    path = request.url.path
    if path == "/app" or path.startswith("/app/"):
        headers = {"Cache-Control": "no-store"}
        username = os.getenv("ADMIN_USERNAME", "")
        password = os.getenv("ADMIN_PASSWORD", "")
        if not username.strip() or not password.strip():
            return HTMLResponse(
                "Operator access is unavailable: configure ADMIN_USERNAME and ADMIN_PASSWORD.",
                status_code=503,
                headers=headers,
            )
        try:
            credentials = await operator_basic(request)
        except HTTPException:
            credentials = None
        # Compare fixed-length digests without revealing credential lengths.
        username_matches = secrets.compare_digest(
            hashlib.sha256(
                (credentials.username if credentials else "").encode("utf-8")
            ).digest(),
            hashlib.sha256(username.encode("utf-8")).digest(),
        )
        password_matches = secrets.compare_digest(
            hashlib.sha256(
                (credentials.password if credentials else "").encode("utf-8")
            ).digest(),
            hashlib.sha256(password.encode("utf-8")).digest(),
        )
        if not (credentials and username_matches and password_matches):
            headers["WWW-Authenticate"] = 'Basic realm="ChangeScout operator"'
            return HTMLResponse(
                "Operator authentication required.",
                status_code=401,
                headers=headers,
            )
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if (
                request.headers.get("sec-fetch-site") == "cross-site"
                or (
                    origin is not None
                    and origin.rstrip("/") != str(request.base_url).rstrip("/")
                )
            ):
                return HTMLResponse(
                    "Cross-site operator actions are not allowed.",
                    status_code=403,
                    headers=headers,
                )
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response
    return await call_next(request)


@app.exception_handler(StarletteHTTPException)
async def http_error_page(
    _request: Request,
    exc: StarletteHTTPException,
) -> HTMLResponse:
    if exc.status_code == 404:
        title = "Page not found"
        default = "The page you're looking for doesn't exist or may have been removed."
    elif exc.status_code >= 500:
        title = "Something went wrong"
        default = "Please try again in a moment."
    else:
        title = "We couldn't complete that request"
        default = "Please go back and try again."
    detail = str(exc.detail or "")
    generic = {"Not Found", "Bad Request", "Method Not Allowed", "Internal Server Error"}
    message = default if not detail or detail in generic else detail
    return HTMLResponse(
        pages.error_page(title=title, message=message),
        status_code=exc.status_code,
        headers=getattr(exc, "headers", None),
    )


@app.exception_handler(RequestValidationError)
async def validation_error_page(
    request: Request,
    _exc: RequestValidationError,
) -> HTMLResponse:
    # Invalid path parameters (e.g. /app/customer/abc) are pages that don't exist.
    if request.method in {"GET", "HEAD"}:
        return HTMLResponse(
            pages.error_page(
                title="Page not found",
                message="The page you're looking for doesn't exist or may have been removed.",
            ),
            status_code=404,
        )
    return HTMLResponse(
        pages.error_page(
            title="Some information is missing",
            message=(
                "Some required information was missing or invalid. Go back, "
                "complete every required field, and try again."
            ),
        ),
        status_code=422,
    )


class CaptureError(RuntimeError):
    pass


@dataclass(frozen=True)
class CheckResult:
    status: str
    capture_id: int | None = None
    error: str | None = None
    alert_status: str | None = None

    @property
    def completed(self) -> bool:
        return self.status == "completed"


EmailSender = Callable[[EmailSettings, str, EmailContent], None]


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS customers(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            email TEXT NOT NULL,
            created_at TEXT NOT NULL,
            active INTEGER NOT NULL DEFAULT 1
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS inquiries(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            business_name TEXT NOT NULL,
            email TEXT NOT NULL,
            website TEXT,
            message TEXT,
            created_at TEXT NOT NULL
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS targets(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            customer_id INTEGER,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            alert_email TEXT,
            check_frequency TEXT NOT NULL DEFAULT 'daily',
            last_check_at TEXT,
            last_check_error TEXT,
            created_at TEXT NOT NULL,
            FOREIGN KEY(customer_id) REFERENCES customers(id)
        )
        """)
        conn.execute("""
        CREATE TABLE IF NOT EXISTS captures(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            target_id INTEGER NOT NULL,
            created_at TEXT NOT NULL,
            screenshot_path TEXT NOT NULL,
            text_content TEXT NOT NULL,
            text_hash TEXT NOT NULL,
            visual_score REAL,
            text_score REAL,
            categories TEXT,
            structured_changes TEXT NOT NULL DEFAULT '[]',
            alert_status TEXT NOT NULL DEFAULT 'not_applicable',
            alert_recipient TEXT,
            alert_sent_at TEXT,
            alert_error TEXT,
            summary TEXT,
            FOREIGN KEY(target_id) REFERENCES targets(id)
        )
        """)
        target_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(targets)").fetchall()
        }
        target_migrations = {
            "customer_id": "INTEGER REFERENCES customers(id)",
            "enabled": "INTEGER NOT NULL DEFAULT 1",
            "alert_email": "TEXT",
            "check_frequency": "TEXT NOT NULL DEFAULT 'daily'",
            "last_check_at": "TEXT",
            "last_check_error": "TEXT",
        }
        for column, definition in target_migrations.items():
            if column not in target_columns:
                conn.execute(
                    f"ALTER TABLE targets ADD COLUMN {column} {definition}"
                )

        capture_columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(captures)").fetchall()
        }
        if "categories" not in capture_columns:
            conn.execute("ALTER TABLE captures ADD COLUMN categories TEXT")
        if "structured_changes" not in capture_columns:
            conn.execute(
                "ALTER TABLE captures ADD COLUMN structured_changes "
                "TEXT NOT NULL DEFAULT '[]'"
            )
        capture_migrations = {
            "alert_status": "TEXT NOT NULL DEFAULT 'not_applicable'",
            "alert_recipient": "TEXT",
            "alert_sent_at": "TEXT",
            "alert_error": "TEXT",
        }
        for column, definition in capture_migrations.items():
            if column not in capture_columns:
                conn.execute(
                    f"ALTER TABLE captures ADD COLUMN {column} {definition}"
                )
        conn.execute(
            """
            UPDATE captures
            SET alert_status='indeterminate',
                alert_error='Delivery was interrupted; whether email was sent is unknown.'
            WHERE alert_status='sending'
            """
        )

        conn.execute(
            """
            UPDATE targets
            SET last_check_at=(
                SELECT MAX(created_at)
                FROM captures
                WHERE captures.target_id=targets.id
            )
            WHERE last_check_at IS NULL
              AND EXISTS(
                SELECT 1 FROM captures WHERE captures.target_id=targets.id
              )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS captures_target_id_id "
            "ON captures(target_id, id DESC)"
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS targets_customer_id "
            "ON targets(customer_id)"
        )

init_db()

def visual_change_score(old_path: Path, new_path: Path) -> float:
    with Image.open(old_path) as old_image, Image.open(new_path) as new_image:
        a = old_image.convert("RGB")
        b = new_image.convert("RGB")
        w = min(a.width, b.width, MAX_SCREENSHOT_WIDTH)
        h = min(a.height, b.height, MAX_SCREENSHOT_HEIGHT)
        comparison_size = (360, max(1, int(h * 360 / w)))
        a = a.crop((0, 0, w, h)).resize(
            comparison_size,
            Image.Resampling.LANCZOS,
        )
        b = b.crop((0, 0, w, h)).resize(
            comparison_size,
            Image.Resampling.LANCZOS,
        )
        diff = ImageChops.difference(a, b)
        stat = ImageStat.Stat(diff)
        mean = sum(stat.mean) / (3 * 255)
        height_difference = abs(old_image.height - new_image.height) / max(
            old_image.height,
            new_image.height,
        )
        width_difference = abs(old_image.width - new_image.width) / max(
            old_image.width,
            new_image.width,
        )
        dimension_difference = max(height_difference, width_difference)
        return round(((mean * 0.9) + (dimension_difference * 0.1)) * 100, 1)


def _request_origin(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    return parsed.scheme.lower(), parsed.netloc.lower()


async def _validate_public_url_async(url: str) -> str:
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(validate_public_url, url),
            timeout=URL_VALIDATION_TIMEOUT_SECONDS,
        )
    except TimeoutError as exc:
        raise URLValidationError(
            "Hostname validation timed out. Please try again."
        ) from exc


async def _capture_once(url: str, path: Path) -> tuple[Path, str]:
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None
    temporary_path = path.with_name(f"{path.stem}.part.png")
    validated_origins: dict[tuple[str, str], str | None] = {}
    blocked_main_navigation: list[str] = []

    async with async_playwright() as p:
        try:
            browser = await p.chromium.launch(headless=True)
            context = await browser.new_context(
                viewport={"width": MAX_SCREENSHOT_WIDTH, "height": 1_000},
                accept_downloads=False,
                service_workers="block",
            )
            page = await context.new_page()
            page.set_default_timeout(20_000)
            page.set_default_navigation_timeout(NAVIGATION_TIMEOUT_MS)

            async def guard_request(route: Route) -> None:
                request = route.request
                parsed = urlparse(request.url)
                scheme = parsed.scheme.lower()
                if scheme in SAFE_BROWSER_SCHEMES:
                    await route.continue_()
                    return
                if scheme not in {"http", "https"}:
                    await route.abort("blockedbyclient")
                    return

                origin = _request_origin(request.url)
                if origin not in validated_origins:
                    try:
                        await _validate_public_url_async(request.url)
                    except URLValidationError as exc:
                        validated_origins[origin] = str(exc)
                    else:
                        validated_origins[origin] = None

                validation_error = validated_origins[origin]
                if validation_error is None:
                    await route.continue_()
                    return

                is_main_navigation = False
                if request.is_navigation_request():
                    with suppress(PlaywrightError):
                        is_main_navigation = request.frame == page.main_frame
                if is_main_navigation:
                    blocked_main_navigation.append(validation_error)
                logger.warning(
                    "Blocked unsafe browser request to host %s: %s",
                    parsed.hostname or "<unknown>",
                    validation_error,
                )
                await route.abort("blockedbyclient")

            def block_websocket(websocket: WebSocketRoute) -> None:
                websocket.close(
                    code=1008,
                    reason="WebSockets are disabled during monitoring captures.",
                )

            await context.route("**/*", guard_request)
            await context.route_web_socket("**/*", block_websocket)
            navigation_timed_out = False
            response = None
            try:
                response = await page.goto(url, wait_until="domcontentloaded")
            except PlaywrightTimeoutError:
                navigation_timed_out = True
            except PlaywrightError as exc:
                if blocked_main_navigation:
                    raise URLValidationError(blocked_main_navigation[-1]) from exc
                raise

            if blocked_main_navigation:
                raise URLValidationError(blocked_main_navigation[-1])
            if response is not None and response.status >= 400:
                raise CaptureError(
                    f"The website returned HTTP {response.status}; no capture was saved."
                )

            try:
                await page.wait_for_load_state(
                    "networkidle",
                    timeout=NETWORK_IDLE_TIMEOUT_MS,
                )
            except PlaywrightTimeoutError:
                logger.info("Page did not reach networkidle; using the loaded DOM.")
            await page.wait_for_timeout(750)

            final_url = page.url
            if final_url and final_url != "about:blank":
                await _validate_public_url_async(final_url)
            html = await page.content()
            text = extract_visible_text(html)
            if navigation_timed_out and not text:
                raise CaptureError(
                    "The page timed out before any usable content was available."
                )

            dimensions = await page.evaluate(
                """() => {
                    const body = document.body;
                    const root = document.documentElement;
                    return {
                        width: Math.max(
                            root.clientWidth,
                            root.scrollWidth,
                            body ? body.scrollWidth : 0
                        ),
                        height: Math.max(
                            root.clientHeight,
                            root.scrollHeight,
                            body ? body.scrollHeight : 0
                        )
                    };
                }"""
            )
            screenshot_width = max(
                1,
                min(int(dimensions["width"]), MAX_SCREENSHOT_WIDTH),
            )
            screenshot_height = max(
                1,
                min(int(dimensions["height"]), MAX_SCREENSHOT_HEIGHT),
            )
            await page.screenshot(
                path=str(temporary_path),
                animations="disabled",
                caret="hide",
                clip={
                    "x": 0,
                    "y": 0,
                    "width": screenshot_width,
                    "height": screenshot_height,
                },
            )
            temporary_path.replace(path)
            return path, text
        finally:
            if page is not None:
                with suppress(PlaywrightError):
                    await page.close()
            if context is not None:
                with suppress(PlaywrightError):
                    await context.close()
            if browser is not None:
                with suppress(PlaywrightError):
                    await browser.close()
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning("Could not remove temporary screenshot %s: %s", temporary_path, exc)


async def capture_page(target_id: int, url: str) -> tuple[Path, str]:
    validated_url = await _validate_public_url_async(url)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = SHOTS / f"{target_id}_{stamp}.png"
    last_error: Exception | None = None
    for attempt in range(1, CAPTURE_ATTEMPTS + 1):
        try:
            return await _capture_once(validated_url, path)
        except URLValidationError:
            path.unlink(missing_ok=True)
            raise
        except (CaptureError, OSError, PlaywrightError) as exc:
            path.unlink(missing_ok=True)
            last_error = exc
            logger.warning(
                "Capture attempt %s/%s failed: %s",
                attempt,
                CAPTURE_ATTEMPTS,
                exc,
            )
    if isinstance(last_error, CaptureError):
        raise last_error
    raise CaptureError(
        "The browser could not capture this page after two attempts. "
        "The site may be unavailable, too slow, or blocking automated browsers."
    ) from last_error


async def ai_summary(
    old_text: str,
    new_text: str,
    categories: tuple[str, ...],
) -> str | None:
    if not os.getenv("OPENAI_API_KEY"):
        return None
    try:
        from browser_use import ChatOpenAI
        from browser_use.llm.messages import SystemMessage, UserMessage

        llm = ChatOpenAI(
            model=os.getenv("OPENAI_MODEL", "gpt-5-mini"),
            api_key=os.environ["OPENAI_API_KEY"],
            max_retries=2,
            timeout=30,
        )
        result = await llm.ainvoke(
            [
                SystemMessage(
                    content=(
                        "Summarize commercially meaningful e-commerce webpage "
                        "changes using only the supplied text. Do not browse, call "
                        "tools, or infer changes not present in the text."
                    )
                ),
                UserMessage(
                    content=(
                        f"Detected categories: {', '.join(categories)}\n\n"
                        f"BEFORE:\n{old_text[:8_000]}\n\n"
                        f"AFTER:\n{new_text[:8_000]}"
                    )
                ),
            ]
        )
        return result.completion.strip()
    except Exception as exc:
        logger.exception("Optional AI summary failed")
        return f"Optional AI summary unavailable ({type(exc).__name__})."


def _application_report_url(target_id: int) -> str | None:
    base_url = os.getenv("APP_BASE_URL", "").strip().rstrip("/")
    if not base_url:
        return None
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        logger.warning(
            "APP_BASE_URL is invalid; alert emails will omit the report link."
        )
        return None
    return f"{base_url}/app/target/{target_id}"


def _clean_error(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    return (message or type(exc).__name__)[:400]


def _update_alert_status(
    capture_id: int,
    status: str,
    *,
    error: str | None = None,
    sent_at: str | None = None,
) -> None:
    with db() as conn:
        conn.execute(
            """
            UPDATE captures
            SET alert_status=?, alert_error=?, alert_sent_at=?
            WHERE id=?
            """,
            (status, error, sent_at, capture_id),
        )


async def deliver_capture_alert(
    capture_id: int,
    *,
    email_sender: EmailSender | None = None,
) -> str:
    with db() as conn:
        capture = conn.execute(
            """
            SELECT c.*,
                   t.name AS target_name,
                   t.url AS target_url,
                   t.customer_id,
                   customer.active AS customer_active
            FROM captures c
            JOIN targets t ON t.id=c.target_id
            LEFT JOIN customers customer ON customer.id=t.customer_id
            WHERE c.id=?
            """,
            (capture_id,),
        ).fetchone()
    if capture is None:
        raise ValueError(f"Capture {capture_id} does not exist.")
    if capture["alert_status"] != "pending":
        return str(capture["alert_status"])

    if (
        capture["customer_id"] is not None
        and not bool(capture["customer_active"])
    ):
        error = "The customer is disabled; the alert was not sent."
        _update_alert_status(capture_id, "customer_disabled", error=error)
        return "customer_disabled"

    try:
        changes = deserialize_structured_changes(capture["structured_changes"])
    except ValueError as exc:
        error = _clean_error(exc)
        _update_alert_status(capture_id, "failed", error=error)
        logger.error(
            "Alert for capture %s failed: invalid structured change data.",
            capture_id,
        )
        return "failed"
    if not changes:
        _update_alert_status(capture_id, "not_applicable")
        return "not_applicable"

    recipient = (capture["alert_recipient"] or "").strip()
    if not recipient:
        error = "No alert recipient is configured for this target."
        _update_alert_status(capture_id, "no_recipient", error=error)
        return "no_recipient"
    try:
        recipient = validate_email_address(recipient)
    except ValueError as exc:
        error = _clean_error(exc)
        _update_alert_status(capture_id, "failed", error=error)
        return "failed"

    settings = email_settings_from_environment()
    if not settings.configured:
        error = settings.configuration_error or "Email is not configured."
        _update_alert_status(capture_id, "not_configured", error=error)
        logger.info(
            "Alert for capture %s was not sent because email is not configured.",
            capture_id,
        )
        return "not_configured"

    with db() as conn:
        claimed = conn.execute(
            """
            UPDATE captures
            SET alert_status='sending', alert_error=NULL
            WHERE id=? AND alert_status='pending'
            """,
            (capture_id,),
        )
        if claimed.rowcount != 1:
            current = conn.execute(
                "SELECT alert_status FROM captures WHERE id=?",
                (capture_id,),
            ).fetchone()
            return str(current["alert_status"]) if current else "missing"

    content = render_change_alert(
        competitor_name=capture["target_name"],
        page_url=capture["target_url"],
        detected_at=capture["created_at"],
        changes=changes,
        report_url=_application_report_url(capture["target_id"]),
    )
    sender = email_sender or send_email
    delivery_task = asyncio.create_task(
        asyncio.to_thread(
            sender,
            settings,
            recipient,
            content,
        )
    )
    try:
        await asyncio.shield(delivery_task)
    except asyncio.CancelledError:
        try:
            await delivery_task
        except EmailDeliveryError as exc:
            error = _clean_error(exc)
            _update_alert_status(capture_id, "failed", error=error)
        except Exception:
            error = "Unexpected email transport failure during shutdown."
            _update_alert_status(capture_id, "failed", error=error)
            logger.exception(
                "Email alert failed during shutdown for capture %s",
                capture_id,
            )
        else:
            sent_at = datetime.now(timezone.utc).isoformat()
            _update_alert_status(capture_id, "sent", sent_at=sent_at)
            logger.info("Email alert sent for capture %s during shutdown.", capture_id)
        raise
    except EmailDeliveryError as exc:
        error = _clean_error(exc)
        _update_alert_status(capture_id, "failed", error=error)
        logger.error(
            "Email alert delivery failed for capture %s: %s",
            capture_id,
            error,
        )
        return "failed"
    except Exception as exc:
        error = f"Unexpected email transport failure ({type(exc).__name__})."
        _update_alert_status(capture_id, "failed", error=error)
        logger.exception(
            "Unexpected email alert failure for capture %s",
            capture_id,
        )
        return "failed"

    sent_at = datetime.now(timezone.utc).isoformat()
    _update_alert_status(capture_id, "sent", sent_at=sent_at)
    logger.info("Email alert sent for capture %s.", capture_id)
    return "sent"


def _score_display(value: object) -> str:
    if value is None:
        return "Unavailable"
    return f"{float(value):.1f}%"


def _change_summary(
    assessment: ChangeAssessment,
    visual_score: float | None,
    structured_changes: tuple[StructuredChange, ...],
    *,
    visual_error: bool,
) -> str:
    if not assessment.meaningful:
        if visual_error:
            return (
                "No meaningful text change detected. "
                "Visual comparison was unavailable for this check."
            )
        return "No meaningful change detected."

    categories = ", ".join(assessment.categories)
    visual = _score_display(visual_score)
    if structured_changes:
        count = len(structured_changes)
        noun = "change" if count == 1 else "changes"
        summary = (
            f"{count} meaningful business {noun} detected: {categories}. "
            f"Text difference {assessment.text_score:.1f}%; "
            f"visual difference {visual}."
        )
    else:
        summary = (
            f"{categories} change detected: {assessment.text_score:.1f}% text "
            f"difference and {visual} visual difference."
        )
    if visual_error:
        summary += " Visual comparison was unavailable for this check."
    return summary


def _capture_changes(capture: sqlite3.Row) -> tuple[StructuredChange, ...]:
    try:
        return deserialize_structured_changes(capture["structured_changes"])
    except ValueError as exc:
        logger.warning(
            "Capture %s has invalid structured change data: %s",
            capture["id"],
            exc,
        )
        return ()


# Only captures with extracted old/new values ever attempt an email alert.
CHANGES_CAPTURE = "TRIM(COALESCE(capture.structured_changes, '')) NOT IN ('', '[]')"
MEANINGFUL_CAPTURE = (
    f"(TRIM(COALESCE(capture.categories, '')) != '' OR {CHANGES_CAPTURE})"
)

MONITOR_QUERY = f"""
SELECT target.id,
       target.customer_id,
       target.name,
       target.url,
       target.enabled,
       target.alert_email,
       target.check_frequency,
       target.last_check_at,
       target.last_check_error,
       customer.name AS customer_name,
       customer.email AS customer_email,
       customer.active AS customer_active,
       latest.id AS latest_id,
       latest.created_at AS latest_at,
       latest.categories AS latest_categories,
       latest.structured_changes AS latest_changes,
       (
           SELECT MIN(earliest.id)
           FROM captures earliest
           WHERE earliest.target_id=target.id
       ) AS first_capture_id,
       report.created_at AS report_at,
       alerted.created_at AS alerted_at,
       alerted.alert_status AS alerted_status
FROM targets target
LEFT JOIN customers customer ON customer.id=target.customer_id
LEFT JOIN captures latest ON latest.id=(
    SELECT capture.id
    FROM captures capture
    WHERE capture.target_id=target.id
    ORDER BY capture.id DESC
    LIMIT 1
)
LEFT JOIN captures report ON report.id=(
    SELECT capture.id
    FROM captures capture
    WHERE capture.target_id=target.id
      AND {MEANINGFUL_CAPTURE}
    ORDER BY capture.id DESC
    LIMIT 1
)
LEFT JOIN captures alerted ON alerted.id=(
    SELECT capture.id
    FROM captures capture
    WHERE capture.target_id=target.id
      AND {CHANGES_CAPTURE}
    ORDER BY capture.id DESC
    LIMIT 1
)
"""

FEED_QUERY = f"""
SELECT capture.id,
       capture.target_id,
       capture.created_at,
       capture.categories,
       capture.structured_changes,
       capture.alert_status,
       capture.alert_recipient,
       capture.alert_sent_at,
       capture.alert_error,
       capture.summary,
       capture.visual_score,
       capture.text_score,
       target.name AS target_name,
       target.url AS target_url,
       target.customer_id,
       customer.name AS customer_name
FROM captures capture
JOIN targets target ON target.id=capture.target_id
LEFT JOIN customers customer ON customer.id=target.customer_id
WHERE {MEANINGFUL_CAPTURE}
"""

OVERVIEW_FEED_LIMIT = 8
CUSTOMER_FEED_LIMIT = 5
HISTORY_LIMIT = 20


def _load_monitors(
    conn: sqlite3.Connection,
    *,
    now: datetime,
    customer_id: int | None = None,
    target_id: int | None = None,
) -> list[MonitorView]:
    query = MONITOR_QUERY
    params: tuple[int, ...] = ()
    if customer_id is not None:
        query += " WHERE target.customer_id=?"
        params = (customer_id,)
    elif target_id is not None:
        query += " WHERE target.id=?"
        params = (target_id,)
    rows = conn.execute(f"{query} ORDER BY target.id DESC", params).fetchall()
    return [monitor_view(row, now=now) for row in rows]


def _load_feed(
    conn: sqlite3.Connection,
    *,
    limit: int,
    customer_id: int | None = None,
) -> list[FeedItem]:
    query = FEED_QUERY
    params: tuple[int, ...] = ()
    if customer_id is not None:
        query += " AND target.customer_id=?"
        params = (customer_id,)
    rows = conn.execute(
        f"{query} ORDER BY capture.id DESC LIMIT ?",
        (*params, limit),
    ).fetchall()
    return [feed_item(row) for row in rows]


def _email_settings() -> tuple[bool, str | None]:
    settings = email_settings_from_environment()
    return settings.configured, settings.configuration_error


def _email_attention(configuration_error: str | None) -> AttentionItem:
    return AttentionItem(
        title="Email alerts are unavailable",
        body=(
            "Changes are still detected and saved to reports, but alert emails "
            "can't be sent until email delivery is set up."
            + disclosure(
                "How to turn on email alerts",
                technical_list(
                    [
                        (
                            "Configuration",
                            h(configuration_error or "SMTP settings are incomplete."),
                        ),
                        (
                            "Fix",
                            "Set the SMTP environment variables on the server, "
                            "then restart ChangeScout.",
                        ),
                    ]
                ),
                extra_class="disclosure--quiet",
            )
        ),
        action="",
        tone="warning",
        priority=-1,
    )


def _attention_items(
    monitors: list[MonitorView],
    *,
    now: datetime,
    include_customer: bool,
) -> list[AttentionItem]:
    items: list[AttentionItem] = []
    email_ready, email_error = _email_settings()
    if not email_ready and any(monitor.active for monitor in monitors):
        items.append(_email_attention(email_error))
    for monitor in monitors:
        items.extend(
            monitor_attention(monitor, now=now, include_customer=include_customer)
        )
    # Paused customers are an intentional choice, not a problem, so they are
    # shown on the Customers pages rather than listed here.
    return sorted(items, key=lambda item: item.priority)


@app.get("/landing", response_class=HTMLResponse)
@app.get("/", response_class=HTMLResponse)
def landing(submitted: str | None = None):
    return HTMLResponse(pages.landing_page(submitted=submitted == "1"))


def _inquiry_website_error(value: str) -> str | None:
    if not value:
        return None
    if len(value) > 2048:
        return "Website must be 2,048 characters or fewer."
    try:
        parsed = urlparse(value)
        hostname = parsed.hostname
    except ValueError:
        return "Enter a valid website address, including https://."
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.netloc
        or not hostname
        or any(character.isspace() for character in value)
    ):
        return "Enter a valid website address, including https://."
    return None


def _send_inquiry_notification(
    *,
    inquiry_id: int,
    name: str,
    business_name: str,
    email: str,
    website: str | None,
    message: str | None,
    submitted_at: str,
) -> None:
    recipient = os.getenv("INQUIRY_NOTIFICATION_EMAIL", "").strip()
    if not recipient:
        return
    settings = email_settings_from_environment()
    if not settings.configured:
        logger.info(
            "Inquiry notification for inquiry %s was not sent because "
            "email is not configured.",
            inquiry_id,
        )
        return
    content = render_inquiry_notification(
        name=name,
        business_name=business_name,
        email=email,
        website=website,
        message=message,
        submitted_at=submitted_at,
    )
    try:
        send_email(settings, recipient, content)
    except (EmailDeliveryError, ValueError):
        logger.warning(
            "Inquiry notification delivery failed for inquiry %s.",
            inquiry_id,
        )


@app.post("/landing/contact")
def create_inquiry(
    name: str = Form(""),
    business_name: str = Form(""),
    email: str = Form(""),
    website: str = Form(""),
    message: str = Form(""),
):
    values = {
        "name": name,
        "business_name": business_name,
        "email": email,
        "website": website,
        "message": message,
    }
    name = name.strip()
    business_name = business_name.strip()
    website = website.strip()
    message = message.strip()
    errors: dict[str, str] = {}
    if not name:
        errors["name"] = "Enter your name."
    elif len(name) > 120:
        errors["name"] = "Name must be 120 characters or fewer."
    if not business_name:
        errors["business_name"] = "Enter your business name."
    elif len(business_name) > 160:
        errors["business_name"] = "Business name must be 160 characters or fewer."
    try:
        email = validate_email_address(email)
    except ValueError:
        errors["email"] = "Enter a valid email address."
    website_error = _inquiry_website_error(website)
    if website_error:
        errors["website"] = website_error
    if len(message) > 2000:
        errors["message"] = "Message must be 2,000 characters or fewer."
    if errors:
        return HTMLResponse(
            pages.landing_page(values=values, errors=errors),
            status_code=400,
        )
    submitted_at = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO inquiries(
                name, business_name, email, website, message, created_at
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                name,
                business_name,
                email,
                website or None,
                message or None,
                submitted_at,
            ),
        )
        inquiry_id = int(cursor.lastrowid)
    _send_inquiry_notification(
        inquiry_id=inquiry_id,
        name=name,
        business_name=business_name,
        email=email,
        website=website or None,
        message=message or None,
        submitted_at=submitted_at,
    )
    return RedirectResponse("/?submitted=1#contact", status_code=303)


@app.get("/app", response_class=HTMLResponse)
def home():
    now = utc_now()
    with db() as conn:
        monitors = _load_monitors(conn, now=now)
        feed = _load_feed(conn, limit=OVERVIEW_FEED_LIMIT)
        customer_counts = conn.execute(
            "SELECT COUNT(*) AS total, COALESCE(SUM(active), 0) AS active "
            "FROM customers"
        ).fetchone()
    return HTMLResponse(
        pages.overview_page(
            now=now,
            customers_total=int(customer_counts["total"]),
            customers_active=int(customer_counts["active"]),
            monitors=monitors,
            feed=feed,
            attention=_attention_items(monitors, now=now, include_customer=True),
        )
    )


def _render_monitors(
    *,
    notice: str | None = None,
    form_values: dict[str, str] | None = None,
    form_errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    now = utc_now()
    with db() as conn:
        monitors = _load_monitors(conn, now=now)
    return HTMLResponse(
        pages.monitors_page(
            now=now,
            monitors=monitors,
            notice=notice,
            form_values=form_values,
            form_errors=form_errors,
        ),
        status_code=status_code,
    )


@app.get("/app/monitors", response_class=HTMLResponse)
def monitor_list(notice: str | None = None):
    return _render_monitors(notice=notice)


@app.get("/app/customers", response_class=HTMLResponse)
def customer_list():
    with db() as conn:
        customers = conn.execute(
            """
            SELECT customer.*,
                   COUNT(target.id) AS monitor_count,
                   MAX(target.last_check_at) AS most_recent_activity
            FROM customers customer
            LEFT JOIN targets target ON target.customer_id=customer.id
            GROUP BY customer.id
            ORDER BY customer.id DESC
            """
        ).fetchall()
    return HTMLResponse(pages.customers_page(now=utc_now(), customers=customers))


@app.get("/app/customers/new", response_class=HTMLResponse)
def new_customer():
    return HTMLResponse(pages.customer_form_page())


@app.post("/app/customers")
def create_customer(
    name: str = Form(...),
    email: str = Form(...),
):
    values = {"name": name, "email": email}
    errors: dict[str, str] = {}
    name = name.strip()
    if not name:
        errors["name"] = "Enter the customer's name."
    try:
        email = validate_email_address(email)
    except ValueError as exc:
        errors["email"] = str(exc)
    if errors:
        return HTMLResponse(
            pages.customer_form_page(values=values, errors=errors),
            status_code=400,
        )
    with db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO customers(name, email, created_at, active)
            VALUES(?,?,?,1)
            """,
            (name, email, datetime.now(timezone.utc).isoformat()),
        )
        customer_id = int(cursor.lastrowid)
    return RedirectResponse(f"/app/customer/{customer_id}", status_code=303)


def _get_customer(customer_id: int) -> sqlite3.Row:
    with db() as conn:
        customer = conn.execute(
            "SELECT * FROM customers WHERE id=?",
            (customer_id,),
        ).fetchone()
    if customer is None:
        raise HTTPException(404, "This customer doesn't exist or was removed.")
    return customer


@app.get("/app/customer/{customer_id}", response_class=HTMLResponse)
def customer_detail(
    customer_id: int,
    notice: str | None = None,
):
    now = utc_now()
    customer = _get_customer(customer_id)
    with db() as conn:
        monitors = _load_monitors(conn, now=now, customer_id=customer_id)
        feed = _load_feed(
            conn,
            limit=CUSTOMER_FEED_LIMIT,
            customer_id=customer_id,
        )
    return HTMLResponse(
        pages.customer_page(
            now=now,
            customer=customer,
            monitors=monitors,
            feed=feed,
            attention=_attention_items(monitors, now=now, include_customer=False),
            notice=notice,
        )
    )


@app.get("/app/customer/{customer_id}/targets/new", response_class=HTMLResponse)
def new_customer_target(customer_id: int):
    return HTMLResponse(
        pages.competitor_form_page(customer=_get_customer(customer_id))
    )


@app.post("/app/customer/{customer_id}/status")
def update_customer_status(
    customer_id: int,
    active: str = Form(...),
):
    if active not in {"0", "1"}:
        raise HTTPException(400, "Customer status is invalid.")
    active_value = int(active)
    with db() as conn:
        updated = conn.execute(
            "UPDATE customers SET active=? WHERE id=?",
            (active_value, customer_id),
        )
    if updated.rowcount != 1:
        raise HTTPException(404, "This customer doesn't exist or was removed.")
    message = (
        "Customer re-enabled. Scheduled scans and email alerts will resume."
        if active_value
        else "Customer disabled. Scheduled scans and email alerts are paused."
    )
    notice = quote(message, safe="")
    return RedirectResponse(
        f"/app/customer/{customer_id}?notice={notice}",
        status_code=303,
    )


def _validated_optional_email(value: str) -> str | None:
    candidate = value.strip()
    return validate_email_address(candidate) if candidate else None


async def _validated_monitor_form(
    name: str,
    url: str,
    check_frequency: str,
) -> tuple[tuple[str, str, str] | None, dict[str, str]]:
    """Validate monitor fields, returning cleaned values or per-field errors."""
    errors: dict[str, str] = {}
    clean_name = name.strip()
    if not clean_name:
        errors["name"] = "Enter the competitor's name."
    clean_url = ""
    if not url.strip():
        errors["url"] = "Enter the address of the page to monitor."
    else:
        try:
            clean_url = await _validate_public_url_async(url)
        except URLValidationError as exc:
            errors["url"] = friendly_error(str(exc))
    frequency = ""
    try:
        frequency = validate_frequency(check_frequency)
    except ValueError as exc:
        errors["check_frequency"] = str(exc)
    if errors:
        return None, errors
    return (clean_name, clean_url, frequency), errors


def _monitor_form_values(
    *,
    name: str,
    url: str,
    check_frequency: str,
    enabled: str | None,
    alert_email: str = "",
) -> dict[str, str]:
    return {
        "name": name,
        "url": url,
        "check_frequency": check_frequency,
        "enabled": "yes" if enabled is not None else "",
        "alert_email": alert_email,
    }


def _record_check_failure(target_id: int, error: str) -> None:
    try:
        with db() as conn:
            conn.execute(
                """
                UPDATE targets
                SET last_check_at=?, last_check_error=?
                WHERE id=?
                """,
                (
                    datetime.now(timezone.utc).isoformat(),
                    error[:400],
                    target_id,
                ),
            )
    except sqlite3.Error:
        logger.exception(
            "Could not record check failure for target %s",
            target_id,
        )


async def run_target_check(
    target_id: int,
    *,
    email_sender: EmailSender | None = None,
) -> CheckResult:
    if target_id in _active_checks:
        return CheckResult(
            status="already_running",
            error="A check is already running for this target.",
        )
    _active_checks.add(target_id)
    shot: Path | None = None
    capture_saved = False
    try:
        with db() as conn:
            target = conn.execute(
                """
                SELECT target.*,
                       customer.email AS customer_email,
                       customer.active AS customer_active
                FROM targets target
                LEFT JOIN customers customer ON customer.id=target.customer_id
                WHERE target.id=?
                """,
                (target_id,),
            ).fetchone()
            if target is None:
                return CheckResult(
                    status="not_found",
                    error="Target not found.",
                )
            previous = conn.execute(
                "SELECT * FROM captures "
                "WHERE target_id=? ORDER BY id DESC LIMIT 1",
                (target_id,),
            ).fetchone()

        try:
            shot, text = await capture_page(target_id, target["url"])
        except (URLValidationError, CaptureError) as exc:
            error = _clean_error(exc)
            _record_check_failure(target_id, error)
            logger.warning("Check failed for target %s: %s", target_id, error)
            return CheckResult(status="failed", error=error)

        text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
        visual_score: float | None = 0.0
        text_score = 0.0
        categories = ""
        structured_changes: tuple[StructuredChange, ...] = ()
        summary = "Baseline capture created."
        if previous:
            visual_error = False
            try:
                visual_score = visual_change_score(
                    Path(previous["screenshot_path"]),
                    shot,
                )
            except (OSError, ValueError) as exc:
                visual_error = True
                visual_score = None
                logger.warning(
                    "Visual comparison failed for target %s: %s",
                    target_id,
                    exc,
                )
            assessment = assess_change(
                previous["text_content"],
                text,
                visual_score=visual_score,
            )
            if assessment.meaningful:
                structured_changes = extract_structured_changes(
                    previous["text_content"],
                    text,
                )
            reported_category_set = {
                *assessment.categories,
                *(change.category for change in structured_changes),
            }
            reported_categories = tuple(
                category
                for category in BUSINESS_CATEGORIES
                if category in reported_category_set
            )
            if reported_categories != assessment.categories:
                assessment = ChangeAssessment(
                    text_score=assessment.text_score,
                    meaningful=assessment.meaningful,
                    categories=reported_categories,
                    changed_tokens=assessment.changed_tokens,
                )
            text_score = assessment.text_score
            categories = ", ".join(assessment.categories)
            summary = _change_summary(
                assessment,
                visual_score,
                structured_changes,
                visual_error=visual_error,
            )
            if assessment.meaningful:
                ai = await ai_summary(
                    previous["text_content"],
                    text,
                    assessment.categories,
                )
                if ai:
                    summary = f"{summary} {ai}"

        created_at = datetime.now(timezone.utc).isoformat()
        alert_status = "pending" if structured_changes else "not_applicable"
        alert_recipient = (
            (target["alert_email"] or target["customer_email"])
            if structured_changes
            else None
        )
        try:
            with db() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO captures(
                        target_id,
                        created_at,
                        screenshot_path,
                        text_content,
                        text_hash,
                        visual_score,
                        text_score,
                        categories,
                        structured_changes,
                        alert_status,
                        alert_recipient,
                        summary
                    )
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        target_id,
                        created_at,
                        str(shot),
                        text,
                        text_hash,
                        visual_score,
                        text_score,
                        categories,
                        serialize_structured_changes(structured_changes),
                        alert_status,
                        alert_recipient,
                        summary,
                    ),
                )
                capture_id = int(cursor.lastrowid)
                conn.execute(
                    """
                    UPDATE targets
                    SET last_check_at=?, last_check_error=NULL
                    WHERE id=?
                    """,
                    (created_at, target_id),
                )
            capture_saved = True
        except sqlite3.Error:
            error = (
                "The page was captured, but the result could not be saved. "
                "Please try again."
            )
            _record_check_failure(target_id, error)
            logger.exception("Failed to save capture for target %s", target_id)
            return CheckResult(status="failed", error=error)

        if alert_status == "pending":
            alert_status = await deliver_capture_alert(
                capture_id,
                email_sender=email_sender,
            )
        return CheckResult(
            status="completed",
            capture_id=capture_id,
            alert_status=alert_status,
        )
    except Exception as exc:
        error = (
            "The check failed unexpectedly. No capture was saved; please try again."
        )
        _record_check_failure(target_id, error)
        logger.exception(
            "Unexpected check failure for target %s (%s)",
            target_id,
            type(exc).__name__,
        )
        return CheckResult(status="failed", error=error)
    finally:
        if shot is not None and not capture_saved:
            try:
                shot.unlink(missing_ok=True)
            except OSError:
                logger.exception(
                    "Could not remove unsaved screenshot for target %s",
                    target_id,
                )
        _active_checks.discard(target_id)


CheckRunner = Callable[[int], Awaitable[CheckResult]]


async def run_due_checks(
    *,
    now: datetime | None = None,
    check_runner: CheckRunner | None = None,
) -> tuple[int, ...]:
    with db() as conn:
        target_ids = tuple(
            int(row["id"])
            for row in conn.execute("SELECT id FROM targets ORDER BY id").fetchall()
        )

    runner = check_runner or run_target_check
    attempted: list[int] = []
    for target_id in target_ids:
        with db() as conn:
            target = conn.execute(
                """
                SELECT target.id,
                       target.customer_id,
                       target.enabled,
                       target.check_frequency,
                       target.last_check_at,
                       customer.active AS customer_active
                FROM targets target
                LEFT JOIN customers customer ON customer.id=target.customer_id
                WHERE target.id=?
                """,
                (target_id,),
            ).fetchone()
        if target is None:
            continue
        try:
            due = target_is_due(
                enabled=(
                    bool(target["enabled"])
                    and (
                        target["customer_id"] is None
                        or bool(target["customer_active"])
                    )
                ),
                frequency=target["check_frequency"],
                last_checked_at=target["last_check_at"],
                now=now,
            )
        except ValueError as exc:
            logger.error(
                "Skipping target %s with invalid schedule: %s",
                target["id"],
                exc,
            )
            continue
        if not due:
            continue
        attempted.append(target_id)
        try:
            result = await runner(target_id)
        except Exception:
            logger.exception(
                "Scheduled check crashed for target %s; continuing.",
                target_id,
            )
            continue
        if result.status == "failed":
            logger.error(
                "Scheduled check failed for target %s: %s",
                target_id,
                result.error or "unknown error",
            )
        elif result.status == "already_running":
            logger.info(
                "Scheduled check skipped target %s because it is already running.",
                target_id,
            )
    return tuple(attempted)


async def scheduler_loop() -> None:
    logger.info(
        "ChangeScout scheduler started (poll interval: %s seconds).",
        SCHEDULER_POLL_SECONDS,
    )
    while True:
        try:
            await run_due_checks()
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Scheduler cycle failed; the next cycle will retry.")
        await asyncio.sleep(SCHEDULER_POLL_SECONDS)


@app.post("/app/targets")
async def create_target(
    name: str = Form(...),
    url: str = Form(...),
    enabled: str | None = Form(None),
    alert_email: str = Form(""),
    check_frequency: str = Form("daily"),
):
    cleaned, errors = await _validated_monitor_form(name, url, check_frequency)
    recipient: str | None = None
    try:
        recipient = _validated_optional_email(alert_email)
    except ValueError as exc:
        errors["alert_email"] = str(exc)
    if cleaned is None or errors:
        return _render_monitors(
            form_values=_monitor_form_values(
                name=name,
                url=url,
                check_frequency=check_frequency,
                enabled=enabled,
                alert_email=alert_email,
            ),
            form_errors=errors,
            status_code=400,
        )
    name, url, frequency = cleaned
    with db() as conn:
        conn.execute(
            """
            INSERT INTO targets(
                name,
                url,
                enabled,
                alert_email,
                check_frequency,
                created_at
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                name,
                url,
                int(enabled is not None),
                recipient,
                frequency,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    notice = quote("Monitor added.", safe="")
    return RedirectResponse(f"/app/monitors?notice={notice}", status_code=303)


@app.post("/app/customer/{customer_id}/targets")
async def create_customer_target(
    customer_id: int,
    name: str = Form(...),
    url: str = Form(...),
    enabled: str | None = Form(None),
    check_frequency: str = Form("daily"),
):
    customer = _get_customer(customer_id)
    cleaned, errors = await _validated_monitor_form(name, url, check_frequency)
    if cleaned is None:
        return HTMLResponse(
            pages.competitor_form_page(
                customer=customer,
                values=_monitor_form_values(
                    name=name,
                    url=url,
                    check_frequency=check_frequency,
                    enabled=enabled,
                ),
                errors=errors,
            ),
            status_code=400,
        )
    name, url, frequency = cleaned
    with db() as conn:
        conn.execute(
            """
            INSERT INTO targets(
                customer_id,
                name,
                url,
                enabled,
                alert_email,
                check_frequency,
                created_at
            )
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                customer_id,
                name,
                url,
                int(enabled is not None),
                customer["email"],
                frequency,
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    notice = quote(f"{name} added. The first scan will capture a baseline.", safe="")
    return RedirectResponse(
        f"/app/customer/{customer_id}?notice={notice}",
        status_code=303,
    )


@app.post("/app/target/{target_id}/settings")
def update_target_settings(
    target_id: int,
    enabled: str | None = Form(None),
    alert_email: str = Form(""),
    check_frequency: str = Form("daily"),
):
    errors: dict[str, str] = {}
    recipient: str | None = None
    frequency = check_frequency
    try:
        recipient = _validated_optional_email(alert_email)
    except ValueError as exc:
        errors["alert_email"] = str(exc)
    try:
        frequency = validate_frequency(check_frequency)
    except ValueError as exc:
        errors["check_frequency"] = str(exc)
    if errors:
        return _render_report(
            target_id,
            settings_values={
                "alert_email": alert_email,
                "check_frequency": check_frequency,
                "enabled": "yes" if enabled is not None else "",
            },
            settings_errors=errors,
            status_code=400,
        )
    with db() as conn:
        updated = conn.execute(
            """
            UPDATE targets
            SET enabled=?, alert_email=?, check_frequency=?
            WHERE id=?
            """,
            (
                int(enabled is not None),
                recipient,
                frequency,
                target_id,
            ),
        )
    if updated.rowcount != 1:
        raise HTTPException(404, MONITOR_NOT_FOUND)
    notice = quote("Monitoring settings saved.", safe="")
    return RedirectResponse(
        f"/app/target/{target_id}?notice={notice}",
        status_code=303,
    )


@app.post("/app/check/{target_id}")
async def check(target_id: int):
    result = await run_target_check(target_id)
    if result.status == "not_found":
        raise HTTPException(404, MONITOR_NOT_FOUND)
    if not result.completed:
        message = quote(result.error or "The check did not complete.", safe="")
        return RedirectResponse(
            f"/app/target/{target_id}?error={message}",
            status_code=303,
        )
    return RedirectResponse(f"/app/target/{target_id}", status_code=303)


MONITOR_NOT_FOUND = "This monitor doesn't exist or was removed."


def _render_report(
    target_id: int,
    *,
    error: str | None = None,
    notice: str | None = None,
    settings_values: dict[str, str] | None = None,
    settings_errors: dict[str, str] | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    now = utc_now()
    with db() as conn:
        monitors = _load_monitors(conn, now=now, target_id=target_id)
        if not monitors:
            raise HTTPException(404, MONITOR_NOT_FOUND)
        history = conn.execute(
            """
            SELECT id, created_at, visual_score, text_score, categories,
                   structured_changes, alert_status, alert_recipient,
                   alert_sent_at, alert_error, summary
            FROM captures
            WHERE target_id=?
            ORDER BY id DESC
            LIMIT ?
            """,
            (target_id, HISTORY_LIMIT),
        ).fetchall()
        report = conn.execute(
            f"""
            SELECT capture.id, capture.created_at, capture.categories,
                   capture.structured_changes, capture.alert_status,
                   capture.alert_recipient, capture.alert_sent_at,
                   capture.alert_error
            FROM captures capture
            WHERE capture.target_id=?
              AND {CHANGES_CAPTURE}
            ORDER BY capture.id DESC
            LIMIT 1
            """,
            (target_id,),
        ).fetchone()
        newer_scans = (
            conn.execute(
                "SELECT COUNT(*) AS count FROM captures WHERE target_id=? AND id>?",
                (target_id, report["id"]),
            ).fetchone()["count"]
            if report is not None
            else 0
        )
    email_ready, email_error = _email_settings()
    monitor = monitors[0]
    return HTMLResponse(
        pages.report_page(
            now=now,
            monitor=monitor,
            history=history,
            first_capture_id=monitor.first_capture_id,
            report=report,
            report_changes=_capture_changes(report) if report is not None else (),
            newer_scans=int(newer_scans),
            email_ready=email_ready,
            email_error=email_error,
            error=error,
            notice=notice,
            settings_values=settings_values,
            settings_errors=settings_errors,
        ),
        status_code=status_code,
    )


@app.get("/app/target/{target_id}", response_class=HTMLResponse)
def target_report(
    target_id: int,
    error: str | None = None,
    notice: str | None = None,
):
    return _render_report(target_id, error=error, notice=notice)
