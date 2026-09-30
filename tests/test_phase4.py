import asyncio
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

import app as app_module
from emailing import (
    EmailDeliveryError,
    SMTPSettings,
    render_change_alert,
)
from monitoring import (
    StructuredChange,
    extract_visible_text,
    serialize_structured_changes,
)
from scheduling import next_check_at, target_is_due

DEMO = Path(__file__).parents[1] / "demo"


def _use_test_database(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB", tmp_path / "phase4.db")
    app_module._active_checks.clear()
    app_module.init_db()


def _configure_smtp(monkeypatch) -> None:
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM", "alerts@example.com")
    monkeypatch.setenv("SMTP_USE_TLS", "true")
    monkeypatch.delenv("SMTP_USERNAME", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)


def _insert_target(
    *,
    enabled: bool = True,
    alert_email: str | None = "owner@example.com",
    frequency: str = "daily",
    last_check_at: str | None = None,
) -> int:
    with app_module.db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO targets(
                name,
                url,
                enabled,
                alert_email,
                check_frequency,
                last_check_at,
                created_at
            )
            VALUES(?,?,?,?,?,?,?)
            """,
            (
                "Example Competitor",
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
    text: str,
    *,
    structured_changes: str = "[]",
    alert_status: str = "not_applicable",
    alert_recipient: str | None = None,
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
                "2026-01-01T00:00:00+00:00",
                "/tmp/before.png",
                text,
                "hash",
                0.0,
                0.0,
                "",
                structured_changes,
                alert_status,
                alert_recipient,
                "Baseline capture created.",
            ),
        )
    return int(cursor.lastrowid)


def _demo_text(state: str) -> str:
    return extract_visible_text(
        (DEMO / f"store_{state}.html").read_text(encoding="utf-8")
    )


def test_frequency_due_logic() -> None:
    now = datetime(2026, 1, 2, 12, tzinfo=timezone.utc)

    assert target_is_due(
        enabled=True,
        frequency="hourly",
        last_checked_at=now - timedelta(hours=1),
        now=now,
    )
    assert not target_is_due(
        enabled=True,
        frequency="every_6_hours",
        last_checked_at=now - timedelta(hours=5, minutes=59),
        now=now,
    )
    assert target_is_due(
        enabled=True,
        frequency="daily",
        last_checked_at=now - timedelta(days=1),
        now=now,
    )
    assert target_is_due(
        enabled=True,
        frequency="daily",
        last_checked_at=None,
        now=now,
    )
    assert not target_is_due(
        enabled=False,
        frequency="hourly",
        last_checked_at=None,
        now=now,
    )
    assert next_check_at(now, "every_6_hours") == now + timedelta(hours=6)


def test_disabled_monitors_are_not_run(tmp_path, monkeypatch) -> None:
    _use_test_database(tmp_path, monkeypatch)
    disabled_id = _insert_target(enabled=False)
    enabled_id = _insert_target(enabled=True)
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

    assert disabled_id not in attempted
    assert attempted == (enabled_id,)
    assert checked == [enabled_id]


def test_operator_can_update_monitor_settings(tmp_path, monkeypatch) -> None:
    _use_test_database(tmp_path, monkeypatch)
    target_id = _insert_target(
        enabled=True,
        alert_email="old@example.com",
        frequency="daily",
    )

    response = app_module.update_target_settings(
        target_id,
        enabled=None,
        alert_email="new@example.com",
        check_frequency="hourly",
    )

    assert response.status_code == 303
    with app_module.db() as conn:
        target = conn.execute(
            "SELECT enabled, alert_email, check_frequency "
            "FROM targets WHERE id=?",
            (target_id,),
        ).fetchone()
    assert target["enabled"] == 0
    assert target["alert_email"] == "new@example.com"
    assert target["check_frequency"] == "hourly"


def test_scheduler_continues_after_one_target_crashes(
    tmp_path,
    monkeypatch,
) -> None:
    _use_test_database(tmp_path, monkeypatch)
    first_id = _insert_target()
    second_id = _insert_target()
    checked: list[int] = []

    async def fake_check(target_id: int) -> app_module.CheckResult:
        checked.append(target_id)
        if target_id == first_id:
            raise RuntimeError("simulated target failure")
        return app_module.CheckResult(status="completed")

    attempted = asyncio.run(
        app_module.run_due_checks(
            now=datetime(2026, 1, 2, tzinfo=timezone.utc),
            check_runner=fake_check,
        )
    )

    assert attempted == (first_id, second_id)
    assert checked == [first_id, second_id]


def test_scheduler_rereads_target_state_before_each_check(
    tmp_path,
    monkeypatch,
) -> None:
    _use_test_database(tmp_path, monkeypatch)
    first_id = _insert_target()
    second_id = _insert_target()
    checked: list[int] = []

    async def fake_check(target_id: int) -> app_module.CheckResult:
        checked.append(target_id)
        if target_id == first_id:
            with app_module.db() as conn:
                conn.execute(
                    "UPDATE targets SET enabled=0 WHERE id=?",
                    (second_id,),
                )
        return app_module.CheckResult(status="completed")

    attempted = asyncio.run(
        app_module.run_due_checks(
            now=datetime(2026, 1, 2, tzinfo=timezone.utc),
            check_runner=fake_check,
        )
    )

    assert attempted == (first_id,)
    assert checked == [first_id]


def test_meaningful_change_sends_one_alert(tmp_path, monkeypatch) -> None:
    _use_test_database(tmp_path, monkeypatch)
    _configure_smtp(monkeypatch)
    target_id = _insert_target()
    _insert_capture(target_id, _demo_text("before"))
    sent: list[tuple[SMTPSettings, str, str, str]] = []

    async def capture_demo(capture_target_id: int, capture_url: str):
        assert capture_target_id == target_id
        assert capture_url.startswith("https://")
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

    def fake_sender(
        settings: SMTPSettings,
        recipient: str,
        content,
    ) -> None:
        sent.append((settings, recipient, content.subject, content.body))

    monkeypatch.setattr(app_module, "capture_page", capture_demo)
    monkeypatch.setattr(app_module, "visual_change_score", lambda old, new: 0.0)
    monkeypatch.setattr(app_module, "ai_summary", no_ai_summary)

    result = asyncio.run(
        app_module.run_target_check(
            target_id,
            email_sender=fake_sender,
        )
    )

    assert result.completed
    assert result.alert_status == "sent"
    assert len(sent) == 1
    assert sent[0][1] == "owner@example.com"
    assert "important changes at Example Competitor" in sent[0][2]
    assert "$99 -> $79" in sent[0][3]
    with app_module.db() as conn:
        capture = conn.execute(
            "SELECT alert_status, alert_sent_at, alert_error "
            "FROM captures WHERE id=?",
            (result.capture_id,),
        ).fetchone()
    assert capture["alert_status"] == "sent"
    assert capture["alert_sent_at"]
    assert capture["alert_error"] is None


def test_no_change_capture_does_not_send_alert(tmp_path, monkeypatch) -> None:
    _use_test_database(tmp_path, monkeypatch)
    _configure_smtp(monkeypatch)
    target_id = _insert_target()
    unchanged = _demo_text("before")
    _insert_capture(target_id, unchanged)
    sent: list[str] = []

    async def capture_unchanged(target_id: int, url: str):
        assert target_id
        assert url
        return tmp_path / "unchanged.png", unchanged

    monkeypatch.setattr(app_module, "capture_page", capture_unchanged)
    monkeypatch.setattr(app_module, "visual_change_score", lambda old, new: 0.0)

    result = asyncio.run(
        app_module.run_target_check(
            target_id,
            email_sender=lambda settings, recipient, content: sent.append(
                content.subject
            ),
        )
    )

    assert result.completed
    assert result.alert_status == "not_applicable"
    assert sent == []


def test_baseline_capture_does_not_send_alert(tmp_path, monkeypatch) -> None:
    _use_test_database(tmp_path, monkeypatch)
    _configure_smtp(monkeypatch)
    target_id = _insert_target()
    sent: list[str] = []

    async def capture_baseline(target_id: int, url: str):
        assert target_id
        assert url
        return tmp_path / "baseline.png", _demo_text("before")

    monkeypatch.setattr(app_module, "capture_page", capture_baseline)

    result = asyncio.run(
        app_module.run_target_check(
            target_id,
            email_sender=lambda settings, recipient, content: sent.append(
                content.subject
            ),
        )
    )

    assert result.completed
    assert result.alert_status == "not_applicable"
    assert sent == []


def test_email_alert_is_deduplicated_per_capture(tmp_path, monkeypatch) -> None:
    _use_test_database(tmp_path, monkeypatch)
    _configure_smtp(monkeypatch)
    target_id = _insert_target()
    changes = (
        StructuredChange(
            category="PRICE",
            old_value="$99",
            new_value="$79",
            description="Price decreased by $20 (20.2%).",
            importance=5,
        ),
    )
    capture_id = _insert_capture(
        target_id,
        _demo_text("after"),
        structured_changes=serialize_structured_changes(changes),
        alert_status="pending",
        alert_recipient="owner@example.com",
    )
    deliveries: list[str] = []

    def fake_sender(
        settings: SMTPSettings,
        recipient: str,
        content,
    ) -> None:
        deliveries.append(content.subject)

    first = asyncio.run(
        app_module.deliver_capture_alert(
            capture_id,
            email_sender=fake_sender,
        )
    )
    second = asyncio.run(
        app_module.deliver_capture_alert(
            capture_id,
            email_sender=fake_sender,
        )
    )

    assert first == "sent"
    assert second == "sent"
    assert len(deliveries) == 1


def test_email_failure_is_recorded_without_marking_sent(
    tmp_path,
    monkeypatch,
) -> None:
    _use_test_database(tmp_path, monkeypatch)
    _configure_smtp(monkeypatch)
    target_id = _insert_target()
    changes = (
        StructuredChange(
            category="CTA",
            old_value="Shop Now",
            new_value="Start Shopping",
            description="Call to action changed.",
            importance=3,
        ),
    )
    capture_id = _insert_capture(
        target_id,
        _demo_text("after"),
        structured_changes=serialize_structured_changes(changes),
        alert_status="pending",
        alert_recipient="owner@example.com",
    )

    def failed_sender(settings, recipient, content) -> None:
        raise EmailDeliveryError("SMTP delivery failed (SMTPConnectError).")

    result = asyncio.run(
        app_module.deliver_capture_alert(
            capture_id,
            email_sender=failed_sender,
        )
    )

    assert result == "failed"
    with app_module.db() as conn:
        capture = conn.execute(
            "SELECT alert_status, alert_sent_at, alert_error "
            "FROM captures WHERE id=?",
            (capture_id,),
        ).fetchone()
    assert capture["alert_status"] == "failed"
    assert capture["alert_sent_at"] is None
    assert "SMTP delivery failed" in capture["alert_error"]


def test_unconfigured_email_is_recorded_without_transport_call(
    tmp_path,
    monkeypatch,
) -> None:
    _use_test_database(tmp_path, monkeypatch)
    for name in (
        "SMTP_HOST",
        "SMTP_PORT",
        "SMTP_USERNAME",
        "SMTP_PASSWORD",
        "SMTP_FROM",
        "SMTP_USE_TLS",
    ):
        monkeypatch.delenv(name, raising=False)
    target_id = _insert_target()
    changes = (
        StructuredChange(
            category="SHIPPING",
            old_value="Free shipping over $100",
            new_value="Free shipping over $50",
            description="Free-shipping threshold decreased by $50.",
            importance=5,
        ),
    )
    capture_id = _insert_capture(
        target_id,
        _demo_text("after"),
        structured_changes=serialize_structured_changes(changes),
        alert_status="pending",
        alert_recipient="owner@example.com",
    )
    transport_calls: list[str] = []

    result = asyncio.run(
        app_module.deliver_capture_alert(
            capture_id,
            email_sender=lambda settings, recipient, content: transport_calls.append(
                recipient
            ),
        )
    )

    assert result == "not_configured"
    assert transport_calls == []
    with app_module.db() as conn:
        capture = conn.execute(
            "SELECT alert_status, alert_sent_at, alert_error "
            "FROM captures WHERE id=?",
            (capture_id,),
        ).fetchone()
    assert capture["alert_status"] == "not_configured"
    assert capture["alert_sent_at"] is None
    assert "SMTP_HOST and SMTP_FROM are required" in capture["alert_error"]


def test_authenticated_smtp_requires_tls() -> None:
    settings = SMTPSettings.from_environment(
        {
            "SMTP_HOST": "smtp.example.com",
            "SMTP_PORT": "25",
            "SMTP_FROM": "alerts@example.com",
            "SMTP_USERNAME": "smtp-user",
            "SMTP_PASSWORD": "smtp-password",
            "SMTP_USE_TLS": "false",
        }
    )

    assert not settings.configured
    assert settings.configuration_error == (
        "Authenticated SMTP requires SMTP_USE_TLS=true."
    )


def test_overlapping_checks_for_same_target_are_blocked(
    tmp_path,
    monkeypatch,
) -> None:
    _use_test_database(tmp_path, monkeypatch)
    target_id = _insert_target(alert_email=None)

    async def scenario() -> tuple[app_module.CheckResult, app_module.CheckResult]:
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_capture(capture_target_id: int, capture_url: str):
            assert capture_target_id == target_id
            assert capture_url
            started.set()
            await release.wait()
            return tmp_path / "baseline.png", _demo_text("before")

        monkeypatch.setattr(app_module, "capture_page", slow_capture)
        first_task = asyncio.create_task(app_module.run_target_check(target_id))
        await started.wait()
        second = await app_module.run_target_check(target_id)
        release.set()
        first = await first_task
        return first, second

    first, second = asyncio.run(scenario())

    assert first.completed
    assert second.status == "already_running"
    with app_module.db() as conn:
        count = conn.execute(
            "SELECT COUNT(*) AS count FROM captures WHERE target_id=?",
            (target_id,),
        ).fetchone()["count"]
    assert count == 1


def test_shutdown_waits_for_claimed_email_delivery(
    tmp_path,
    monkeypatch,
) -> None:
    _use_test_database(tmp_path, monkeypatch)
    _configure_smtp(monkeypatch)
    target_id = _insert_target()
    changes = (
        StructuredChange(
            category="PRICE",
            old_value="$99",
            new_value="$79",
            description="Price decreased by $20 (20.2%).",
            importance=5,
        ),
    )
    capture_id = _insert_capture(
        target_id,
        _demo_text("after"),
        structured_changes=serialize_structured_changes(changes),
        alert_status="pending",
        alert_recipient="owner@example.com",
    )
    started = threading.Event()
    release = threading.Event()

    def slow_sender(settings, recipient, content) -> None:
        started.set()
        release.wait(timeout=2)

    async def scenario() -> None:
        delivery = asyncio.create_task(
            app_module.deliver_capture_alert(
                capture_id,
                email_sender=slow_sender,
            )
        )
        assert await asyncio.to_thread(started.wait, 2)
        delivery.cancel()
        release.set()
        try:
            await delivery
        except asyncio.CancelledError:
            pass

    asyncio.run(scenario())

    with app_module.db() as conn:
        capture = conn.execute(
            "SELECT alert_status, alert_sent_at FROM captures WHERE id=?",
            (capture_id,),
        ).fetchone()
    assert capture["alert_status"] == "sent"
    assert capture["alert_sent_at"]


def test_startup_marks_interrupted_delivery_as_indeterminate(
    tmp_path,
    monkeypatch,
) -> None:
    _use_test_database(tmp_path, monkeypatch)
    target_id = _insert_target()
    capture_id = _insert_capture(
        target_id,
        _demo_text("after"),
        alert_status="sending",
        alert_recipient="owner@example.com",
    )

    app_module.init_db()

    with app_module.db() as conn:
        capture = conn.execute(
            "SELECT alert_status, alert_sent_at, alert_error "
            "FROM captures WHERE id=?",
            (capture_id,),
        ).fetchone()
    assert capture["alert_status"] == "indeterminate"
    assert capture["alert_sent_at"] is None
    assert "whether email was sent is unknown" in capture["alert_error"]


def test_email_body_rendering() -> None:
    content = render_change_alert(
        competitor_name="Example Competitor",
        page_url="https://shop.example.com/product",
        detected_at="2026-01-02T12:00:00+00:00",
        changes=(
            StructuredChange(
                category="PRICE",
                old_value="$99",
                new_value="$79",
                description="Price decreased by $20 (20.2%).",
                importance=5,
            ),
            StructuredChange(
                category="PROMOTION",
                old_value="No visible promotion",
                new_value="20% off this week",
                description="Promotion added.",
                importance=5,
            ),
        ),
        report_url="https://changescout.example/target/42",
    )

    assert content.subject == (
        "ChangeScout: 2 important changes at Example Competitor"
    )
    assert "Competitor: Example Competitor" in content.body
    assert "Page URL: https://shop.example.com/product" in content.body
    assert "Detected: 2026-01-02T12:00:00+00:00" in content.body
    assert "PRICE\n$99 -> $79\nPrice decreased by $20 (20.2%)." in content.body
    assert "PROMOTION\nNo visible promotion -> 20% off this week" in content.body
    assert "https://changescout.example/target/42" in content.body
