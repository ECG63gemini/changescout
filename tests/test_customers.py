import asyncio
from datetime import datetime, timezone
from pathlib import Path

import app as app_module
from monitoring import (
    StructuredChange,
    extract_visible_text,
    serialize_structured_changes,
)

DEMO = Path(__file__).parents[1] / "demo"
PRICE_CHANGE = StructuredChange(
    category="PRICE",
    old_value="$99",
    new_value="$79",
    description="Price decreased by $20 (20.2%).",
    importance=5,
)


def _use_test_database(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB", tmp_path / "customers.db")
    app_module._active_checks.clear()
    app_module.init_db()


def _configure_smtp(monkeypatch) -> None:
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM", "alerts@example.com")
    monkeypatch.setenv("SMTP_USE_TLS", "true")
    monkeypatch.delenv("SMTP_USERNAME", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)


def _insert_customer(
    *,
    name: str = "Acme Retail",
    email: str = "owner@example.com",
    active: bool = True,
) -> int:
    with app_module.db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO customers(name, email, created_at, active)
            VALUES(?,?,?,?)
            """,
            (
                name,
                email,
                "2026-01-01T00:00:00+00:00",
                int(active),
            ),
        )
    return int(cursor.lastrowid)


def _insert_target(
    *,
    customer_id: int | None = None,
    name: str = "Example Competitor",
    enabled: bool = True,
    alert_email: str | None = "owner@example.com",
    frequency: str = "daily",
    last_check_at: str | None = None,
) -> int:
    with app_module.db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO targets(
                customer_id,
                name,
                url,
                enabled,
                alert_email,
                check_frequency,
                last_check_at,
                created_at
            )
            VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                customer_id,
                name,
                "https://shop.example.com/products/trail-pack",
                int(enabled),
                alert_email,
                frequency,
                last_check_at,
                "2026-01-01T00:00:00+00:00",
            ),
        )
    return int(cursor.lastrowid)


def _insert_capture(
    target_id: int,
    *,
    text: str = "Baseline",
    created_at: str = "2026-01-01T00:00:00+00:00",
    changes: tuple[StructuredChange, ...] = (),
    alert_status: str = "not_applicable",
    alert_recipient: str | None = None,
    summary: str = "Baseline capture created.",
) -> int:
    with app_module.db() as conn:
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
                "/tmp/customer-test.png",
                text,
                "hash",
                0.0,
                0.0,
                ", ".join(change.category for change in changes),
                serialize_structured_changes(changes),
                alert_status,
                alert_recipient,
                summary,
            ),
        )
    return int(cursor.lastrowid)


def _demo_text(state: str) -> str:
    return extract_visible_text(
        (DEMO / f"store_{state}.html").read_text(encoding="utf-8")
    )


def test_operator_can_create_customer(tmp_path, monkeypatch) -> None:
    _use_test_database(tmp_path, monkeypatch)

    response = app_module.create_customer(
        name="  Acme Retail  ",
        email="  owner@example.com  ",
    )

    assert response.status_code == 303
    with app_module.db() as conn:
        customer = conn.execute("SELECT * FROM customers").fetchone()
    assert response.headers["location"] == f"/customer/{customer['id']}"
    assert customer["name"] == "Acme Retail"
    assert customer["email"] == "owner@example.com"
    assert customer["active"] == 1
    assert datetime.fromisoformat(customer["created_at"]).tzinfo is not None


def test_customer_monitor_is_assigned_and_defaults_email(
    tmp_path,
    monkeypatch,
) -> None:
    _use_test_database(tmp_path, monkeypatch)
    customer_id = _insert_customer(email="alerts@acme.example")

    async def validated_url(url: str) -> str:
        assert url == " https://competitor.example/pricing "
        return url.strip()

    monkeypatch.setattr(app_module, "_validate_public_url_async", validated_url)
    response = asyncio.run(
        app_module.create_customer_target(
            customer_id,
            name=" Competitor One ",
            url=" https://competitor.example/pricing ",
            enabled="yes",
            check_frequency="hourly",
        )
    )

    assert response.status_code == 303
    with app_module.db() as conn:
        target = conn.execute("SELECT * FROM targets").fetchone()
    assert target["customer_id"] == customer_id
    assert target["name"] == "Competitor One"
    assert target["alert_email"] == "alerts@acme.example"
    assert target["check_frequency"] == "hourly"
    assert target["enabled"] == 1


def test_assigned_target_without_override_uses_customer_email(
    tmp_path,
    monkeypatch,
) -> None:
    _use_test_database(tmp_path, monkeypatch)
    _configure_smtp(monkeypatch)
    customer_id = _insert_customer(email="customer@acme.example")
    target_id = _insert_target(
        customer_id=customer_id,
        alert_email=None,
    )
    _insert_capture(target_id, text=_demo_text("before"))
    sent_to: list[str] = []

    async def capture_demo(target_id: int, url: str):
        assert target_id
        assert url.startswith("https://")
        return tmp_path / "after.png", _demo_text("after")

    async def no_ai_summary(
        old_text: str,
        new_text: str,
        categories: tuple[str, ...],
    ):
        assert old_text
        assert new_text
        assert categories
        return None

    monkeypatch.setattr(app_module, "capture_page", capture_demo)
    monkeypatch.setattr(
        app_module,
        "visual_change_score",
        lambda _old, _new: 0.0,
    )
    monkeypatch.setattr(app_module, "ai_summary", no_ai_summary)

    result = asyncio.run(
        app_module.run_target_check(
            target_id,
            email_sender=lambda _settings, recipient, _content: sent_to.append(
                recipient
            ),
        )
    )

    assert result.alert_status == "sent"
    assert sent_to == ["customer@acme.example"]
    with app_module.db() as conn:
        capture = conn.execute(
            "SELECT alert_recipient FROM captures WHERE id=?",
            (result.capture_id,),
        ).fetchone()
    assert capture["alert_recipient"] == "customer@acme.example"


def test_disabled_customer_prevents_scheduled_checks_and_keeps_history(
    tmp_path,
    monkeypatch,
) -> None:
    _use_test_database(tmp_path, monkeypatch)
    customer_id = _insert_customer()
    target_id = _insert_target(customer_id=customer_id)
    _insert_capture(target_id)
    response = app_module.update_customer_status(customer_id, active="0")
    checked: list[int] = []

    async def fake_check(target_id: int) -> app_module.CheckResult:
        checked.append(target_id)
        return app_module.CheckResult(status="completed")

    attempted = asyncio.run(
        app_module.run_due_checks(
            now=datetime(2026, 1, 2, tzinfo=timezone.utc),
            check_runner=fake_check,
        )
    )

    assert response.status_code == 303
    assert attempted == ()
    assert checked == []
    with app_module.db() as conn:
        target_count = conn.execute(
            "SELECT COUNT(*) AS count FROM targets WHERE customer_id=?",
            (customer_id,),
        ).fetchone()["count"]
        capture_count = conn.execute(
            "SELECT COUNT(*) AS count FROM captures WHERE target_id=?",
            (target_id,),
        ).fetchone()["count"]
    assert target_count == 1
    assert capture_count == 1


def test_reenabled_customer_allows_scheduled_checks(
    tmp_path,
    monkeypatch,
) -> None:
    _use_test_database(tmp_path, monkeypatch)
    customer_id = _insert_customer(active=False)
    target_id = _insert_target(customer_id=customer_id)
    response = app_module.update_customer_status(customer_id, active="1")

    async def fake_check(_target_id: int) -> app_module.CheckResult:
        return app_module.CheckResult(status="completed")

    attempted = asyncio.run(
        app_module.run_due_checks(
            now=datetime(2026, 1, 2, tzinfo=timezone.utc),
            check_runner=fake_check,
        )
    )

    assert response.status_code == 303
    assert attempted == (target_id,)


def test_unassigned_target_remains_schedulable(
    tmp_path,
    monkeypatch,
) -> None:
    _use_test_database(tmp_path, monkeypatch)
    disabled_customer_id = _insert_customer(active=False)
    assigned_id = _insert_target(customer_id=disabled_customer_id)
    unassigned_id = _insert_target(customer_id=None)

    async def fake_check(_target_id: int) -> app_module.CheckResult:
        return app_module.CheckResult(status="completed")

    attempted = asyncio.run(
        app_module.run_due_checks(
            now=datetime(2026, 1, 2, tzinfo=timezone.utc),
            check_runner=fake_check,
        )
    )

    assert assigned_id not in attempted
    assert attempted == (unassigned_id,)


def test_disabled_customer_does_not_receive_pending_alert(
    tmp_path,
    monkeypatch,
) -> None:
    _use_test_database(tmp_path, monkeypatch)
    _configure_smtp(monkeypatch)
    customer_id = _insert_customer(active=False)
    target_id = _insert_target(
        customer_id=customer_id,
        alert_email="explicit@example.com",
    )
    capture_id = _insert_capture(
        target_id,
        changes=(PRICE_CHANGE,),
        alert_status="pending",
        alert_recipient="explicit@example.com",
    )
    deliveries: list[str] = []

    result = asyncio.run(
        app_module.deliver_capture_alert(
            capture_id,
            email_sender=lambda _settings, recipient, _content: deliveries.append(
                recipient
            ),
        )
    )

    assert result == "customer_disabled"
    assert deliveries == []
    with app_module.db() as conn:
        capture = conn.execute(
            "SELECT alert_status, alert_error FROM captures WHERE id=?",
            (capture_id,),
        ).fetchone()
    assert capture["alert_status"] == "customer_disabled"
    assert "customer is disabled" in capture["alert_error"]


def test_init_db_migrates_current_schema_without_changing_existing_targets(
    tmp_path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(app_module, "DB", tmp_path / "current-schema.db")
    with app_module.db() as conn:
        conn.execute(
            """
            CREATE TABLE targets(
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
            """
        )
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
                categories TEXT,
                structured_changes TEXT NOT NULL DEFAULT '[]',
                alert_status TEXT NOT NULL DEFAULT 'not_applicable',
                alert_recipient TEXT,
                alert_sent_at TEXT,
                alert_error TEXT,
                summary TEXT,
                FOREIGN KEY(target_id) REFERENCES targets(id)
            )
            """
        )
        cursor = conn.execute(
            """
            INSERT INTO targets(name, url, alert_email, created_at)
            VALUES(?,?,?,?)
            """,
            (
                "Legacy Monitor",
                "https://legacy.example.com",
                "legacy@example.com",
                "2026-01-01T00:00:00+00:00",
            ),
        )
        target_id = int(cursor.lastrowid)

    app_module.init_db()

    with app_module.db() as conn:
        customer_table = conn.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type='table' AND name='customers'
            """
        ).fetchone()
        target_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(targets)").fetchall()
        }
        foreign_keys = conn.execute(
            "PRAGMA foreign_key_list(targets)"
        ).fetchall()
        target = conn.execute(
            "SELECT * FROM targets WHERE id=?",
            (target_id,),
        ).fetchone()
    assert customer_table["name"] == "customers"
    assert "customer_id" in target_columns
    assert any(row["table"] == "customers" for row in foreign_keys)
    assert target["name"] == "Legacy Monitor"
    assert target["alert_email"] == "legacy@example.com"
    assert target["customer_id"] is None


def test_customer_list_and_detail_show_monitoring_activity(
    tmp_path,
    monkeypatch,
) -> None:
    _use_test_database(tmp_path, monkeypatch)
    monkeypatch.setenv("DISPLAY_TIMEZONE", "UTC")
    customer_id = _insert_customer(
        name="Acme <Retail>",
        email="owner@acme.example",
    )
    changed_target_id = _insert_target(
        customer_id=customer_id,
        name="Competitor <One>",
        last_check_at="2026-01-03T12:00:00+00:00",
    )
    _insert_target(
        customer_id=customer_id,
        name="Competitor Two",
        enabled=False,
        last_check_at="2026-01-02T12:00:00+00:00",
    )
    _insert_capture(
        changed_target_id,
        created_at="2026-01-03T12:00:00+00:00",
        changes=(PRICE_CHANGE,),
        summary="<b>Important price change</b>",
    )

    customer_list = app_module.customer_list().body.decode("utf-8")
    detail = app_module.customer_detail(customer_id).body.decode("utf-8")

    assert "Acme &lt;Retail&gt;" in customer_list
    assert "<td>2</td>" in customer_list
    assert "Jan 3, 2026 at 12:00 PM UTC" in customer_list
    assert "Acme <Retail>" not in customer_list
    assert "2 monitored competitors" in detail
    assert "Competitor &lt;One&gt;" in detail
    assert "Competitor Two" in detail
    assert "Enabled" in detail
    assert "Disabled" in detail
    assert "Latest meaningful change" in detail
    assert "&lt;b&gt;Important price change&lt;/b&gt;" in detail
    assert "<b>Important price change</b>" not in detail
