import json
import os
import re
import smtplib
import ssl
from collections.abc import Mapping
from contextlib import closing
from dataclasses import dataclass, field
from email.message import EmailMessage
from http.client import HTTPException, HTTPSConnection

from display_time import format_display_time
from monitoring import StructuredChange

_EMAIL_RE = re.compile(r"^[^@\s<>]+@[^@\s<>]+\.[^@\s<>]+$")
_TRUE_VALUES = {"1", "true", "yes", "on"}
_FALSE_VALUES = {"0", "false", "no", "off"}


class EmailDeliveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class SMTPSettings:
    host: str
    port: int
    username: str
    password: str = field(repr=False)
    from_address: str
    use_tls: bool
    configuration_error: str | None = None

    @property
    def configured(self) -> bool:
        return self.configuration_error is None

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "SMTPSettings":
        env = environment if environment is not None else os.environ
        host = env.get("SMTP_HOST", "").strip()
        username = env.get("SMTP_USERNAME", "").strip()
        password = env.get("SMTP_PASSWORD", "")
        from_address = env.get("SMTP_FROM", "").strip()
        tls_value = env.get("SMTP_USE_TLS", "true").strip().casefold()
        port_value = env.get("SMTP_PORT", "587").strip()

        error: str | None = None
        try:
            port = int(port_value)
            if not 1 <= port <= 65535:
                raise ValueError
        except ValueError:
            port = 587
            error = "SMTP_PORT must be an integer from 1 to 65535."

        if tls_value in _TRUE_VALUES:
            use_tls = True
        elif tls_value in _FALSE_VALUES:
            use_tls = False
        else:
            use_tls = True
            error = error or (
                "SMTP_USE_TLS must be true/false, yes/no, on/off, or 1/0."
            )

        if not host or not from_address:
            error = error or "SMTP_HOST and SMTP_FROM are required."
        elif not valid_email_address(from_address):
            error = error or "SMTP_FROM must be a valid email address."
        elif bool(username) != bool(password):
            error = error or (
                "SMTP_USERNAME and SMTP_PASSWORD must be configured together."
            )
        elif username and not use_tls:
            error = error or (
                "Authenticated SMTP requires SMTP_USE_TLS=true."
            )

        return cls(
            host=host,
            port=port,
            username=username,
            password=password,
            from_address=from_address,
            use_tls=use_tls,
            configuration_error=error,
        )


@dataclass(frozen=True)
class ResendSettings:
    api_key: str = field(repr=False)
    from_address: str
    configuration_error: str | None = None

    @property
    def configured(self) -> bool:
        return self.configuration_error is None

    @classmethod
    def from_environment(
        cls,
        environment: Mapping[str, str] | None = None,
    ) -> "ResendSettings":
        env = environment if environment is not None else os.environ
        api_key = env.get("RESEND_API_KEY", "").strip()
        from_address = env.get("RESEND_FROM_EMAIL", "").strip()
        error: str | None = None
        if not api_key or not from_address:
            error = "RESEND_API_KEY and RESEND_FROM_EMAIL are required."
        elif not valid_email_address(from_address):
            error = "RESEND_FROM_EMAIL must be a valid email address."
        return cls(
            api_key=api_key,
            from_address=from_address,
            configuration_error=error,
        )


EmailSettings = SMTPSettings | ResendSettings


def email_settings_from_environment(
    environment: Mapping[str, str] | None = None,
) -> EmailSettings:
    env = environment if environment is not None else os.environ
    resend = ResendSettings.from_environment(env)
    if resend.api_key and resend.from_address:
        return resend
    return SMTPSettings.from_environment(env)


@dataclass(frozen=True)
class EmailContent:
    subject: str
    body: str


def valid_email_address(value: str) -> bool:
    candidate = value.strip()
    return (
        len(candidate) <= 254
        and "\r" not in candidate
        and "\n" not in candidate
        and bool(_EMAIL_RE.fullmatch(candidate))
    )


def validate_email_address(value: str) -> str:
    candidate = value.strip()
    if not valid_email_address(candidate):
        raise ValueError("Enter a valid alert email address.")
    return candidate


def _header_text(value: str) -> str:
    return " ".join(value.replace("\r", " ").replace("\n", " ").split())


def render_inquiry_notification(
    *,
    name: str,
    business_name: str,
    email: str,
    website: str | None,
    message: str | None,
    submitted_at: str,
) -> EmailContent:
    lines = [
        "A new landing-page inquiry was submitted.",
        "",
        f"Name: {name}",
        f"Business name: {business_name}",
        f"Email: {email}",
    ]
    if website:
        lines.append(f"Website: {website}")
    if message:
        lines.extend(["", "Message:", message])
    lines.extend(
        [
            "",
            f"Submission time: {format_display_time(submitted_at)}",
        ]
    )
    return EmailContent(
        subject=(
            "ChangeScout: New inquiry from "
            f"{_header_text(business_name)}"
        ),
        body="\n".join(lines).rstrip() + "\n",
    )


def render_change_alert(
    *,
    competitor_name: str,
    page_url: str,
    detected_at: str,
    changes: tuple[StructuredChange, ...],
    report_url: str | None = None,
) -> EmailContent:
    count = len(changes)
    noun = "change" if count == 1 else "changes"
    subject = (
        f"ChangeScout: {count} important {noun} at "
        f"{_header_text(competitor_name)}"
    )
    lines = [
        "ChangeScout detected commercially meaningful changes.",
        "",
        f"Competitor: {competitor_name}",
        f"Page URL: {page_url}",
        f"Detected: {format_display_time(detected_at)}",
        "",
    ]
    for change in changes:
        lines.extend(
            [
                change.category,
                f"{change.old_value} -> {change.new_value}",
                change.description,
                "",
            ]
        )
    if report_url:
        lines.extend(["View the ChangeScout report:", report_url, ""])
    return EmailContent(subject=subject, body="\n".join(lines).rstrip() + "\n")


def send_email(
    settings: EmailSettings,
    recipient: str,
    content: EmailContent,
) -> None:
    if not settings.configured:
        raise EmailDeliveryError(
            settings.configuration_error or "Email is not configured."
        )
    validated_recipient = validate_email_address(recipient)
    if isinstance(settings, ResendSettings):
        _send_resend_email(settings, validated_recipient, content)
        return

    message = EmailMessage()
    message["From"] = settings.from_address
    message["To"] = validated_recipient
    message["Subject"] = content.subject
    message.set_content(content.body)

    try:
        with smtplib.SMTP(settings.host, settings.port, timeout=30) as smtp:
            smtp.ehlo()
            if settings.use_tls:
                smtp.starttls(context=ssl.create_default_context())
                smtp.ehlo()
            if settings.username:
                smtp.login(settings.username, settings.password)
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as exc:
        raise EmailDeliveryError(
            f"SMTP delivery failed ({type(exc).__name__})."
        ) from exc


def _send_resend_email(
    settings: ResendSettings,
    recipient: str,
    content: EmailContent,
) -> None:
    payload = {
        "from": settings.from_address,
        "to": [recipient],
        "subject": content.subject,
        "text": content.body,
    }
    try:
        with closing(HTTPSConnection("api.resend.com", timeout=30)) as connection:
            connection.request(
                "POST",
                "/emails",
                body=json.dumps(payload).encode("utf-8"),
                headers={
                    "Authorization": f"Bearer {settings.api_key}",
                    "Content-Type": "application/json",
                    "User-Agent": "ChangeScout",
                },
            )
            response = connection.getresponse()
            if not 200 <= response.status < 300:
                raise EmailDeliveryError(
                    f"Resend delivery failed (HTTP {response.status})."
                )
            result = json.loads(response.read())
    except (OSError, HTTPException, ValueError) as exc:
        # Provider exception text can contain credentials or authorization headers.
        raise EmailDeliveryError(
            f"Resend delivery failed ({type(exc).__name__})."
        ) from None

    if (
        not isinstance(result, dict)
        or not isinstance(result.get("id"), str)
        or not result["id"].strip()
    ):
        raise EmailDeliveryError("Resend returned an invalid email response.")
