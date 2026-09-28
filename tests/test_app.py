import asyncio
from urllib.parse import unquote

import app as app_module


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
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(captures)").fetchall()
        }
    assert "categories" in columns


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
                summary
            )
            VALUES(?,?,?,?,?,?,?,?,?)
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
