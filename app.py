import asyncio
import difflib
import hashlib
import io
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from bs4 import BeautifulSoup
from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, RedirectResponse
from PIL import Image, ImageChops, ImageStat
from playwright.async_api import async_playwright

BASE = Path(__file__).resolve().parent
DATA = BASE / "data"
SHOTS = DATA / "screenshots"
DATA.mkdir(exist_ok=True)
SHOTS.mkdir(exist_ok=True)
DB = DATA / "changescout.db"

app = FastAPI(title="ChangeScout")

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
            summary TEXT,
            FOREIGN KEY(target_id) REFERENCES targets(id)
        )
        """)

init_db()

def safe_url(url: str) -> str:
    u = urlparse(url.strip())
    if u.scheme not in {"http", "https"} or not u.netloc:
        raise ValueError("Use a full public http/https URL.")
    host = (u.hostname or "").lower()
    if host in {"localhost", "127.0.0.1", "::1"} or host.endswith(".local"):
        raise ValueError("Local/private addresses are not supported.")
    return url.strip()

def extract_visible_text(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript", "svg"]):
        tag.decompose()
    text = " ".join(soup.stripped_strings)
    return " ".join(text.split())[:120000]

def text_change_score(old: str, new: str) -> float:
    if not old and not new:
        return 0.0
    ratio = difflib.SequenceMatcher(a=old[:50000], b=new[:50000]).ratio()
    return round((1 - ratio) * 100, 1)

def visual_change_score(old_path: Path, new_path: Path) -> float:
    try:
        a = Image.open(old_path).convert("RGB")
        b = Image.open(new_path).convert("RGB")
        w = min(a.width, b.width, 1440)
        h = min(a.height, b.height, 5000)
        a = a.crop((0, 0, w, h)).resize((360, max(1, int(h * 360 / w))))
        b = b.crop((0, 0, w, h)).resize(a.size)
        diff = ImageChops.difference(a, b)
        stat = ImageStat.Stat(diff)
        mean = sum(stat.mean) / (3 * 255)
        return round(mean * 100, 1)
    except Exception:
        return 100.0

async def capture_page(target_id: int, url: str):
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = SHOTS / f"{target_id}_{stamp}.png"
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1000})
        await page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            await page.wait_for_load_state("networkidle", timeout=8000)
        except Exception:
            pass
        html = await page.content()
        await page.screenshot(path=str(path), full_page=True)
        await browser.close()
    return path, extract_visible_text(html)

async def ai_summary(url: str, old_text: str, new_text: str) -> str | None:
    if not os.getenv("OPENAI_API_KEY"):
        return None
    try:
        from browser_use import Agent, ChatOpenAI
        prompt = f"""
Visit {url}. This is a public competitor-monitoring task.
Summarize only commercially meaningful changes visible on the current page.
Use the previous extracted page text below as context.
Focus on price, offer, product/service, headline, CTA, shipping/promotion, or positioning changes.
Do not log in, bypass access controls, or interact beyond normal public-page viewing.

PREVIOUS TEXT:
{old_text[:12000]}
"""
        llm = ChatOpenAI(model="gpt-5.6-luna")
        agent = Agent(task=prompt, llm=llm)
        history = await agent.run(max_steps=12)
        return history.final_result()
    except Exception as exc:
        return f"AI summary unavailable: {type(exc).__name__}"

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
        f"""<tr><td><b>{t['name']}</b><br><span class='muted'>{t['url']}</span></td>
        <td>{t['last_checked'] or 'Never'}</td>
        <td>{'' if t['visual_score'] is None else str(t['visual_score'])+'%'}</td>
        <td>{'' if t['text_score'] is None else str(t['text_score'])+'%'}</td>
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
def create_target(name: str = Form(...), url: str = Form(...)):
    try:
        url = safe_url(url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    with db() as conn:
        conn.execute("INSERT INTO targets(name,url,created_at) VALUES(?,?,?)",
                     (name.strip(), url, datetime.now(timezone.utc).isoformat()))
    return RedirectResponse("/", status_code=303)

@app.post("/check/{target_id}")
async def check(target_id: int):
    with db() as conn:
        target = conn.execute("SELECT * FROM targets WHERE id=?", (target_id,)).fetchone()
        if not target:
            raise HTTPException(404)
        previous = conn.execute("SELECT * FROM captures WHERE target_id=? ORDER BY id DESC LIMIT 1", (target_id,)).fetchone()

    shot, text = await capture_page(target_id, target["url"])
    h = hashlib.sha256(text.encode("utf-8")).hexdigest()
    vscore = tscore = 0.0
    summary = "Baseline capture created."
    if previous:
        vscore = visual_change_score(Path(previous["screenshot_path"]), shot)
        tscore = text_change_score(previous["text_content"], text)
        if max(vscore, tscore) < 3:
            summary = "No meaningful change detected."
        else:
            summary = f"Change detected: {tscore}% text difference and {vscore}% visual difference."
            ai = await ai_summary(target["url"], previous["text_content"], text)
            if ai:
                summary = ai

    with db() as conn:
        conn.execute("""
        INSERT INTO captures(target_id,created_at,screenshot_path,text_content,text_hash,visual_score,text_score,summary)
        VALUES(?,?,?,?,?,?,?,?)
        """, (target_id, datetime.now(timezone.utc).isoformat(), str(shot), text, h, vscore, tscore, summary))
    return RedirectResponse(f"/target/{target_id}", status_code=303)

@app.get("/target/{target_id}", response_class=HTMLResponse)
def target_report(target_id: int):
    with db() as conn:
        target = conn.execute("SELECT * FROM targets WHERE id=?", (target_id,)).fetchone()
        caps = conn.execute("SELECT * FROM captures WHERE target_id=? ORDER BY id DESC LIMIT 20", (target_id,)).fetchall()
    if not target:
        raise HTTPException(404)
    rows = "".join(
        f"<tr><td>{c['created_at']}</td><td>{c['visual_score']}%</td><td>{c['text_score']}%</td><td>{(c['summary'] or '')}</td></tr>"
        for c in caps
    )
    return page_shell(f"""
<p><a href="/">← Dashboard</a></p>
<h1>{target['name']}</h1>
<p><a href="{target['url']}" target="_blank">{target['url']}</a></p>
<form method="post" action="/check/{target_id}"><button>Run check</button></form>
<div class="card">
<h2>Change history</h2>
<table><tr><th>Checked</th><th>Visual</th><th>Text</th><th>Summary</th></tr>{rows or '<tr><td colspan=4>No captures yet.</td></tr>'}</table>
</div>
""")