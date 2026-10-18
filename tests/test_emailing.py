import json
import smtplib
import sys
import traceback
from http.client import HTTPException
from unittest.mock import MagicMock

import pytest

import emailing
import send_test_email
from emailing import (
    EmailContent,
    EmailDeliveryError,
    ResendSettings,
    SMTPSettings,
    email_settings_from_environment,
    send_email,
)

SMTP_ENVIRONMENT = {
    "SMTP_HOST": "smtp.example.com",
    "SMTP_PORT": "587",
    "SMTP_USERNAME": "smtp-user",
    "SMTP_PASSWORD": "smtp-secret",
    "SMTP_FROM": "smtp-alerts@example.com",
    "SMTP_USE_TLS": "true",
}
CONTENT = EmailContent(
    subject="ChangeScout test email",
    body="ChangeScout email delivery is working.\n",
)


def test_resend_is_preferred_even_when_smtp_configuration_is_invalid() -> None:
    settings = email_settings_from_environment(
        {
            **SMTP_ENVIRONMENT,
            "SMTP_PORT": "invalid",
            "RESEND_API_KEY": " re_test_api_key ",
            "RESEND_FROM_EMAIL": " alerts@example.com ",
        }
    )

    assert isinstance(settings, ResendSettings)
    assert settings.configured
    assert settings.api_key == "re_test_api_key"
    assert settings.from_address == "alerts@example.com"
    assert settings.configuration_error is None
    assert "re_test_api_key" not in repr(settings)
    assert "smtp-secret" not in repr(
        SMTPSettings.from_environment(SMTP_ENVIRONMENT)
    )


@pytest.mark.parametrize(
    "resend_environment",
    [
        {},
        {"RESEND_API_KEY": "re_test_api_key"},
        {"RESEND_FROM_EMAIL": "alerts@example.com"},
        {"RESEND_API_KEY": "", "RESEND_FROM_EMAIL": ""},
        {"RESEND_API_KEY": " \t ", "RESEND_FROM_EMAIL": "alerts@example.com"},
        {"RESEND_API_KEY": "re_test_api_key", "RESEND_FROM_EMAIL": " \t "},
    ],
)
def test_smtp_fallback_when_resend_configuration_is_incomplete(
    resend_environment,
) -> None:
    settings = email_settings_from_environment(
        {**SMTP_ENVIRONMENT, **resend_environment}
    )

    assert isinstance(settings, SMTPSettings)
    assert settings == SMTPSettings.from_environment(SMTP_ENVIRONMENT)


def test_invalid_resend_sender_is_reported_without_switching_to_smtp(
    resend_api,
) -> None:
    settings = email_settings_from_environment(
        {
            **SMTP_ENVIRONMENT,
            "RESEND_API_KEY": "re_test_api_key",
            "RESEND_FROM_EMAIL": "not-an-email",
        }
    )

    assert isinstance(settings, ResendSettings)
    assert not settings.configured
    with pytest.raises(
        EmailDeliveryError,
        match="RESEND_FROM_EMAIL must be a valid email address",
    ):
        send_email(settings, "owner@example.com", CONTENT)
    resend_api.assert_not_called()


@pytest.mark.parametrize(
    ("use_tls", "authenticated"),
    [("true", True), ("true", False), ("false", False)],
)
def test_smtp_fallback_preserves_existing_delivery(
    monkeypatch,
    use_tls,
    authenticated,
) -> None:
    environment = {**SMTP_ENVIRONMENT, "SMTP_USE_TLS": use_tls}
    if not authenticated:
        environment["SMTP_USERNAME"] = ""
        environment["SMTP_PASSWORD"] = ""
    smtp_factory = MagicMock()
    monkeypatch.setattr(emailing.smtplib, "SMTP", smtp_factory)
    smtp = smtp_factory.return_value.__enter__.return_value

    send_email(
        email_settings_from_environment(environment),
        " owner@example.com ",
        CONTENT,
    )

    smtp_factory.assert_called_once_with("smtp.example.com", 587, timeout=30)
    assert smtp.ehlo.call_count == (2 if use_tls == "true" else 1)
    if use_tls == "true":
        smtp.starttls.assert_called_once()
    else:
        smtp.starttls.assert_not_called()
    if authenticated:
        smtp.login.assert_called_once_with("smtp-user", "smtp-secret")
    else:
        smtp.login.assert_not_called()
    smtp.send_message.assert_called_once()
    message = smtp.send_message.call_args.args[0]
    assert message["From"] == "smtp-alerts@example.com"
    assert message["To"] == "owner@example.com"
    assert message["Subject"] == CONTENT.subject
    assert message.get_content() == CONTENT.body


def test_smtp_failure_preserves_existing_safe_error(monkeypatch, caplog) -> None:
    smtp_factory = MagicMock()
    smtp = smtp_factory.return_value.__enter__.return_value
    smtp.send_message.side_effect = smtplib.SMTPException("smtp-secret")
    monkeypatch.setattr(emailing.smtplib, "SMTP", smtp_factory)

    with pytest.raises(EmailDeliveryError) as failure:
        send_email(
            email_settings_from_environment(SMTP_ENVIRONMENT),
            "owner@example.com",
            CONTENT,
        )

    assert str(failure.value) == "SMTP delivery failed (SMTPException)."
    assert "smtp-secret" not in caplog.text


def test_resend_sends_plain_text_over_https(resend_api) -> None:
    send_email(
        email_settings_from_environment(),
        " owner@example.com ",
        CONTENT,
    )

    resend_api.assert_called_once_with("api.resend.com", timeout=30)
    connection = resend_api.return_value
    connection.request.assert_called_once()
    request = connection.request.call_args
    assert request.args == ("POST", "/emails")
    assert request.kwargs["headers"] == {
        "Authorization": "Bearer re_test_api_key",
        "Content-Type": "application/json",
        "User-Agent": "ChangeScout",
    }
    assert json.loads(request.kwargs["body"]) == {
        "from": "alerts@example.com",
        "to": ["owner@example.com"],
        "subject": CONTENT.subject,
        "text": CONTENT.body,
    }
    connection.getresponse.return_value.read.assert_called_once_with()
    connection.close.assert_called_once_with()


@pytest.mark.parametrize(
    "recipient",
    ["not-an-email", "owner@example.com\r\nBcc: someone@example.com"],
)
def test_resend_validates_recipient_before_connecting(resend_api, recipient) -> None:
    with pytest.raises(ValueError, match="Enter a valid alert email address"):
        send_email(email_settings_from_environment(), recipient, CONTENT)

    resend_api.assert_not_called()


@pytest.mark.parametrize("status", [301, 400, 401, 403, 422, 429, 500, 503])
def test_resend_http_errors_are_safe_and_do_not_fall_back(
    resend_api,
    caplog,
    status,
) -> None:
    connection = resend_api.return_value
    response = connection.getresponse.return_value
    response.status = status
    response.read.return_value = (
        b'{"message":"Authorization: Bearer re_test_api_key smtp-secret"}'
    )

    with pytest.raises(EmailDeliveryError) as failure:
        send_email(email_settings_from_environment(), "owner@example.com", CONTENT)

    assert str(failure.value) == f"Resend delivery failed (HTTP {status})."
    response.read.assert_not_called()
    connection.request.assert_called_once()
    connection.close.assert_called_once_with()
    for secret in ("re_test_api_key", "smtp-secret", "Authorization", "Bearer"):
        assert secret not in str(failure.value)
        assert secret not in caplog.text


@pytest.mark.parametrize(
    "exception_type",
    [OSError, TimeoutError, HTTPException, ValueError],
)
def test_resend_transport_errors_do_not_expose_secrets(
    resend_api,
    caplog,
    exception_type,
) -> None:
    connection = resend_api.return_value
    connection.request.side_effect = exception_type(
        "Authorization: Bearer re_test_api_key smtp-secret"
    )

    with pytest.raises(EmailDeliveryError) as failure:
        send_email(email_settings_from_environment(), "owner@example.com", CONTENT)

    assert str(failure.value) == (
        f"Resend delivery failed ({exception_type.__name__})."
    )
    connection.close.assert_called_once_with()
    rendered_error = "".join(traceback.format_exception(failure.value))
    for secret in ("re_test_api_key", "smtp-secret", "Authorization", "Bearer"):
        assert secret not in rendered_error
        assert secret not in caplog.text


@pytest.mark.parametrize(
    "response_body",
    [
        b"not JSON",
        b"\xff",
        b"null",
        b"[]",
        b"{}",
        b'{"id":null}',
        b'{"id":7}',
        b'{"id":""}',
        b'{"id":" "}',
    ],
)
def test_resend_requires_a_valid_success_response(resend_api, response_body) -> None:
    connection = resend_api.return_value
    connection.getresponse.return_value.read.return_value = response_body

    with pytest.raises(EmailDeliveryError, match="Resend"):
        send_email(email_settings_from_environment(), "owner@example.com", CONTENT)

    connection.close.assert_called_once_with()


@pytest.mark.parametrize("provider", ["resend", "smtp"])
def test_test_email_uses_selected_provider(
    monkeypatch,
    resend_api,
    capsys,
    provider,
) -> None:
    monkeypatch.setattr(send_test_email, "load_dotenv", lambda path: None)
    monkeypatch.setattr(sys, "argv", ["send_test_email.py", "owner@example.com"])
    smtp_factory = MagicMock()
    monkeypatch.setattr(emailing.smtplib, "SMTP", smtp_factory)
    if provider == "smtp":
        monkeypatch.delenv("RESEND_API_KEY")
        monkeypatch.delenv("RESEND_FROM_EMAIL")
        for name, value in SMTP_ENVIRONMENT.items():
            monkeypatch.setenv(name, value)

    send_test_email.main()

    assert capsys.readouterr().out == "Test email sent to owner@example.com.\n"
    if provider == "resend":
        smtp_factory.assert_not_called()
        request = resend_api.return_value.request.call_args
        assert json.loads(request.kwargs["body"]) == {
            "from": "alerts@example.com",
            "to": ["owner@example.com"],
            "subject": CONTENT.subject,
            "text": CONTENT.body,
        }
    else:
        resend_api.assert_not_called()
        smtp_factory.return_value.__enter__.return_value.send_message.assert_called_once()


def test_resend_test_email_failure_exits_without_success_or_secrets(
    monkeypatch,
    resend_api,
    capsys,
    caplog,
) -> None:
    monkeypatch.setattr(send_test_email, "load_dotenv", lambda path: None)
    monkeypatch.setattr(sys, "argv", ["send_test_email.py", "owner@example.com"])
    resend_api.return_value.getresponse.return_value.status = 403

    with pytest.raises(SystemExit) as failure:
        send_test_email.main()

    assert str(failure.value) == "Resend delivery failed (HTTP 403)."
    output = capsys.readouterr()
    assert output.out == ""
    for secret in ("re_test_api_key", "smtp-secret", "Authorization", "Bearer"):
        assert secret not in output.err
        assert secret not in caplog.text
