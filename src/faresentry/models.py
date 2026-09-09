"""Validated domain data, independent of providers and agent judgment."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Self

from pydantic import BaseModel, Field, StringConstraints, model_validator

AirportCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]


class TripQuery(BaseModel):
    """One round trip between two airports, with fixed travel dates."""

    origin: AirportCode
    destination: AirportCode
    outbound_date: date
    return_date: date
    currency: CurrencyCode = "USD"

    @model_validator(mode="after")
    def validate_trip(self) -> Self:
        if self.origin == self.destination:
            raise ValueError("origin and destination must differ")
        if self.return_date < self.outbound_date:
            raise ValueError("return_date must be on or after outbound_date")
        return self


class FlightOption(BaseModel):
    """An itinerary summary; price and metrics cover the returned option.

    Providers must use the same journey scope when comparing options.
    Duration includes layovers, and stops counts intermediate connections.
    """

    airline: str = Field(min_length=1)
    price: Decimal = Field(ge=0, allow_inf_nan=False)
    currency: CurrencyCode = "USD"
    stops: int = Field(ge=0)
    duration_minutes: int = Field(gt=0)
