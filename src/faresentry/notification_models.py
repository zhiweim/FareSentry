"""Transport-neutral notification data; no SDK, database, or delivery behavior."""

from datetime import UTC, datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from faresentry.models import FlightLabel


class NotificationMessage(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    subject: str = Field(min_length=1, max_length=200)
    body: str = Field(min_length=1)

    @field_validator("subject")
    @classmethod
    def validate_subject(cls, value: str) -> str:
        if "\r" in value or "\n" in value:
            raise ValueError("Email subject must be a single line")
        return value


class DeliveryReceipt(BaseModel):
    """Provider acceptance for sending, not a confirmation of inbox arrival."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    provider: FlightLabel
    provider_message_id: FlightLabel


class NotificationDelivery(DeliveryReceipt):
    run_id: int = Field(gt=0, strict=True)
    delivered_at: AwareDatetime

    @field_validator("delivered_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class NotificationOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    watch_id: str
    run_id: int | None = Field(default=None, gt=0, strict=True)
    status: Literal[
        "not_applicable", "not_needed", "delivered", "already_delivered", "failed"
    ]
    delivery: NotificationDelivery | None = None
    failure_stage: Literal["validation", "history", "provider", "recording"] | None = (
        None
    )
    error_type: str | None = None
