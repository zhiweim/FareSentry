"""One plain-text email recipient through Amazon SES; AWS details stay here."""

import os
import re
from typing import Protocol

import boto3
from botocore.config import Config

from faresentry.notification_models import DeliveryReceipt, NotificationMessage
from faresentry.notifications import NotificationProviderError


class SESClient(Protocol):
    def send_email(self, **kwargs: object) -> dict[str, object]: ...


class SESEmailProvider:
    """Use the standard AWS credential chain; construction performs no AWS I/O."""

    def __init__(
        self,
        *,
        sender: str,
        recipient: str,
        region_name: str | None = None,
        client: SESClient | None = None,
    ) -> None:
        for address in (sender, recipient):
            if not address.isascii() or not re.fullmatch(
                r"[^@\s<>,;]+@[^@\s<>,;]+", address
            ):
                raise ValueError(
                    "Supply one plain ASCII sender and recipient email address"
                )
        self._sender = sender
        self._recipient = recipient
        self._region_name = region_name
        self._client = client

    @classmethod
    def from_environment(cls) -> "SESEmailProvider":
        return cls(
            sender=os.environ.get("FARESENTRY_EMAIL_FROM", ""),
            recipient=os.environ.get("FARESENTRY_EMAIL_TO", ""),
            region_name=os.environ.get("AWS_REGION") or None,
        )

    def send(self, message: NotificationMessage) -> DeliveryReceipt:
        try:
            client = self._client
            if client is None:
                client = boto3.client(
                    "ses",
                    region_name=self._region_name,
                    config=Config(
                        connect_timeout=10,
                        read_timeout=30,
                        retries={"total_max_attempts": 1, "mode": "standard"},
                    ),
                )
                self._client = client
            response = client.send_email(
                Source=self._sender,
                Destination={"ToAddresses": [self._recipient]},
                Message={
                    "Subject": {"Data": message.subject, "Charset": "UTF-8"},
                    "Body": {"Text": {"Data": message.body, "Charset": "UTF-8"}},
                },
            )
            return DeliveryReceipt(
                provider="ses", provider_message_id=response["MessageId"]
            )
        except Exception:
            raise NotificationProviderError("SES email request failed") from None
