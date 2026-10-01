import asyncio
import hashlib
import logging
import os
import sqlite3
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from html import escape
from pathlib import Path
from urllib.parse import quote, urlparse

from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
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

from display_time import format_display_time
from emailing import (
    EmailContent,
    EmailDeliveryError,
    SMTPSettings,
    render_change_alert,
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
    FREQUENCY_LABELS,
    next_check_at,
    target_is_due,
    validate_frequency,
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


app = FastAPI(title="ChangeScout", lifespan=lifespan)


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


EmailSender = Callable[[SMTPSettings, str, EmailContent], None]


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS targets(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            enabled INTEGER NOT NULL DEFAULT 1,
            alert_email TEXT,
            check_frequency TEXT NOT NULL DEFAULT 'daily',
            last_check_at TEXT,
            last_check_error TEXT,
            created_at TEXT NOT NULL
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
    return f"{base_url}/target/{target_id}"


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
            SELECT c.*, t.name AS target_name, t.url AS target_url
            FROM captures c
            JOIN targets t ON t.id=c.target_id
            WHERE c.id=?
            """,
            (capture_id,),
        ).fetchone()
    if capture is None:
        raise ValueError(f"Capture {capture_id} does not exist.")
    if capture["alert_status"] != "pending":
        return str(capture["alert_status"])

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

    smtp_settings = SMTPSettings.from_environment()
    if not smtp_settings.configured:
        error = smtp_settings.configuration_error or "SMTP is not configured."
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
            smtp_settings,
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


def _html(value: object) -> str:
    return escape(str(value), quote=True)


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


def _change_cards(changes: tuple[StructuredChange, ...]) -> str:
    return "".join(
        f"""
<article class="change-card">
<span class="change-category">{_html(change.category)}</span>
<div class="change-values">
<div><span class="change-label">Old</span><strong>{_html(change.old_value)}</strong></div>
<div class="change-arrow" aria-hidden="true">→</div>
<div><span class="change-label">New</span><strong>{_html(change.new_value)}</strong></div>
</div>
<p class="change-impact"><span class="change-label">Impact</span>{_html(change.description)}</p>
</article>
"""
        for change in changes
    )


def _alert_display(capture: sqlite3.Row) -> str:
    status = capture["alert_status"] or "not_applicable"
    recipient = capture["alert_recipient"] or ""
    error = capture["alert_error"] or ""
    if status == "sent":
        sent_at = (
            format_display_time(capture["alert_sent_at"])
            if capture["alert_sent_at"] else "unknown time"
        )
        return (
            f"<span class='status good'>Sent</span><br>"
            f"<span class='muted'>{_html(recipient)} at {_html(sent_at)}</span>"
        )
    if status in {"pending", "sending"}:
        label = "Pending" if status == "pending" else "Sending"
        return f"<span class='status pending'>{label}</span>"
    if status in {"failed", "not_configured", "no_recipient", "indeterminate"}:
        if status == "failed":
            label = "Failed"
        elif status == "indeterminate":
            label = "Delivery unknown"
        else:
            label = "Not sent"
        return (
            f"<span class='status warning'>{label}</span><br>"
            f"<span class='muted'>{_html(error)}</span>"
        )
    return "<span class='muted'>Not needed</span>"


def _latest_changes_panel(capture: sqlite3.Row | None) -> str:
    if capture is None:
        return ""
    changes = _capture_changes(capture)
    if not changes:
        return ""
    return f"""
<div class="card">
<h2>Latest business changes</h2>
<p class="muted">Detected {_html(format_display_time(capture['created_at']))}. Showing {len(changes)} high-confidence changes.</p>
<p><b>Email alert:</b> {_alert_display(capture)}</p>
<div class="change-list">{_change_cards(changes)}</div>
</div>
"""


def _history_summary(capture: sqlite3.Row) -> str:
    summary = _html(capture["summary"] or "")
    changes = _capture_changes(capture)
    if not changes:
        return summary
    count = len(changes)
    noun = "change" if count == 1 else "changes"
    return (
        f"{summary}"
        f"<details class='history-changes'><summary>View {count} structured {noun}</summary>"
        f"<div class='change-list compact'>{_change_cards(changes)}</div></details>"
    )


def _frequency_options(selected: str) -> str:
    return "".join(
        f"<option value='{_html(value)}'"
        f"{' selected' if value == selected else ''}>"
        f"{_html(label)}</option>"
        for value, label in FREQUENCY_LABELS.items()
    )


def _next_check_display(target: sqlite3.Row) -> str:
    if not bool(target["enabled"]):
        return "Disabled"
    try:
        due_at = next_check_at(
            target["last_check_at"],
            target["check_frequency"],
        )
    except ValueError:
        return "Invalid frequency"
    return "Due now" if due_at is None else format_display_time(due_at)


def _smtp_configuration_notice() -> str:
    settings = SMTPSettings.from_environment()
    if settings.configured:
        return (
            "<p class='status good'>SMTP email delivery is configured.</p>"
        )
    return (
        "<p class='status warning'>Email delivery is not configured: "
        f"{_html(settings.configuration_error or 'SMTP configuration is incomplete.')} "
        "Monitoring and reports will continue, but alerts will not be sent.</p>"
    )


def page_shell(content: str) -> HTMLResponse:
    return HTMLResponse(f"""
<!doctype html>
<html>
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>ChangeScout</title>
<style>
body{{font-family:ui-sans-serif,system-ui;margin:0;background:#f5f7fb;color:#101828}}
.wrap{{max-width:1050px;margin:42px auto;padding:0 20px}}
.card{{background:white;border:1px solid #e4e7ec;border-radius:18px;padding:22px;margin:16px 0;box-shadow:0 8px 24px rgba(16,24,40,.05)}}
h1{{font-size:34px;margin-bottom:4px}} h2{{margin-top:0}}
input,select{{padding:12px;border:1px solid #d0d5dd;border-radius:10px;width:100%;box-sizing:border-box;margin:6px 0 12px;background:white}}
input[type=checkbox]{{width:auto;margin-right:8px}}
button{{padding:11px 16px;border:0;border-radius:10px;background:#101828;color:white;cursor:pointer}}
a{{color:#175cd3;text-decoration:none}} .muted{{color:#667085}} .pill{{display:inline-block;background:#eef2ff;border-radius:999px;padding:5px 10px;margin-right:6px}}
.alert{{background:#fff4ed;border:1px solid #ff8a4c;color:#9c2a10;border-radius:12px;padding:14px;margin:16px 0}}
.status{{font-weight:700}} .status.good{{color:#067647}} .status.warning{{color:#b54708}} .status.pending{{color:#175cd3}}
.settings-grid{{display:grid;grid-template-columns:1fr 1fr;gap:14px}} .settings-grid .wide{{grid-column:1/-1}}
.change-list{{display:grid;gap:12px}}
.change-card{{border:1px solid #d0d5dd;border-left:4px solid #175cd3;border-radius:12px;padding:16px;background:#fcfcfd}}
.change-category{{display:inline-block;font-size:12px;font-weight:700;letter-spacing:.06em;color:#175cd3;background:#eff8ff;border-radius:999px;padding:4px 9px;margin-bottom:12px}}
.change-values{{display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);gap:14px;align-items:center}}
.change-values strong{{display:block;overflow-wrap:anywhere}}
.change-label{{display:block;color:#667085;font-size:12px;font-weight:700;text-transform:uppercase;letter-spacing:.04em;margin-bottom:3px}}
.change-arrow{{color:#667085;font-size:20px}}
.change-impact{{margin:14px 0 0;padding-top:12px;border-top:1px solid #eaecf0}}
.history-changes{{margin-top:8px}} .history-changes summary{{color:#175cd3;cursor:pointer}}
.change-list.compact{{margin-top:10px;min-width:440px}}
table{{width:100%;border-collapse:collapse}} td,th{{padding:10px;border-bottom:1px solid #eaecf0;text-align:left;vertical-align:top}}
@media(max-width:700px){{.change-values,.settings-grid{{grid-template-columns:1fr}}.change-arrow{{transform:rotate(90deg)}}.change-list.compact{{min-width:0}}}}
</style>
</head>
<body><div class="wrap">{content}</div></body></html>
""")


def _dashboard_target_row(target: sqlite3.Row) -> str:
    enabled = bool(target["enabled"])
    monitoring_class = "good" if enabled else "warning"
    monitoring_label = "Enabled" if enabled else "Disabled"
    frequency = FREQUENCY_LABELS.get(
        target["check_frequency"],
        target["check_frequency"],
    )
    last_error = (
        f"<br><span class='status warning'>{_html(target['last_check_error'])}</span>"
        if target["last_check_error"]
        else ""
    )
    has_been_checked = target["last_check_at"] is not None
    return f"""
<tr>
<td><b>{_html(target['name'])}</b><br><span class="muted">{_html(target['url'])}</span></td>
<td><span class="status {monitoring_class}">{monitoring_label}</span><br><span class="muted">{_html(frequency)}</span></td>
<td>{_html(target['alert_email'] or 'Not configured')}</td>
<td>{_html(format_display_time(target['last_check_at']) if target['last_check_at'] else 'Never')}{last_error}</td>
<td>{_html(_next_check_display(target))}</td>
<td>{_score_display(target['visual_score']) if has_been_checked else ''}</td>
<td>{_score_display(target['text_score']) if has_been_checked else ''}</td>
<td><form method="post" action="/check/{target['id']}"><button>Check now</button></form></td>
<td><a href="/target/{target['id']}">Report &amp; settings</a></td>
</tr>
"""


@app.get("/", response_class=HTMLResponse)
def home():
    with db() as conn:
        targets = conn.execute("""
        SELECT t.*,
               (SELECT visual_score FROM captures c WHERE c.target_id=t.id ORDER BY c.id DESC LIMIT 1) visual_score,
               (SELECT text_score FROM captures c WHERE c.target_id=t.id ORDER BY c.id DESC LIMIT 1) text_score
        FROM targets t ORDER BY t.id DESC
        """).fetchall()

    rows = "".join(_dashboard_target_row(target) for target in targets)
    return page_shell(f"""
<h1>ChangeScout</h1>
<p class="muted">See what changed on competitor pages without checking them manually.</p>
<div class="card">
<h2>Add competitor</h2>
<form method="post" action="/targets">
<label>Name</label><input name="name" placeholder="Acme Widgets" required>
<label>Public URL</label><input name="url" placeholder="https://example.com/pricing" required>
<div class="settings-grid">
<div><label>Alert email</label><input type="email" name="alert_email" placeholder="owner@example.com"></div>
<div><label>Check frequency</label><select name="check_frequency">{_frequency_options('daily')}</select></div>
<label class="wide"><input type="checkbox" name="enabled" value="yes" checked>Enable automatic monitoring</label>
</div>
<button>Add monitor</button>
</form>
</div>
<div class="card">
<h2>Monitors</h2>
<table><tr><th>Target</th><th>Monitoring</th><th>Alert recipient</th><th>Last check</th><th>Next check</th><th>Visual</th><th>Text</th><th></th><th></th></tr>{rows or '<tr><td colspan=9>No monitors yet.</td></tr>'}</table>
</div>
""")

def _validated_optional_email(value: str) -> str | None:
    candidate = value.strip()
    return validate_email_address(candidate) if candidate else None


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
                "SELECT * FROM targets WHERE id=?",
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
            target["alert_email"] if structured_changes else None
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
                SELECT id, enabled, check_frequency, last_check_at
                FROM targets
                WHERE id=?
                """,
                (target_id,),
            ).fetchone()
        if target is None:
            continue
        try:
            due = target_is_due(
                enabled=bool(target["enabled"]),
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


@app.post("/targets")
async def create_target(
    name: str = Form(...),
    url: str = Form(...),
    enabled: str | None = Form(None),
    alert_email: str = Form(""),
    check_frequency: str = Form("daily"),
):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Name is required.")
    try:
        url = await _validate_public_url_async(url)
        recipient = _validated_optional_email(alert_email)
        frequency = validate_frequency(check_frequency)
    except (URLValidationError, ValueError) as exc:
        raise HTTPException(400, str(exc)) from exc
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
    return RedirectResponse("/", status_code=303)


@app.post("/target/{target_id}/settings")
def update_target_settings(
    target_id: int,
    enabled: str | None = Form(None),
    alert_email: str = Form(""),
    check_frequency: str = Form("daily"),
):
    try:
        recipient = _validated_optional_email(alert_email)
        frequency = validate_frequency(check_frequency)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
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
        raise HTTPException(404)
    notice = quote("Monitoring settings saved.", safe="")
    return RedirectResponse(
        f"/target/{target_id}?notice={notice}",
        status_code=303,
    )


@app.post("/check/{target_id}")
async def check(target_id: int):
    result = await run_target_check(target_id)
    if result.status == "not_found":
        raise HTTPException(404)
    if not result.completed:
        message = quote(result.error or "The check did not complete.", safe="")
        return RedirectResponse(
            f"/target/{target_id}?error={message}",
            status_code=303,
        )
    return RedirectResponse(f"/target/{target_id}", status_code=303)

@app.get("/target/{target_id}", response_class=HTMLResponse)
def target_report(
    target_id: int,
    error: str | None = None,
    notice: str | None = None,
):
    with db() as conn:
        target = conn.execute("SELECT * FROM targets WHERE id=?", (target_id,)).fetchone()
        caps = conn.execute("SELECT * FROM captures WHERE target_id=? ORDER BY id DESC LIMIT 20", (target_id,)).fetchall()
    if not target:
        raise HTTPException(404)
    rows = "".join(
        f"<tr><td>{_html(format_display_time(c['created_at']))}</td>"
        f"<td>{_score_display(c['visual_score'])}</td>"
        f"<td>{_score_display(c['text_score'])}</td>"
        f"<td>{_html(c['categories'] or '—')}</td>"
        f"<td>{_alert_display(c)}</td>"
        f"<td>{_history_summary(c)}</td></tr>"
        for c in caps
    )
    latest_changes = _latest_changes_panel(caps[0] if caps else None)
    error_message = (
        f"<div class='alert' role='alert'>{_html(error)}</div>" if error else ""
    )
    notice_message = (
        f"<div class='card status good' role='status'>{_html(notice)}</div>"
        if notice
        else ""
    )
    check_error = (
        f"<div class='alert' role='alert'><b>Last check failed:</b> "
        f"{_html(target['last_check_error'])}</div>"
        if target["last_check_error"] and not error
        else ""
    )
    return page_shell(f"""
<p><a href="/">← Dashboard</a></p>
<h1>{_html(target['name'])}</h1>
<p><a href="{_html(target['url'])}" target="_blank" rel="noopener noreferrer">{_html(target['url'])}</a></p>
{error_message}
{notice_message}
{check_error}
<form method="post" action="/check/{target_id}"><button>Run check</button></form>
<div class="card">
<h2>Monitoring settings</h2>
{_smtp_configuration_notice()}
<form method="post" action="/target/{target_id}/settings">
<div class="settings-grid">
<div>
<label>Alert email</label>
<input type="email" name="alert_email" value="{_html(target['alert_email'] or '')}" placeholder="owner@example.com">
</div>
<div>
<label>Check frequency</label>
<select name="check_frequency">{_frequency_options(target['check_frequency'])}</select>
</div>
<label class="wide"><input type="checkbox" name="enabled" value="yes"{' checked' if target['enabled'] else ''}>Enable automatic monitoring</label>
</div>
<p class="muted">Last checked: {_html(format_display_time(target['last_check_at']) if target['last_check_at'] else 'Never')} · Next check: {_html(_next_check_display(target))}</p>
<button>Save settings</button>
</form>
</div>
{latest_changes}
<div class="card">
<h2>Change history</h2>
<table><tr><th>Checked</th><th>Visual</th><th>Text</th><th>Category</th><th>Email alert</th><th>Summary</th></tr>{rows or '<tr><td colspan=6>No captures yet.</td></tr>'}</table>
</div>
""")