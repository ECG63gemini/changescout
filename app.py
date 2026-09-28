import asyncio
import hashlib
import logging
import os
import sqlite3
from contextlib import suppress
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

from monitoring import (
    ChangeAssessment,
    URLValidationError,
    assess_change,
    extract_visible_text,
    validate_public_url,
)

BASE = Path(__file__).resolve().parent
load_dotenv(BASE / ".env")
DATA = BASE / "data"
SHOTS = DATA / "screenshots"
DATA.mkdir(exist_ok=True)
SHOTS.mkdir(exist_ok=True)
DB = DATA / "changescout.db"
MAX_SCREENSHOT_HEIGHT = 12_000
MAX_SCREENSHOT_WIDTH = 1_440
NAVIGATION_TIMEOUT_MS = 60_000
NETWORK_IDLE_TIMEOUT_MS = 5_000
URL_VALIDATION_TIMEOUT_SECONDS = 5
CAPTURE_ATTEMPTS = 2
SAFE_BROWSER_SCHEMES = {"about", "blob", "data"}

app = FastAPI(title="ChangeScout")
logger = logging.getLogger("changescout")


class CaptureError(RuntimeError):
    pass

def db():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with db() as conn:
        conn.execute("""
        CREATE TABLE IF NOT EXISTS targets(
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
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
            summary TEXT,
            FOREIGN KEY(target_id) REFERENCES targets(id)
        )
        """)
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(captures)").fetchall()
        }
        if "categories" not in columns:
            conn.execute("ALTER TABLE captures ADD COLUMN categories TEXT")
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


def _html(value: object) -> str:
    return escape(str(value), quote=True)


def _score_display(value: object) -> str:
    if value is None:
        return "Unavailable"
    return f"{float(value):.1f}%"


def _change_summary(
    assessment: ChangeAssessment,
    visual_score: float | None,
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
    summary = (
        f"{categories} change detected: {assessment.text_score:.1f}% text "
        f"difference and {visual} visual difference."
    )
    if visual_error:
        summary += " Visual comparison was unavailable for this check."
    return summary


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
input{{padding:12px;border:1px solid #d0d5dd;border-radius:10px;width:100%;box-sizing:border-box;margin:6px 0 12px}}
button{{padding:11px 16px;border:0;border-radius:10px;background:#101828;color:white;cursor:pointer}}
a{{color:#175cd3;text-decoration:none}} .muted{{color:#667085}} .pill{{display:inline-block;background:#eef2ff;border-radius:999px;padding:5px 10px;margin-right:6px}}
.alert{{background:#fff4ed;border:1px solid #ff8a4c;color:#9c2a10;border-radius:12px;padding:14px;margin:16px 0}}
table{{width:100%;border-collapse:collapse}} td,th{{padding:10px;border-bottom:1px solid #eaecf0;text-align:left;vertical-align:top}}
</style>
</head>
<body><div class="wrap">{content}</div></body></html>
""")

@app.get("/", response_class=HTMLResponse)
def home():
    with db() as conn:
        targets = conn.execute("""
        SELECT t.*,
               (SELECT created_at FROM captures c WHERE c.target_id=t.id ORDER BY c.id DESC LIMIT 1) last_checked,
               (SELECT visual_score FROM captures c WHERE c.target_id=t.id ORDER BY c.id DESC LIMIT 1) visual_score,
               (SELECT text_score FROM captures c WHERE c.target_id=t.id ORDER BY c.id DESC LIMIT 1) text_score
        FROM targets t ORDER BY t.id DESC
        """).fetchall()

    rows = "".join(
        f"""<tr><td><b>{_html(t['name'])}</b><br><span class='muted'>{_html(t['url'])}</span></td>
        <td>{_html(t['last_checked'] or 'Never')}</td>
        <td>{'' if t['last_checked'] is None else _score_display(t['visual_score'])}</td>
        <td>{'' if t['last_checked'] is None else _score_display(t['text_score'])}</td>
        <td><form method='post' action='/check/{t["id"]}'><button>Check now</button></form></td>
        <td><a href='/target/{t["id"]}'>Report</a></td></tr>"""
        for t in targets
    )
    return page_shell(f"""
<h1>ChangeScout</h1>
<p class="muted">See what changed on competitor pages without checking them manually.</p>
<div class="card">
<h2>Add competitor</h2>
<form method="post" action="/targets">
<label>Name</label><input name="name" placeholder="Acme Widgets" required>
<label>Public URL</label><input name="url" placeholder="https://example.com/pricing" required>
<button>Add monitor</button>
</form>
</div>
<div class="card">
<h2>Monitors</h2>
<table><tr><th>Target</th><th>Last check</th><th>Visual</th><th>Text</th><th></th><th></th></tr>{rows or '<tr><td colspan=6>No monitors yet.</td></tr>'}</table>
</div>
""")

@app.post("/targets")
async def create_target(name: str = Form(...), url: str = Form(...)):
    name = name.strip()
    if not name:
        raise HTTPException(400, "Name is required.")
    try:
        url = await _validate_public_url_async(url)
    except URLValidationError as e:
        raise HTTPException(400, str(e))
    with db() as conn:
        conn.execute("INSERT INTO targets(name,url,created_at) VALUES(?,?,?)",
                     (name, url, datetime.now(timezone.utc).isoformat()))
    return RedirectResponse("/", status_code=303)

@app.post("/check/{target_id}")
async def check(target_id: int):
    with db() as conn:
        target = conn.execute("SELECT * FROM targets WHERE id=?", (target_id,)).fetchone()
        if not target:
            raise HTTPException(404)
        previous = conn.execute("SELECT * FROM captures WHERE target_id=? ORDER BY id DESC LIMIT 1", (target_id,)).fetchone()

    try:
        shot, text = await capture_page(target_id, target["url"])
    except (URLValidationError, CaptureError) as exc:
        logger.warning("Check failed for target %s: %s", target_id, exc)
        message = quote(str(exc), safe="")
        return RedirectResponse(
            f"/target/{target_id}?error={message}",
            status_code=303,
        )
    except Exception:
        logger.exception("Unexpected check failure for target %s", target_id)
        message = quote(
            "The check failed unexpectedly. No capture was saved; please try again.",
            safe="",
        )
        return RedirectResponse(
            f"/target/{target_id}?error={message}",
            status_code=303,
        )

    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    vscore: float | None = 0.0
    tscore = 0.0
    categories = ""
    summary = "Baseline capture created."
    if previous:
        visual_error = False
        try:
            vscore = visual_change_score(Path(previous["screenshot_path"]), shot)
        except (OSError, ValueError) as exc:
            visual_error = True
            vscore = None
            logger.warning(
                "Visual comparison failed for target %s: %s",
                target_id,
                exc,
            )
        assessment = assess_change(
            previous["text_content"],
            text,
            visual_score=vscore,
        )
        tscore = assessment.text_score
        categories = ", ".join(assessment.categories)
        summary = _change_summary(
            assessment,
            vscore,
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

    try:
        with db() as conn:
            conn.execute("""
            INSERT INTO captures(
                target_id,
                created_at,
                screenshot_path,
                text_content,
                text_hash,
                visual_score,
                text_score,
                categories,
                summary
            )
            VALUES(?,?,?,?,?,?,?,?,?)
            """, (
                target_id,
                datetime.now(timezone.utc).isoformat(),
                str(shot),
                text,
                h,
                vscore,
                tscore,
                categories,
                summary,
            ))
    except sqlite3.Error:
        shot.unlink(missing_ok=True)
        logger.exception("Failed to save capture for target %s", target_id)
        message = quote(
            "The page was captured, but the result could not be saved. Please try again.",
            safe="",
        )
        return RedirectResponse(
            f"/target/{target_id}?error={message}",
            status_code=303,
        )
    return RedirectResponse(f"/target/{target_id}", status_code=303)

@app.get("/target/{target_id}", response_class=HTMLResponse)
def target_report(target_id: int, error: str | None = None):
    with db() as conn:
        target = conn.execute("SELECT * FROM targets WHERE id=?", (target_id,)).fetchone()
        caps = conn.execute("SELECT * FROM captures WHERE target_id=? ORDER BY id DESC LIMIT 20", (target_id,)).fetchall()
    if not target:
        raise HTTPException(404)
    rows = "".join(
        f"<tr><td>{_html(c['created_at'])}</td>"
        f"<td>{_score_display(c['visual_score'])}</td>"
        f"<td>{_score_display(c['text_score'])}</td>"
        f"<td>{_html(c['categories'] or '—')}</td>"
        f"<td>{_html(c['summary'] or '')}</td></tr>"
        for c in caps
    )
    error_message = (
        f"<div class='alert' role='alert'>{_html(error)}</div>" if error else ""
    )
    return page_shell(f"""
<p><a href="/">← Dashboard</a></p>
<h1>{_html(target['name'])}</h1>
<p><a href="{_html(target['url'])}" target="_blank" rel="noopener noreferrer">{_html(target['url'])}</a></p>
{error_message}
<form method="post" action="/check/{target_id}"><button>Run check</button></form>
<div class="card">
<h2>Change history</h2>
<table><tr><th>Checked</th><th>Visual</th><th>Text</th><th>Category</th><th>Summary</th></tr>{rows or '<tr><td colspan=5>No captures yet.</td></tr>'}</table>
</div>
""")