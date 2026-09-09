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
    """An outbound choice with a round-trip price, not a complete itinerary.

    Route, flights, duration, stops, and layovers describe the outbound only.
    Duration includes layovers. Return flights have not been selected; the
    departure token allows a later lookup of compatible return choices.
    """

    airline: str = Field(min_length=1)
    price: Decimal = Field(ge=0, allow_inf_nan=False)
    currency: CurrencyCode = "USD"
    stops: int = Field(ge=0)
    duration_minutes: int = Field(gt=0)
    airlines: list[str] = Field(default_factory=list)
    flight_numbers: list[str] = Field(default_factory=list)
    origin: AirportCode | None = None
    destination: AirportCode | None = None
    max_layover_minutes: int | None = Field(default=None, ge=0)
    departure_token: str | None = Field(default=None, repr=False)
