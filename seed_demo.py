import argparse
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw

from app import SHOTS, db, init_db
from monitoring import (
    BUSINESS_CATEGORIES,
    assess_change,
    extract_structured_changes,
    extract_visible_text,
    serialize_structured_changes,
)

BASE = Path(__file__).resolve().parent
DEMO = BASE / "demo"
DEMO_NAME = "Northstar Outfitters Demo"
DEMO_URL = "https://example.com/changescout-demo"


def _fixture_text(state: str) -> str:
    html = (DEMO / f"store_{state}.html").read_text(encoding="utf-8")
    return extract_visible_text(html)


def _write_screenshot(target_id: int, state: str, text: str) -> Path:
    path = SHOTS / f"demo_{target_id}_{state}.png"
    image = Image.new("RGB", (1_000, 700), "white")
    draw = ImageDraw.Draw(image)
    draw.text((50, 45), "Northstar Outfitters", fill="#101828")
    y = 110
    for line in text.splitlines():
        draw.text((50, y), line, fill="#344054")
        y += 42
    image.save(path)
    return path


def _remove_existing_demo() -> None:
    screenshot_paths: list[Path] = []
    with db() as conn:
        targets = conn.execute(
            "SELECT id FROM targets WHERE name=?",
            (DEMO_NAME,),
        ).fetchall()
        for target in targets:
            captures = conn.execute(
                "SELECT screenshot_path FROM captures WHERE target_id=?",
                (target["id"],),
            ).fetchall()
            screenshot_paths.extend(
                Path(capture["screenshot_path"]) for capture in captures
            )
            conn.execute(
                "DELETE FROM captures WHERE target_id=?",
                (target["id"],),
            )
        conn.execute("DELETE FROM targets WHERE name=?", (DEMO_NAME,))

    for path in screenshot_paths:
        if path.parent == SHOTS and path.name.startswith("demo_"):
            path.unlink(missing_ok=True)


def seed_before() -> int:
    _remove_existing_demo()
    text = _fixture_text("before")
    now = datetime.now(timezone.utc).isoformat()
    with db() as conn:
        cursor = conn.execute(
            "INSERT INTO targets(name,url,created_at) VALUES(?,?,?)",
            (DEMO_NAME, DEMO_URL, now),
        )
        target_id = int(cursor.lastrowid)
        screenshot = _write_screenshot(target_id, "before", text)
        conn.execute(
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
                summary
            )
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                target_id,
                now,
                str(screenshot),
                text,
                hashlib.sha256(text.encode("utf-8")).hexdigest(),
                0.0,
                0.0,
                "",
                "[]",
                "Baseline capture created.",
            ),
        )
    return target_id


def seed_after() -> int:
    with db() as conn:
        target = conn.execute(
            "SELECT id FROM targets WHERE name=? ORDER BY id DESC LIMIT 1",
            (DEMO_NAME,),
        ).fetchone()
    target_id = int(target["id"]) if target else seed_before()

    with db() as conn:
        previous = conn.execute(
            "SELECT text_content FROM captures "
            "WHERE target_id=? ORDER BY id DESC LIMIT 1",
            (target_id,),
        ).fetchone()
    before = _fixture_text("before")
    after = _fixture_text("after")
    if previous is None or previous["text_content"] != before:
        target_id = seed_before()

    changes = extract_structured_changes(before, after)
    assessment = assess_change(before, after, visual_score=0.0)
    category_set = {
        *assessment.categories,
        *(change.category for change in changes),
    }
    categories = tuple(
        category for category in BUSINESS_CATEGORIES if category in category_set
    )
    category_text = ", ".join(categories)
    count = len(changes)
    noun = "change" if count == 1 else "changes"
    summary = (
        f"{count} meaningful business {noun} detected: {category_text}. "
        f"Text difference {assessment.text_score:.1f}%; visual difference 0.0%."
    )
    now = datetime.now(timezone.utc).isoformat()
    screenshot = _write_screenshot(target_id, "after", after)
    with db() as conn:
        conn.execute(
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
                summary
            )
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                target_id,
                now,
                str(screenshot),
                after,
                hashlib.sha256(after.encode("utf-8")).hexdigest(),
                0.0,
                assessment.text_score,
                category_text,
                serialize_structured_changes(changes),
                summary,
            ),
        )
    return target_id


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Seed the deterministic ChangeScout report demo."
    )
    parser.add_argument("state", choices=("before", "after"))
    args = parser.parse_args()

    init_db()
    target_id = seed_before() if args.state == "before" else seed_after()
    print(f"Demo state '{args.state}' is ready.")
    print(f"Open http://127.0.0.1:8000/target/{target_id}")


if __name__ == "__main__":
    main()
