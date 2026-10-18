from fastapi.testclient import TestClient
import pytest

import app as app_module
from display_time import format_display_time
from emailing import EmailContent, EmailDeliveryError, SMTPSettings


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.delenv("INQUIRY_NOTIFICATION_EMAIL", raising=False)
    monkeypatch.setattr(app_module, "DB", tmp_path / "landing.db")
    app_module.init_db()
    return TestClient(app_module.app)


def _configure_smtp(monkeypatch) -> None:
    monkeypatch.setenv("SMTP_HOST", "smtp.example.com")
    monkeypatch.setenv("SMTP_PORT", "587")
    monkeypatch.setenv("SMTP_FROM", "alerts@example.com")
    monkeypatch.setenv("SMTP_USE_TLS", "true")
    monkeypatch.delenv("SMTP_USERNAME", raising=False)
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)


def test_landing_page_explains_product_at_public_home(client) -> None:
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    body = response.text
    assert (
        "Know when competitors change prices, promotions, products, or messaging."
        in body
    )
    assert (
        "ChangeScout monitors competitor websites and alerts you when something "
        "that matters changes"
    ) in body
    for step in (
        "Add competitor pages",
        "ChangeScout scans them automatically",
        "Get meaningful alerts when something changes",
    ):
        assert step in body
    for watched_change in (
        "Price changes",
        "Promotions",
        "Shipping offers",
        "Product changes",
        "Calls to action",
        "Positioning and messaging",
    ):
        assert watched_change in body
    for example in (
        "$99",
        "$79",
        "Down $20 (20.2%)",
        "No promotion",
        "20% off this week",
        "Free shipping over $100",
        "Free shipping over $50",
        "Shop Now",
        "Start Shopping",
    ):
        assert example in body
    assert "Marketing example only." in body
    assert "Founding Customer" in body
    assert "$29" in body
    assert "Growth" in body
    assert "$59" in body
    assert body.count('href="#contact"') >= 5
    assert 'action="/landing/contact"' in body
    assert "/static/landing.css?v=" in body

    assert client.get("/landing").text == body
    assert "Start monitoring your first competitor." not in body


def test_landing_contact_form_stores_inquiry_and_confirms_submission(client) -> None:
    response = client.post(
        "/landing/contact",
        data={
            "name": "  Alex Morgan  ",
            "business_name": "  Northstar Goods  ",
            "email": "  alex@northstar.example  ",
            "website": "  https://northstar.example/shop  ",
            "message": "  We want to watch three pricing pages.  ",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?submitted=1#contact"
    with app_module.db() as conn:
        inquiry = conn.execute(
            """
            SELECT name, business_name, email, website, message, created_at
            FROM inquiries
            """
        ).fetchone()
    assert inquiry is not None
    assert inquiry["name"] == "Alex Morgan"
    assert inquiry["business_name"] == "Northstar Goods"
    assert inquiry["email"] == "alex@northstar.example"
    assert inquiry["website"] == "https://northstar.example/shop"
    assert inquiry["message"] == "We want to watch three pricing pages."
    assert inquiry["created_at"]

    confirmation = client.get("/?submitted=1")
    assert confirmation.status_code == 200
    assert "Thanks, your request is in." in confirmation.text
    assert "does not create an account or start billing" in confirmation.text


def test_landing_contact_form_sends_notification_after_storing(
    client,
    monkeypatch,
) -> None:
    _configure_smtp(monkeypatch)
    monkeypatch.setenv(
        "INQUIRY_NOTIFICATION_EMAIL",
        "operator@example.com",
    )
    sent: list[tuple[SMTPSettings, str, EmailContent]] = []

    def fake_sender(
        settings: SMTPSettings,
        recipient: str,
        content: EmailContent,
    ) -> None:
        with app_module.db() as conn:
            stored_count = conn.execute(
                "SELECT COUNT(*) FROM inquiries"
            ).fetchone()[0]
        assert stored_count == 1
        sent.append((settings, recipient, content))

    monkeypatch.setattr(app_module, "send_email", fake_sender)

    response = client.post(
        "/landing/contact",
        data={
            "name": "Alex Morgan",
            "business_name": "Northstar Goods",
            "email": "alex@northstar.example",
            "website": "https://northstar.example/shop",
            "message": "We want to watch three pricing pages.",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert response.headers["location"] == "/?submitted=1#contact"
    assert len(sent) == 1
    settings, recipient, content = sent[0]
    assert settings.configured
    assert recipient == "operator@example.com"
    assert content.subject == "ChangeScout: New inquiry from Northstar Goods"
    assert "Name: Alex Morgan" in content.body
    assert "Business name: Northstar Goods" in content.body
    assert "Email: alex@northstar.example" in content.body
    assert "Website: https://northstar.example/shop" in content.body
    assert "Message:\nWe want to watch three pricing pages." in content.body
    with app_module.db() as conn:
        submitted_at = conn.execute(
            "SELECT created_at FROM inquiries"
        ).fetchone()["created_at"]
    assert (
        f"Submission time: {format_display_time(submitted_at)}"
        in content.body
    )


def test_landing_contact_form_keeps_inquiry_when_notification_fails(
    client,
    monkeypatch,
    caplog,
) -> None:
    _configure_smtp(monkeypatch)
    monkeypatch.setenv("SMTP_USERNAME", "smtp-user")
    monkeypatch.setenv("SMTP_PASSWORD", "smtp-secret")
    monkeypatch.setenv(
        "INQUIRY_NOTIFICATION_EMAIL",
        "operator@example.com",
    )

    def failed_sender(
        settings: SMTPSettings,
        recipient: str,
        content: EmailContent,
    ) -> None:
        assert settings.configured
        assert recipient == "operator@example.com"
        assert content.subject == "ChangeScout: New inquiry from Harbor Supply"
        raise EmailDeliveryError("SMTP delivery failed (SMTPConnectError).")

    monkeypatch.setattr(app_module, "send_email", failed_sender)

    with caplog.at_level("WARNING", logger="changescout"):
        response = client.post(
            "/landing/contact",
            data={
                "name": "Sam Lee",
                "business_name": "Harbor Supply",
                "email": "sam@harbor.example",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/?submitted=1#contact"
    with app_module.db() as conn:
        inquiry = conn.execute(
            "SELECT name, business_name, email FROM inquiries"
        ).fetchone()
    assert tuple(inquiry) == (
        "Sam Lee",
        "Harbor Supply",
        "sam@harbor.example",
    )
    assert "Inquiry notification delivery failed for inquiry 1." in caplog.text
    assert "smtp-user" not in caplog.text
    assert "smtp-secret" not in caplog.text


def test_landing_contact_form_does_not_notify_without_recipient(
    client,
    monkeypatch,
) -> None:
    _configure_smtp(monkeypatch)
    attempts: list[tuple[SMTPSettings, str, EmailContent]] = []

    def fake_sender(
        settings: SMTPSettings,
        recipient: str,
        content: EmailContent,
    ) -> None:
        attempts.append((settings, recipient, content))

    monkeypatch.setattr(app_module, "send_email", fake_sender)

    response = client.post(
        "/landing/contact",
        data={
            "name": "Sam Lee",
            "business_name": "Harbor Supply",
            "email": "sam@harbor.example",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with app_module.db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM inquiries").fetchone()[0]
    assert count == 1
    assert attempts == []


def test_landing_contact_form_accepts_blank_optional_fields(client) -> None:
    response = client.post(
        "/landing/contact",
        data={
            "name": "Sam Lee",
            "business_name": "Harbor Supply",
            "email": "sam@harbor.example",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    with app_module.db() as conn:
        inquiry = conn.execute(
            "SELECT website, message FROM inquiries"
        ).fetchone()
    assert tuple(inquiry) == (None, None)


def test_landing_contact_form_rejects_invalid_input_without_storing_it(
    client,
) -> None:
    response = client.post(
        "/landing/contact",
        data={
            "name": " ",
            "business_name": "",
            "email": "not-an-email",
            "website": "competitor.example",
            "message": "x" * 2001,
        },
    )

    assert response.status_code == 400
    assert "Enter your name." in response.text
    assert "Enter your business name." in response.text
    assert "Enter a valid email address." in response.text
    assert "Enter a valid website address, including https://." in response.text
    assert "Message must be 2,000 characters or fewer." in response.text
    assert 'value="not-an-email"' in response.text
    with app_module.db() as conn:
        count = conn.execute("SELECT COUNT(*) FROM inquiries").fetchone()[0]
    assert count == 0


def test_inquiry_schema_is_additive_and_idempotent(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(app_module, "DB", tmp_path / "existing.db")
    app_module.init_db()
    with app_module.db() as conn:
        conn.execute(
            """
            INSERT INTO inquiries(
                name, business_name, email, website, message, created_at
            )
            VALUES(?,?,?,?,?,?)
            """,
            (
                "Existing lead",
                "Existing business",
                "owner@example.com",
                None,
                None,
                "2026-10-14T12:00:00+00:00",
            ),
        )

    app_module.init_db()

    with app_module.db() as conn:
        columns = {
            row["name"] for row in conn.execute("PRAGMA table_info(inquiries)")
        }
        count = conn.execute("SELECT COUNT(*) FROM inquiries").fetchone()[0]
    assert columns == {
        "id",
        "name",
        "business_name",
        "email",
        "website",
        "message",
        "created_at",
    }
    assert count == 1


def test_landing_stylesheet_is_served_with_mobile_layout_rules(client) -> None:
    response = client.get("/static/landing.css")

    assert response.status_code == 200
    assert "@media (max-width: 767.98px)" in response.text
    assert ".contact-layout" in response.text
    assert "overflow-x: hidden" not in response.text
