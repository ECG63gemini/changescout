import asyncio
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import unquote

import pytest
from fastapi.testclient import TestClient

import app as app_module
from display_time import display_timezone_name
from monitoring import StructuredChange, serialize_structured_changes
from ui import (
    BUSY_SCRIPT,
    STATIC_DIR,
    friendly_error,
    monitor_status,
    page_link,
    split_url,
)

PRICE_CHANGE = StructuredChange(
    category="PRICE",
    old_value="$99",
    new_value="$79",
    description="Price decreased by $20 (20.2%).",
    importance=5,
)


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DB", tmp_path / "ui.db")
    monkeypatch.setenv("DISPLAY_TIMEZONE", "UTC")
    for name in ("SMTP_HOST", "SMTP_FROM", "SMTP_USERNAME", "SMTP_PASSWORD"):
        monkeypatch.delenv(name, raising=False)
    app_module._active_checks.clear()
    app_module.init_db()


@pytest.fixture
def client(isolated_db, monkeypatch):
    # Without a context manager the lifespan (and its scheduler) never starts.
    monkeypatch.setenv("ADMIN_USERNAME", "test-operator")
    monkeypatch.setenv("ADMIN_PASSWORD", "test-only-password")
    client = TestClient(app_module.app)
    client.auth = ("test-operator", "test-only-password")
    return client


def _customer(name="Acme Retail", email="owner@acme.example", active=True) -> int:
    with app_module.db() as conn:
        return int(
            conn.execute(
                "INSERT INTO customers(name,email,created_at,active) VALUES(?,?,?,?)",
                (name, email, "2026-01-01T00:00:00+00:00", int(active)),
            ).lastrowid
        )


def _target(
    *,
    customer_id=None,
    name="Northstar",
    url="https://www.northstar.example.com/products/hoodie/",
    enabled=True,
    alert_email=None,
    last_check_at=None,
    last_check_error=None,
) -> int:
    with app_module.db() as conn:
        return int(
            conn.execute(
                """
                INSERT INTO targets(customer_id,name,url,enabled,alert_email,
                    check_frequency,last_check_at,last_check_error,created_at)
                VALUES(?,?,?,?,?,?,?,?,?)
                """,
                (
                    customer_id,
                    name,
                    url,
                    int(enabled),
                    alert_email,
                    "daily",
                    last_check_at,
                    last_check_error,
                    "2026-01-01T00:00:00+00:00",
                ),
            ).lastrowid
        )


def _capture(
    target_id: int,
    created_at: str,
    *,
    changes=(),
    categories=None,
    alert_status="not_applicable",
    alert_recipient=None,
    visual_score=0.0,
    text_score=0.0,
    summary="No meaningful change detected.",
) -> int:
    with app_module.db() as conn:
        return int(
            conn.execute(
                """
                INSERT INTO captures(target_id,created_at,screenshot_path,
                    text_content,text_hash,visual_score,text_score,categories,
                    structured_changes,alert_status,alert_recipient,summary)
                VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    target_id,
                    created_at,
                    "/tmp/ui-test.png",
                    "text",
                    "hash",
                    visual_score,
                    text_score,
                    (
                        categories
                        if categories is not None
                        else ", ".join(change.category for change in changes)
                    ),
                    serialize_structured_changes(changes),
                    alert_status,
                    alert_recipient,
                    summary,
                ),
            ).lastrowid
        )


def _primary_content(body: str) -> str:
    """Page markup outside scripts and expandable disclosures (Technical details)."""
    body = re.sub(r"<script\b.*?</script>", "", body, flags=re.S)
    return re.sub(r"<details\b.*?</details>", "", body, flags=re.S)


def test_url_presentation_hides_scheme_and_keeps_full_href() -> None:
    assert split_url("https://www.shop.example.com/products/pricing/") == (
        "shop.example.com",
        "/products/pricing",
    )
    assert split_url("https://example.com/") == ("example.com", "")
    assert split_url("http://shop.example.com:8080/a?b=1") == (
        "shop.example.com:8080",
        "/a?b=1",
    )
    link = page_link("https://www.shop.example.com/sale/?a=<b>")
    assert 'href="https://www.shop.example.com/sale/?a=&lt;b&gt;"' in link
    assert 'rel="noopener noreferrer"' in link
    assert 'target="_blank"' in link
    visible = re.sub(r"<[^>]+>", "", link)
    assert visible.startswith("shop.example.com/sale?a=&lt;b&gt;")
    assert "https://" not in visible
    assert "<wbr>" in link


def test_monitor_health_is_separate_from_scan_result() -> None:
    def status(**overrides):
        values = {
            "enabled": True,
            "customer_paused": False,
            "has_baseline": True,
            "scan_error": None,
            "alert_problem": False,
            "frequency": "daily",
        }
        values.update(overrides)
        return monitor_status(**values).label

    assert status() == "Watching"
    assert status(has_baseline=False) == "Setup needed"
    assert status(scan_error="HTTP 500") == "Needs attention"
    assert status(alert_problem=True) == "Needs attention"
    assert status(customer_paused=True, scan_error="HTTP 500") == "Paused by customer"
    assert status(enabled=False, customer_paused=True) == "Paused"


def test_friendly_error_translates_known_scan_failures() -> None:
    assert friendly_error(
        "The website returned HTTP 404; no capture was saved."
    ) == "The website returned an error (HTTP 404, page not found)."
    assert "already running" in friendly_error(
        "A check is already running for this target."
    )
    assert friendly_error("The hostname could not be resolved.") == (
        "We couldn't find or reach this website address. Check the URL and try again."
    )
    browser = friendly_error(
        "The browser could not capture this page after two attempts. "
        "The site may be unavailable, too slow, or blocking automated browsers."
    )
    assert browser.startswith("We couldn't load this page.")
    assert "Try again later" in browser
    for raw in (
        "The hostname could not be resolved.",
        "The hostname did not resolve to an IP address.",
        "Hostname validation timed out. Please try again.",
        "Internal hostnames are not supported.",
        "Local, private, loopback, link-local, and reserved addresses are not supported.",
        "The browser could not capture this page after two attempts. "
        "The site may be unavailable, too slow, or blocking automated browsers.",
    ):
        friendly = friendly_error(raw).lower()
        for jargon in ("hostname", "resolve", "automated browser", "loopback", "dns"):
            assert jargon not in friendly, (raw, jargon)
    assert friendly_error("Something unexpected.") == "Something unexpected."


def test_empty_overview_invites_first_customer(client) -> None:
    body = client.get("/app").text

    assert "Start monitoring your first competitor." in body
    assert "Create first customer" in body
    assert 'href="/app/customers/new"' in body
    assert "No monitors yet." not in body


def test_empty_lists_have_useful_empty_states(client) -> None:
    assert "No customers yet." in client.get("/app/customers").text
    customer_id = _customer()
    detail = client.get(f"/app/customer/{customer_id}").text
    assert "No competitor pages are being monitored for this customer yet." in detail


def test_report_keeps_latest_business_changes_after_no_change_scan(
    client,
) -> None:
    customer_id = _customer()
    target_id = _target(
        customer_id=customer_id,
        last_check_at="2026-01-03T00:00:00+00:00",
    )
    _capture(target_id, "2026-01-01T00:00:00+00:00", summary="Baseline capture created.")
    _capture(
        target_id,
        "2026-01-02T00:00:00+00:00",
        changes=(PRICE_CHANGE,),
        alert_status="sent",
        alert_recipient="owner@acme.example",
        visual_score=12.3,
        text_score=45.6,
    )
    _capture(target_id, "2026-01-03T00:00:00+00:00")

    body = client.get(f"/app/target/{target_id}").text

    latest_scan = body.index("No meaningful business changes detected.")
    business = body.index("Latest business changes")
    history = body.index('id="history-title"')
    settings = body.index('id="settings-title"')
    assert latest_scan < business < history < settings
    assert "Price decreased by $20 (20.2%)." in body
    assert "1 newer scan since then." in body
    assert "Monitoring started" in body
    # Difference scores stay available but only inside Technical details.
    technical = body.index("Technical details")
    assert body.index("12.3%") > technical
    assert body.index("45.6%") > technical
    primary = _primary_content(body).lower()
    assert "structured" not in primary
    assert "capture" not in primary
    assert "target" not in re.sub(r"<[^>]+>", " ", primary)


def test_new_monitor_report_shows_first_scan_guidance(client) -> None:
    target_id = _target()

    body = client.get(f"/app/target/{target_id}").text

    assert "Not checked yet" in body
    assert "Setup needed" in body
    assert 'action="/app/check/' in body
    assert "Scan now" in body


def test_paused_customer_and_monitor_states_are_labelled(client) -> None:
    paused_customer = _customer(name="Harbor Home", active=False)
    customer_target = _target(customer_id=paused_customer, name="Coastal")
    _capture(customer_target, "2026-01-01T00:00:00+00:00")
    paused_target = _target(name="Disabled Rival", enabled=False)
    _capture(paused_target, "2026-01-01T00:00:00+00:00")

    customer = client.get(f"/app/customer/{paused_customer}").text
    report = client.get(f"/app/target/{customer_target}").text
    disabled = client.get(f"/app/target/{paused_target}").text

    assert "This customer is paused" in customer
    assert "Re-enable customer" in customer
    assert "Paused by customer" in report
    assert "Harbor Home is paused" in report
    assert ">Paused<" in disabled
    assert "Automatic scans are off for this page." in disabled


def test_overview_lists_attention_and_recent_changes(client) -> None:
    now = datetime.now(timezone.utc)
    customer_id = _customer()
    failing = _target(
        customer_id=customer_id,
        name="Failing Rival",
        last_check_at=now.isoformat(),
        last_check_error="The website returned HTTP 503; no capture was saved.",
    )
    _capture(failing, (now - timedelta(days=1)).isoformat())
    changed = _target(customer_id=customer_id, name="Changed Rival")
    _capture(changed, (now - timedelta(days=2)).isoformat())
    _capture(
        changed,
        (now - timedelta(hours=1)).isoformat(),
        changes=(PRICE_CHANGE,),
        alert_status="sent",
        alert_recipient="owner@acme.example",
    )

    body = client.get("/app").text

    snapshot = body.index('id="snapshot-title"')
    changes = body.index('id="recent-changes-title"')
    attention = body.index('id="attention-title"')
    monitoring = body.index('id="monitoring-title"')
    assert snapshot < changes < attention < monitoring
    assert "Scan failed: Failing Rival" in body
    assert "HTTP 503, server error" in body
    assert "Retry scan" in body
    assert "Email alerts are unavailable" in body
    assert "$99" in body and "$79" in body
    assert "Price decreased by $20 (20.2%)." in body
    assert ">Sent<" in body


def test_invalid_customer_form_renders_inline_errors(client) -> None:
    response = client.post("/app/customers", data={"name": " ", "email": "nope"})

    assert response.status_code == 400
    assert response.headers["content-type"].startswith("text/html")
    assert "Enter the customer&#x27;s name." in response.text
    assert "Enter a valid alert email address." in response.text
    assert 'value="nope"' in response.text
    with app_module.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 0


def test_invalid_competitor_url_renders_inline_error(client) -> None:
    customer_id = _customer()

    response = client.post(
        f"/app/customer/{customer_id}/targets",
        data={
            "name": "Rival",
            "url": "http://localhost/pricing",
            "check_frequency": "daily",
            "enabled": "yes",
        },
    )

    assert response.status_code == 400
    assert "Internal hostnames are not supported." not in response.text
    assert "private or internal network" in response.text
    assert "Use the address of a public website." in response.text
    assert 'value="http://localhost/pricing"' in response.text
    with app_module.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM targets").fetchone()[0] == 0


def test_invalid_settings_are_not_saved_and_explain_why(client) -> None:
    target_id = _target(alert_email="old@example.com")

    response = client.post(
        f"/app/target/{target_id}/settings",
        data={"alert_email": "broken@", "check_frequency": "hourly"},
    )

    assert response.status_code == 400
    assert "Settings not saved" in response.text
    assert "Enter a valid alert email address." in response.text
    with app_module.db() as conn:
        target = conn.execute(
            "SELECT alert_email, check_frequency, enabled FROM targets WHERE id=?",
            (target_id,),
        ).fetchone()
    assert tuple(target) == ("old@example.com", "daily", 1)


def test_missing_resources_render_html_not_json(client) -> None:
    for path in ("/app/customer/999", "/app/target/999", "/app/customer/abc", "/missing"):
        response = client.get(path)
        assert response.status_code == 404
        assert response.headers["content-type"].startswith("text/html")
        assert "Page not found" in response.text
        assert '"detail"' not in response.text

    incomplete = client.post("/app/customers", data={"name": "Only a name"})
    assert incomplete.status_code == 422
    assert "Some information is missing" in incomplete.text


def test_unassigned_monitor_creation_redirects_to_monitors(
    client,
    monkeypatch,
) -> None:
    async def validated(url: str) -> str:
        return url.strip()

    monkeypatch.setattr(app_module, "_validate_public_url_async", validated)

    response = client.post(
        "/app/targets",
        data={
            "name": "Benchmark",
            "url": "https://benchmark.example.com",
            "alert_email": "",
            "check_frequency": "daily",
            "enabled": "yes",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert unquote(response.headers["location"]) == "/app/monitors?notice=Monitor added."
    assert "Benchmark" in client.get("/app/monitors").text


def test_layout_has_navigation_breadcrumbs_and_stylesheet(client) -> None:
    customer_id = _customer(name="Acme <Retail>")
    target_id = _target(customer_id=customer_id, name="Rival <One>")

    body = client.get(f"/app/target/{target_id}").text

    assert 'aria-label="Primary"' in body
    for label in ("Overview", "Customers", "Monitors"):
        assert f">{label}</a>" in body
    assert 'aria-label="Breadcrumb"' in body
    assert "Acme &lt;Retail&gt;" in body
    assert "Rival &lt;One&gt;" in body
    assert "<Retail>" not in body
    assert '/static/app.css?v=' in body
    stylesheet = client.get("/static/app.css")
    assert stylesheet.status_code == 200
    assert "overflow-x: hidden" not in stylesheet.text
    assert "break-all" not in stylesheet.text


def test_check_route_redirect_shows_failure_once(client, monkeypatch) -> None:
    target_id = _target()

    async def unreachable(target_id: int, url: str):
        raise app_module.URLValidationError("The hostname could not be resolved.")

    monkeypatch.setattr(app_module, "capture_page", unreachable)

    response = asyncio.run(app_module.check(target_id))
    page = client.get(response.headers["location"]).text

    assert page.count("The hostname could not be resolved.") == 1
    assert "Latest scan failed." in page
    # Plain language in the page; the original error only under Technical details.
    primary = _primary_content(page)
    assert "We couldn&#x27;t find or reach this website address." in primary
    assert "hostname" not in primary.lower()
    technical = page.index("Technical details")
    assert page.index("The hostname could not be resolved.") > technical


def test_failed_alert_stays_visible_after_newer_change_without_values(
    client,
) -> None:
    now = datetime.now(timezone.utc)
    customer_id = _customer()
    target_id = _target(
        customer_id=customer_id,
        name="Rival",
        last_check_at=now.isoformat(),
    )
    _capture(target_id, (now - timedelta(days=2)).isoformat())
    _capture(
        target_id,
        (now - timedelta(days=1)).isoformat(),
        changes=(PRICE_CHANGE,),
        alert_status="failed",
        alert_recipient="owner@acme.example",
    )
    # A visual-only change is meaningful but has no extracted values, so
    # run_target_check stores it without attempting an alert.
    _capture(
        target_id,
        (now - timedelta(hours=1)).isoformat(),
        categories="OTHER",
        alert_status="not_applicable",
    )

    overview = client.get("/app").text
    report = client.get(f"/app/target/{target_id}").text

    assert "Email alert failed: Rival" in overview
    assert "Needs attention" in report.split('id="latest-scan-title"')[0]


def _section_html(body: str, section_id: str) -> str:
    match = re.search(
        rf'<section\b[^>]*(?:id|aria-labelledby)="{re.escape(section_id)}"', body
    )
    assert match is not None, section_id
    return body[match.start() : body.index("</section>", match.start())]


def _history_rows(body: str) -> list[str]:
    history = _section_html(body, "history")
    table = history[: history.index("Technical details")]
    return re.findall(r"<tr>.*?</tr>", table, flags=re.S)[1:]


def _relative_luminance(color: str) -> float:
    channels = [int(color.lstrip("#")[i : i + 2], 16) / 255 for i in (0, 2, 4)]
    linear = [
        c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def _contrast(first: str, second: str) -> float:
    high, low = sorted(
        (_relative_luminance(first), _relative_luminance(second)), reverse=True
    )
    return (high + 0.05) / (low + 0.05)


def test_overview_previews_three_attention_items_and_skips_paused_customers(
    client,
) -> None:
    now = datetime.now(timezone.utc)
    customer_id = _customer()
    failing = []
    for index in range(5):
        monitor_id = _target(
            customer_id=customer_id,
            name=f"Failing {index}",
            last_check_at=now.isoformat(),
            last_check_error="The website returned HTTP 503; no capture was saved.",
        )
        _capture(monitor_id, (now - timedelta(days=1)).isoformat())
        failing.append(monitor_id)
    paused_customer = _customer(name="Harbor Home", active=False)
    _target(
        customer_id=paused_customer,
        name="Paused Rival",
        last_check_at=now.isoformat(),
        last_check_error="The hostname could not be resolved.",
    )

    body = client.get("/app").text
    attention = _section_html(body, "attention")

    # Email setup (unset in tests) plus five failed scans: three are previewed.
    assert attention.count('class="attention-item ') == 3
    assert "3 more need attention." in attention
    assert '<a href="/app/monitors">Review in Monitors</a>' in attention
    assert "not shown" not in attention
    assert "Customer paused" not in body
    assert "Paused Rival" not in attention
    assert "Harbor Home" not in attention

    with app_module.db() as conn:
        conn.execute(
            "UPDATE targets SET last_check_error=NULL WHERE id IN (?, ?)",
            tuple(failing[:2]),
        )
    attention = _section_html(client.get("/app").text, "attention")
    assert "1 more needs attention." in attention


def test_change_without_specific_values_reads_consistently(client) -> None:
    target_id = _target(last_check_at="2026-01-02T00:00:00+00:00")
    _capture(target_id, "2026-01-01T00:00:00+00:00", summary="Baseline capture created.")
    _capture(target_id, "2026-01-02T00:00:00+00:00", categories="CTA")

    body = client.get(f"/app/target/{target_id}").text
    latest = _section_html(body, "latest-scan-title")

    assert (
        "The page changed, but no specific old-to-new business change was identified."
        in latest
    )
    assert "No specific business changes identified yet." in body
    assert "A page change was detected" not in body
    assert "No meaningful business changes yet" not in body
    rows = _history_rows(body)
    assert "Change detected" in rows[0] and ">Email not sent<" in rows[0]
    assert "Monitoring started" in rows[1] and ">No alert needed<" in rows[1]
    assert "No alert required" not in body


def test_business_changes_without_alert_attempt_say_email_not_sent(client) -> None:
    now = datetime.now(timezone.utc)
    target_id = _target(name="Rival", last_check_at=now.isoformat())
    _capture(target_id, (now - timedelta(days=2)).isoformat())
    _capture(
        target_id,
        (now - timedelta(days=1)).isoformat(),
        changes=(PRICE_CHANGE,),
        alert_status="not_applicable",
    )
    _capture(target_id, now.isoformat())

    report = client.get(f"/app/target/{target_id}").text
    overview = client.get("/app").text

    business = _section_html(report, "business-changes")
    assert ">Email not sent<" in business
    assert "Email alert" not in business
    assert "No alert" not in business
    assert [
        re.search(r'badge--\w+">([^<]+)<', row).group(1)
        for row in _history_rows(report)
    ] == ["No alert needed", "Email not sent", "No alert needed"]
    technical = report[report.index("Technical details") :]
    assert "Email not sent" in technical
    feed = _section_html(overview, "recent-changes")
    assert ">Email not sent<" in feed
    assert "No alert required" not in report + overview


def test_failed_latest_scan_keeps_retained_business_changes_first(client) -> None:
    now = datetime.now(timezone.utc)
    target_id = _target(
        name="Rival",
        last_check_at=now.isoformat(),
        last_check_error="The hostname could not be resolved.",
    )
    _capture(target_id, (now - timedelta(days=2)).isoformat())
    _capture(
        target_id,
        (now - timedelta(days=1)).isoformat(),
        changes=(PRICE_CHANGE,),
        alert_status="sent",
        alert_recipient="owner@acme.example",
    )
    without_report = _target(
        name="New Rival",
        last_check_at=now.isoformat(),
        last_check_error="The hostname could not be resolved.",
    )
    _capture(without_report, (now - timedelta(days=1)).isoformat())

    body = client.get(f"/app/target/{target_id}").text

    header = body.index('class="page-header"')
    business = body.index('id="business-changes-title"')
    latest = body.index('id="latest-scan-title"')
    history = body.index('id="history-title"')
    assert header < business < latest < history
    assert "$99" in body[business:latest] and "$79" in body[business:latest]
    callout = _section_html(body, "latest-scan-title")
    assert "callout--compact" in callout
    assert "Latest scan failed." in callout
    assert "We couldn&#x27;t find or reach this website address." in callout
    assert "The business changes above are from an earlier successful scan." in callout
    assert "The hostname could not be resolved." in callout  # Technical details
    assert "below" not in _primary_content(callout)

    # Without retained business changes the failure stays first, full size.
    other = client.get(f"/app/target/{without_report}").text
    assert other.index('id="latest-scan-title"') < other.index(
        'id="business-changes-title"'
    )
    assert "callout--compact" not in other
    assert "Earlier results are still available below." in other


def test_monitor_rows_state_a_failed_scan_once(client) -> None:
    customer_id = _customer()
    _target(
        customer_id=customer_id,
        name="Failing Rival",
        last_check_at=datetime.now(timezone.utc).isoformat(),
        last_check_error="The hostname could not be resolved.",
    )

    for path in ("/app/monitors", f"/app/customer/{customer_id}"):
        body = client.get(path).text
        row = re.search(r"<tr>\s*<td class=\"cell-primary\">.*?</tr>", body, re.S)
        assert row is not None
        assert ">Needs attention<" in row.group(0)
        assert ">Scan failed<" in row.group(0)
        assert "Latest scan failed" not in body
        assert '<p class="cell-sub"></p>' not in body


def test_email_setup_message_avoids_server_jargon(client) -> None:
    target_id = _target(name="Rival")
    _capture(target_id, "2026-01-01T00:00:00+00:00")

    overview = _primary_content(client.get("/app").text)
    report = _primary_content(client.get(f"/app/target/{target_id}").text)

    assert "Email alerts are unavailable" in overview
    assert "until email delivery is set up." in overview
    assert "until email delivery is set up." in report
    for page in (overview, report):
        assert "configured on the server" not in page
        assert "SMTP" not in page


def test_form_controls_meet_contrast_minimums() -> None:
    css = (STATIC_DIR / "app.css").read_text(encoding="utf-8")
    tokens = dict(re.findall(r"(--[\w-]+):\s*(#[0-9a-fA-F]{6})\s*;", css))

    def color(declarations: str, prop: str) -> str:
        value = re.search(rf"(?<![\w-]){prop}:\s*([^;]+);", declarations).group(1)
        token = re.search(r"var\((--[\w-]+)\)", value)
        return tokens[token.group(1)] if token else re.search(r"#[0-9a-fA-F]{6}", value).group(0)

    control = next(
        block
        for block in re.findall(r"\.input,\s*\.select\s*\{([^}]*)\}", css)
        if "border:" in block
    )
    placeholder = re.search(r"\.input::placeholder\s*\{([^}]*)\}", css).group(1)
    border = color(control, "border")
    for surface in (tokens["--surface"], tokens["--canvas"]):
        assert _contrast(border, surface) >= 3
    assert _contrast(color(placeholder, "color"), tokens["--surface"]) >= 4.5


def test_footer_names_timezone_in_plain_language(client, monkeypatch) -> None:
    assert "Times shown in UTC." in client.get("/app").text

    monkeypatch.setenv("DISPLAY_TIMEZONE", "America/New_York")
    body = client.get("/app").text
    footer = body[body.index('<footer class="app-footer">') :]
    assert "Times shown in Eastern Time." in footer
    assert "America" not in footer
    assert "Times are shown in" not in body

    expected = {
        "America/Los_Angeles": "Pacific Time",
        "Europe/London": "London time",
        "America/Argentina/Buenos_Aires": "Buenos Aires time",
        "Etc/GMT+5": "UTC-05",
        "Not/A_Zone": "UTC",
        "": "UTC",
    }
    for zone, label in expected.items():
        monkeypatch.setenv("DISPLAY_TIMEZONE", zone)
        assert display_timezone_name() == label


def test_busy_state_reset_clears_aria_busy_and_metadata() -> None:
    reset = BUSY_SCRIPT[BUSY_SCRIPT.index('addEventListener("pageshow"') :]

    assert 'removeAttribute("data-idle-label")' in reset
    assert 'removeAttribute("aria-busy")' in reset
    assert "button.disabled = false" in reset


def test_customer_page_orders_changes_problems_operations_controls(client) -> None:
    now = datetime.now(timezone.utc)
    customer_id = _customer()
    failing = _target(
        customer_id=customer_id,
        name="Failing Rival",
        last_check_at=now.isoformat(),
        last_check_error="The website returned HTTP 503; no capture was saved.",
    )
    _capture(failing, (now - timedelta(days=1)).isoformat())
    changed = _target(customer_id=customer_id, name="Changed Rival")
    _capture(changed, (now - timedelta(days=2)).isoformat())
    _capture(
        changed,
        (now - timedelta(hours=1)).isoformat(),
        changes=(PRICE_CHANGE,),
        alert_status="sent",
        alert_recipient="owner@acme.example",
    )

    body = client.get(f"/app/customer/{customer_id}").text

    positions = [
        body.index('class="page-header"'),
        body.index('id="recent-changes-title"'),
        body.index('id="attention-title"'),
        body.index('id="competitors-title"'),
        body.index('id="add-competitor-title"'),
        body.index('id="customer-controls-title"'),
    ]
    assert positions == sorted(positions)
    assert "$99" in _section_html(body, "recent-changes")
    assert "Scan failed: Failing Rival" in _section_html(body, "attention")
