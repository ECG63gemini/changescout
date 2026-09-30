import argparse
from pathlib import Path

from dotenv import load_dotenv

from emailing import (
    EmailContent,
    EmailDeliveryError,
    SMTPSettings,
    send_email,
    validate_email_address,
)

BASE = Path(__file__).resolve().parent


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Send a harmless ChangeScout SMTP configuration test."
    )
    parser.add_argument("recipient", help="Email address that should receive the test")
    args = parser.parse_args()

    load_dotenv(BASE / ".env")
    try:
        recipient = validate_email_address(args.recipient)
    except ValueError as exc:
        raise SystemExit(str(exc)) from exc

    settings = SMTPSettings.from_environment()
    if not settings.configured:
        raise SystemExit(
            f"Email is not configured: {settings.configuration_error}"
        )

    try:
        send_email(
            settings,
            recipient,
            EmailContent(
                subject="ChangeScout test email",
                body="ChangeScout email delivery is working.\n",
            ),
        )
    except EmailDeliveryError as exc:
        raise SystemExit(str(exc)) from exc
    print(f"Test email sent to {recipient}.")


if __name__ == "__main__":
    main()
