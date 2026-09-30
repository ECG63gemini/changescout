import asyncio
from pathlib import Path
from urllib.parse import unquote

import app as app_module
from monitoring import (
    StructuredChange,
    deserialize_structured_changes,
    extract_visible_text,
    serialize_structured_changes,
)

DEMO = Path(__file__).parents[1] / "demo"


def test_runtime_data_paths_default_to_repository_data(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.delenv("DATA_DIR", raising=False)
    base = tmp_path / "project"

    data, screenshots, database = app_module._runtime_data_paths(base)

    assert data == base / "data"
    assert screenshots == base / "data" / "screenshots"
    assert database == base / "data" / "changescout.db"
    assert data.is_dir()
    assert screenshots.is_dir()


def test_runtime_data_paths_respect_configured_directory(
    tmp_path,
    monkeypatch,
) -> None:
    volume = tmp_path / "railway-volume"
    monkeypatch.setenv("DATA_DIR", str(volume))

    data, screenshots, database = app_module._runtime_data_paths(
        tmp_path / "project"
    )

    assert data == volume
    assert screenshots == volume / "screenshots"
    assert database == volume / "changescout.db"
    assert data.is_dir()
    assert screenshots.is_dir()


def test_init_db_migrates_existing_capture_table(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB", tmp_path / "legacy.db")
    with app_module.db() as conn:
        conn.execute(
            """
            CREATE TABLE captures(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                target_id INTEGER NOT NULL,
                created_at TEXT NOT NULL,
                screenshot_path TEXT NOT NULL,
                text_content TEXT NOT NULL,
                text_hash TEXT NOT NULL,
                visual_score REAL,
                text_score REAL,
                summary TEXT
            )
            """
        )

    app_module.init_db()

    with app_module.db() as conn:
        capture_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(captures)").fetchall()
        }
        target_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(targets)").fetchall()
        }
    assert {"categories", "structured_changes", "alert_status"} <= capture_columns
    assert {
        "enabled",
        "alert_email",
        "check_frequency",
        "last_check_at",
        "last_check_error",
    } <= target_columns


def test_failed_capture_redirects_to_a_useful_escaped_message(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(app_module, "DB", tmp_path / "test.db")
    app_module.init_db()
    with app_module.db() as conn:
        cursor = conn.execute(
            "INSERT INTO targets(name,url,created_at) VALUES(?,?,?)",
            ("Test <Store>", "https://shop.example.com", "2026-01-01T00:00:00Z"),
        )
        target_id = cursor.lastrowid

    async def failed_capture(capture_target_id: int, capture_url: str):
        assert capture_target_id == target_id
        assert capture_url == "https://shop.example.com"
        raise app_module.CaptureError("The page timed out before it could be captured.")

    monkeypatch.setattr(app_module, "capture_page", failed_capture)
    response = asyncio.run(app_module.check(target_id))

    assert response.status_code == 303
    location = response.headers["location"]
    assert location.startswith(f"/target/{target_id}?error=")
    assert "timed out" in unquote(location)

    with app_module.db() as conn:
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
                "2026-01-01T01:00:00Z",
                "/tmp/not-read.png",
                "Changed",
                "hash",
                1.0,
                2.0,
                "OTHER",
                serialize_structured_changes(
                    (
                        StructuredChange(
                            category="OTHER",
                            old_value="<script>old()</script>",
                            new_value="<img src=x onerror=new()>",
                            description="<b>Unsafe impact</b>",
                            importance=1,
                        ),
                    )
                ),
                "<img src=x onerror=alert(1)>",
            ),
        )

    report = app_module.target_report(
        target_id,
        error="<script>alert('unsafe')</script>",
    )
    body = report.body.decode("utf-8")
    assert "Test &lt;Store&gt;" in body
    assert "&lt;script&gt;" in body
    assert "<script>alert('unsafe')</script>" not in body
    assert "&lt;img src=x onerror=alert(1)&gt;" in body
    assert "<img src=x onerror=alert(1)>" not in body
    assert "&lt;script&gt;old()&lt;/script&gt;" in body
    assert "<script>old()</script>" not in body
    assert "&lt;b&gt;Unsafe impact&lt;/b&gt;" in body


def test_check_persists_and_report_renders_structured_changes(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(app_module, "DB", tmp_path / "structured.db")
    app_module.init_db()
    before = extract_visible_text(
        (DEMO / "store_before.html").read_text(encoding="utf-8")
    )
    after = extract_visible_text(
        (DEMO / "store_after.html").read_text(encoding="utf-8")
    )
    with app_module.db() as conn:
        cursor = conn.execute(
            "INSERT INTO targets(name,url,created_at) VALUES(?,?,?)",
            ("Demo Store", "https://shop.example.com", "2026-01-01T00:00:00Z"),
        )
        target_id = int(cursor.lastrowid)
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
                summary
            )
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                target_id,
                "2026-01-01T00:00:00Z",
                str(tmp_path / "before.png"),
                before,
                "before-hash",
                0.0,
                0.0,
                "",
                "Baseline capture created.",
            ),
        )

    after_path = tmp_path / "after.png"

    async def capture_demo(capture_target_id: int, capture_url: str):
        assert capture_target_id == target_id
        assert capture_url == "https://shop.example.com"
        return after_path, after

    async def no_ai_summary(
        old_text: str,
        new_text: str,
        categories: tuple[str, ...],
    ):
        assert old_text == before
        assert new_text == after
        assert {"PRICE", "SHIPPING", "CTA", "PROMOTION"} <= set(categories)
        return None

    def no_visual_change(old_path: Path, new_path: Path) -> float:
        assert old_path == tmp_path / "before.png"
        assert new_path == after_path
        return 0.0

    monkeypatch.setattr(app_module, "capture_page", capture_demo)
    monkeypatch.setattr(app_module, "ai_summary", no_ai_summary)
    monkeypatch.setattr(app_module, "visual_change_score", no_visual_change)

    response = asyncio.run(app_module.check(target_id))

    assert response.status_code == 303
    with app_module.db() as conn:
        latest = conn.execute(
            "SELECT * FROM captures WHERE target_id=? ORDER BY id DESC LIMIT 1",
            (target_id,),
        ).fetchone()
    changes = deserialize_structured_changes(latest["structured_changes"])
    assert {"PRICE", "SHIPPING", "CTA", "PROMOTION"} <= {
        change.category for change in changes
    }

    report = app_module.target_report(target_id)
    body = report.body.decode("utf-8")
    assert "Latest business changes" in body
    assert "Price decreased by $20 (20.2%)." in body
    assert "Free-shipping threshold decreased by $50." in body
    assert "No visible promotion" in body
    assert "Start Shopping" in body
