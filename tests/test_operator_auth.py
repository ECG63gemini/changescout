import base64
import threading

import pytest
from bs4 import BeautifulSoup
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

import app as app_module


OPERATOR_AUTH = ("test-operator", "test-only-long-random-password")
OPERATOR_PAGES = (
    "/app",
    "/app/",
    "/app/customers",
    "/app/customers/new",
    "/app/customer/1",
    "/app/customer/1/targets/new",
    "/app/monitors",
    "/app/target/1",
)
OPERATOR_ACTIONS = (
    "/app/customers",
    "/app/targets",
    "/app/customer/1/targets",
    "/app/customer/1/status",
    "/app/target/1/settings",
    "/app/check/1",
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "DB", tmp_path / "operator-auth.db")
    monkeypatch.setenv("ADMIN_USERNAME", OPERATOR_AUTH[0])
    monkeypatch.setenv("ADMIN_PASSWORD", OPERATOR_AUTH[1])
    app_module._active_checks.clear()
    app_module.init_db()
    with app_module.db() as conn:
        conn.execute(
            "INSERT INTO customers(name,email,created_at) VALUES(?,?,?)",
            ("Private Customer", "private-customer@example.com", "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO targets(customer_id,name,url,created_at) VALUES(?,?,?,?)",
            (1, "Private Competitor", "https://competitor.example", "2026-01-01T00:00:00Z"),
        )
    return TestClient(app_module.app)


@pytest.mark.parametrize("path", OPERATOR_PAGES + ("/app/settings", "/app/reports"))
def test_operator_pages_require_authentication_before_routing(client, path):
    response = client.get(path, follow_redirects=False)

    assert response.status_code == 401
    assert response.headers["www-authenticate"].startswith("Basic ")
    assert response.headers["cache-control"] == "no-store"
    assert "Private Customer" not in response.text
    assert "private-customer@example.com" not in response.text


@pytest.mark.parametrize("path", OPERATOR_ACTIONS)
def test_operator_posts_require_authentication_without_side_effects(client, path):
    response = client.post(
        path,
        data={"name": "Attacker", "email": "attacker@example.com"},
        follow_redirects=False,
    )

    assert response.status_code == 401
    with app_module.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM customers").fetchone()[0] == 1
        target = conn.execute("SELECT * FROM targets WHERE id=1").fetchone()
        assert target["name"] == "Private Competitor"
        assert target["enabled"] == 1
        assert target["last_check_at"] is None
        assert conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 0


@pytest.mark.parametrize("method", ("HEAD", "OPTIONS", "PUT", "PATCH", "DELETE"))
def test_authentication_gate_covers_all_methods(client, method):
    assert client.request(method, "/app/customer/1").status_code == 401


@pytest.mark.parametrize(
    "auth",
    (
        ("wrong-user", OPERATOR_AUTH[1]),
        (OPERATOR_AUTH[0], "wrong-password"),
        ("wrong-user", "wrong-password"),
        ("", ""),
    ),
)
@pytest.mark.parametrize("method,path", (("GET", "/app"), ("POST", "/app/check/1")))
def test_invalid_credentials_are_rejected(client, auth, method, path):
    response = client.request(method, path, auth=auth)
    assert response.status_code == 401
    assert "www-authenticate" in response.headers


@pytest.mark.parametrize(
    "authorization",
    (
        "Bearer not-a-basic-credential",
        "Basic !!!",
        "Basic " + base64.b64encode(b"missing-colon").decode("ascii"),
        "Basic " + base64.b64encode(b"\xff:password").decode("ascii"),
    ),
)
def test_malformed_credentials_are_rejected_without_errors(client, authorization):
    response = client.get("/app", headers={"Authorization": authorization})
    assert response.status_code == 401


@pytest.mark.parametrize("path", OPERATOR_PAGES)
def test_valid_credentials_allow_operator_pages(client, path):
    response = client.get(path, auth=OPERATOR_AUTH)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert 'href="/app"' in response.text


@pytest.mark.parametrize("path", OPERATOR_PAGES)
def test_operator_navigation_and_form_actions_stay_under_app(client, path):
    response = client.get(path, auth=OPERATOR_AUTH)
    soup = BeautifulSoup(response.text, "html.parser")
    for element in soup.select("a[href], form[action]"):
        url = element.get("href") or element.get("action")
        if url.startswith("/"):
            assert url == "/app" or url.startswith("/app/"), url


@pytest.mark.parametrize(
    "variable,value",
    (
        ("ADMIN_USERNAME", None),
        ("ADMIN_PASSWORD", None),
        ("ADMIN_USERNAME", ""),
        ("ADMIN_PASSWORD", ""),
        ("ADMIN_USERNAME", " "),
        ("ADMIN_PASSWORD", " "),
    ),
)
def test_missing_configuration_fails_closed_but_keeps_public_site_open(
    client, monkeypatch, variable, value
):
    if value is None:
        monkeypatch.delenv(variable)
    else:
        monkeypatch.setenv(variable, value)

    for method, path in (("GET", "/app"), ("POST", "/app/check/1")):
        response = client.request(method, path, auth=OPERATOR_AUTH)
        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"
    assert client.get("/").status_code == 200
    assert client.get("/landing").status_code == 200
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/static/landing.css").status_code == 200
    response = client.post(
        "/landing/contact",
        data={"name": "Public visitor", "business_name": "Shop", "email": "lead@example.com"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/?submitted=1#contact"


@pytest.mark.parametrize(
    "path",
    (
        "/customers",
        "/customers/new",
        "/customer/1",
        "/customer/1/targets/new",
        "/monitors",
        "/target/1",
        "/docs",
        "/redoc",
        "/openapi.json",
        "/data/changescout.db",
        "/data/screenshots/test.png",
    ),
)
def test_legacy_and_private_paths_do_not_expose_operator_data(client, path):
    response = client.get(path, follow_redirects=False)
    assert response.status_code == 404
    assert "Private Customer" not in response.text
    assert "private-customer@example.com" not in response.text


@pytest.mark.parametrize("path", tuple(path.removeprefix("/app") for path in OPERATOR_ACTIONS))
def test_old_operator_posts_are_not_registered(client, path):
    assert client.post(path, follow_redirects=False).status_code == 404


def test_only_marketing_routes_are_registered_outside_operator_prefix():
    public_paths = {
        route.path
        for route in app_module.app.routes
        if isinstance(route, APIRoute) and not route.path.startswith("/app")
    }
    assert public_paths == {"/", "/landing", "/landing/contact"}


@pytest.mark.parametrize(
    "headers",
    (
        {"Origin": "https://attacker.example"},
        {"Origin": "null"},
        {"Sec-Fetch-Site": "cross-site"},
    ),
)
def test_cross_site_operator_posts_are_rejected_even_with_credentials(client, headers):
    response = client.post(
        "/app/customer/1/status",
        data={"active": "0"},
        auth=OPERATOR_AUTH,
        headers=headers,
    )
    assert response.status_code == 403
    with app_module.db() as conn:
        assert conn.execute("SELECT active FROM customers WHERE id=1").fetchone()[0] == 1


def test_authenticated_operator_workflow_and_redirects(client, monkeypatch, tmp_path):
    client.auth = OPERATOR_AUTH
    client.headers["Origin"] = "http://testserver"
    response = client.post(
        "/app/customers",
        data={"name": "New Customer", "email": "new@example.com"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"] == "/app/customer/2"

    async def validated_url(url):
        return url

    async def capture_page(target_id, url):
        return tmp_path / f"{target_id}.png", "Baseline public page content"

    monkeypatch.setattr(app_module, "_validate_public_url_async", validated_url)
    monkeypatch.setattr(app_module, "capture_page", capture_page)

    for path, expected in (
        ("/app/targets", "/app/monitors"),
        ("/app/customer/2/targets", "/app/customer/2"),
    ):
        response = client.post(
            path,
            data={
                "name": "New Competitor",
                "url": "https://new-competitor.example",
                "enabled": "yes",
                "check_frequency": "hourly",
            },
            follow_redirects=False,
        )
        assert response.status_code == 303
        assert response.headers["location"].startswith(expected + "?notice=")

    response = client.post(
        "/app/target/1/settings",
        data={"check_frequency": "every_6_hours", "alert_email": "alerts@example.com"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/app/target/1?notice=")

    response = client.post("/app/check/1", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/app/target/1"
    report = client.get(response.headers["location"])
    assert report.status_code == 200
    assert "Private Competitor" in report.text
    assert 'action="/app/target/1/settings"' in report.text
    assert 'action="/app/check/1"' in report.text

    response = client.post(
        "/app/customer/1/status", data={"active": "0"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/app/customer/1?notice=")
    with app_module.db() as conn:
        assert conn.execute("SELECT COUNT(*) FROM targets").fetchone()[0] == 3
        assert conn.execute("SELECT COUNT(*) FROM captures").fetchone()[0] == 1
        target = conn.execute("SELECT * FROM targets WHERE id=1").fetchone()
        assert target["enabled"] == 0
        assert target["check_frequency"] == "every_6_hours"
        assert target["alert_email"] == "alerts@example.com"
        assert conn.execute("SELECT active FROM customers WHERE id=1").fetchone()[0] == 0


def test_lifespan_scheduler_runs_without_operator_credentials(client, monkeypatch, tmp_path):
    monkeypatch.delenv("ADMIN_USERNAME")
    monkeypatch.delenv("ADMIN_PASSWORD")
    completed = threading.Event()
    run_due_checks = app_module.run_due_checks

    async def capture_page(target_id, url):
        return tmp_path / f"{target_id}.png", "Scheduled baseline"

    async def observed_cycle():
        await run_due_checks()
        completed.set()

    monkeypatch.setattr(app_module, "capture_page", capture_page)
    monkeypatch.setattr(app_module, "run_due_checks", observed_cycle)
    with TestClient(app_module.app) as running_client:
        assert completed.wait(timeout=5), "The scheduler did not complete its first cycle."
        assert running_client.get("/app").status_code == 503
        assert running_client.get("/").status_code == 200

    with app_module.db() as conn:
        capture = conn.execute("SELECT * FROM captures").fetchone()
        assert capture["target_id"] == 1
        assert capture["summary"] == "Baseline capture created."
        assert conn.execute("SELECT last_check_at FROM targets").fetchone()[0]


@pytest.mark.parametrize("base_url", ("https://changescout.example", "https://changescout.example/"))
def test_email_report_url_uses_protected_operator_path(monkeypatch, base_url):
    monkeypatch.setenv("APP_BASE_URL", base_url)
    assert app_module._application_report_url(42) == "https://changescout.example/app/target/42"


def test_credentials_are_not_exposed_in_responses_or_logs(client, caplog):
    responses = (
        client.get("/"),
        client.get("/app"),
        client.get("/app", auth=OPERATOR_AUTH),
        client.get("/app", auth=("invalid", "invalid")),
    )
    for response in responses:
        assert OPERATOR_AUTH[0] not in response.text
        assert OPERATOR_AUTH[1] not in response.text
    assert OPERATOR_AUTH[0] not in caplog.text
    assert OPERATOR_AUTH[1] not in caplog.text
