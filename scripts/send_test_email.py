"""Explicit manual SES transport smoke test; never invoked by the scheduler."""

import argparse
import sys

from faresentry.notification_models import NotificationMessage
from faresentry.notifications import NotificationProviderError
from faresentry.providers.ses import SESEmailProvider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Send one FareSentry SES test email.")
    parser.add_argument(
        "--send", action="store_true", help="Explicitly send the test email"
    )
    args = parser.parse_args(argv)
    if not args.send:
        parser.error("Pass --send to explicitly send one test email")
    try:
        provider = SESEmailProvider.from_environment()
        receipt = provider.send(
            NotificationMessage(
                subject="FareSentry: email delivery test",
                body=(
                    "This is an explicitly requested FareSentry email transport test. "
                    "No fare check was performed."
                ),
            )
        )
    except (ValueError, NotificationProviderError) as error:
        print(str(error), file=sys.stderr)
        return 1
    print(f"SES accepted the test email: {receipt.provider_message_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
