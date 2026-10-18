from unittest.mock import Mock

import pytest

import emailing


@pytest.fixture(autouse=True)
def isolate_resend_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("RESEND_FROM_EMAIL", raising=False)


@pytest.fixture
def resend_api(monkeypatch: pytest.MonkeyPatch) -> Mock:
    monkeypatch.setenv("RESEND_API_KEY", "re_test_api_key")
    monkeypatch.setenv("RESEND_FROM_EMAIL", "alerts@example.com")
    connection = Mock()
    connection.getresponse.return_value.status = 200
    connection.getresponse.return_value.read.return_value = b'{"id":"test-email-id"}'
    factory = Mock(return_value=connection)
    monkeypatch.setattr(emailing, "HTTPSConnection", factory)
    monkeypatch.setattr(
        emailing.smtplib,
        "SMTP",
        Mock(side_effect=AssertionError("Resend must not attempt SMTP delivery.")),
    )
    return factory
