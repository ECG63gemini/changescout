import os
import re
import smtplib
import ssl
from collections.abc import Mapping
from dataclasses import dataclass
from email.message import EmailMessage

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
    password: str
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
        f"Detected: {detected_at}",
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
    settings: SMTPSettings,
    recipient: str,
    content: EmailContent,
) -> None:
    if not settings.configured:
        raise EmailDeliveryError(
            settings.configuration_error or "SMTP is not configured."
        )
    validated_recipient = validate_email_address(recipient)
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
