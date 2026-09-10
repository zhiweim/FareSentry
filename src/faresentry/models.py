"""Validated domain data, independent of providers and agent judgment."""

from datetime import date
from decimal import Decimal
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    StringConstraints,
    model_validator,
)

AirportCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
CurrencyCode = Annotated[str, StringConstraints(pattern=r"^[A-Z]{3}$")]
FlightLabel = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

TravelDirection = Literal["outbound", "inbound"]
ConstraintType = Literal[
    "max_stops_per_direction",
    "max_duration_minutes_per_direction",
    "max_connection_duration_minutes",
    "allow_airport_transfers",
]


class TravelerSoftPreferences(BaseModel):
    """Subjective priorities; none of these fields excludes an itinerary.

    Willingness to pay more is qualitative, not a budget or a price limit.
    Airline names should use the same labels as the normalized itineraries.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    preferred_max_stops_per_direction: int | None = Field(
        default=None, ge=0, strict=True
    )
    prefer_shorter_total_travel_time: bool = Field(default=True, strict=True)
    prefer_shorter_connections: bool = Field(default=True, strict=True)
    dislike_airport_transfers: bool = Field(default=True, strict=True)
    preferred_airlines: tuple[FlightLabel, ...] = ()
    willingness_to_pay_more: Literal["none", "low", "moderate", "high"] = "moderate"


JudgmentCategory = Literal[
    "price",
    "total_travel_time",
    "stops",
    "connections",
    "airport_transfer",
    "airline_preference",
    "overall_value",
]


class RecommendationTradeoff(BaseModel):
    """Typed subjective reasoning; explanation is for display only.

    candidate_ids names the distinct candidates discussed. A null favored ID
    means no candidate is favored on this dimension (e.g. a tie or no preference).
    overall_value represents the final synthesis; other categories may favor an
    alternative to the final selection. Request membership is checked separately.
    Downstream decisions must use these structured fields, never parse explanation.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    category: JudgmentCategory
    candidate_ids: tuple[FlightLabel, ...] = Field(min_length=1)
    favored_candidate_id: FlightLabel | None = Field(
        description="A discussed candidate, or null when none is favored."
    )
    explanation: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=500)
    ] = Field(description="Display-only explanation; never parse for decision logic.")

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        if len(set(self.candidate_ids)) != len(self.candidate_ids):
            raise ValueError("Tradeoff candidate IDs must be distinct")
        if (
            self.favored_candidate_id is not None
            and self.favored_candidate_id not in self.candidate_ids
        ):
            raise ValueError("Favored candidate must be among the discussed candidates")
        return self


class Recommendation(BaseModel):
    """Subjective choice among supplied candidates, never a constraint decision.

    Confidence describes strength of preference, not a calibrated probability
    or a prediction of future fares. Candidate membership is checked separately.
    selected_candidate_id is the authoritative final target; any overall_value
    tradeoff must agree. Decisions consume typed tradeoffs, selection and confidence.
    recommendation and tradeoff explanations are display-only, never decision inputs.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    selected_candidate_id: FlightLabel
    recommendation: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)
    ] = Field(
        description="Display-only recommendation; never parse for decision logic."
    )
    key_tradeoffs: tuple[RecommendationTradeoff, ...] = Field(
        min_length=1, max_length=6
    )
    confidence: Literal["low", "medium", "high"]

    @model_validator(mode="after")
    def validate_final_target(self) -> Self:
        for tradeoff in self.key_tradeoffs:
            if (
                tradeoff.category == "overall_value"
                and tradeoff.favored_candidate_id != self.selected_candidate_id
            ):
                raise ValueError("Overall value must favor the selected candidate")
        return self


class HardTravelConstraints(BaseModel):
    """Inclusive limits applied independently to both directions.

    None disables a numeric limit. Durations are in whole minutes; total
    direction duration includes all flights and connections. Airport transfers
    are allowed by default, so an empty configuration imposes no restrictions.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    max_stops_per_direction: int | None = Field(default=None, ge=0, strict=True)
    max_duration_minutes_per_direction: int | None = Field(
        default=None, ge=0, strict=True
    )
    max_connection_duration_minutes: int | None = Field(default=None, ge=0, strict=True)
    allow_airport_transfers: bool = Field(default=True, strict=True)


class ConstraintViolation(BaseModel):
    """A failed rule; connection_index is zero-based within its direction.

    Numeric values use stops or minutes according to constraint_type. For
    allow_airport_transfers, actual_value=True means a transfer is present
    and allowed_value=False means transfers are prohibited.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    constraint_type: ConstraintType
    direction: TravelDirection
    actual_value: StrictInt | StrictBool
    allowed_value: StrictInt | StrictBool
    explanation: FlightLabel
    connection_index: int | None = Field(default=None, ge=0, strict=True)


class ConstraintEvaluation(BaseModel):
    """All failures from an evaluation, with a consistent pass/fail flag."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    passes: bool = Field(strict=True)
    violations: tuple[ConstraintViolation, ...] = ()

    @model_validator(mode="after")
    def validate_passes(self) -> Self:
        if self.passes != (not self.violations):
            raise ValueError("passes must be true exactly when there are no violations")
        return self


class FlightSegment(BaseModel):
    """One flight between two airports; duration excludes connection time."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    origin: AirportCode
    destination: AirportCode
    airline: FlightLabel
    flight_number: FlightLabel
    duration_minutes: int = Field(gt=0, strict=True)

    @model_validator(mode="after")
    def validate_route(self) -> Self:
        if self.origin == self.destination:
            raise ValueError("segment origin and destination must differ")
        return self


class Layover(BaseModel):
    """Time spent at a connection airport between consecutive flight segments."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    airport: AirportCode
    duration_minutes: int = Field(ge=0, strict=True)

    @property
    def arrival_airport(self) -> str:
        return self.airport

    @property
    def departure_airport(self) -> str:
        return self.airport

    @property
    def requires_airport_transfer(self) -> bool:
        return False


class AirportTransfer(BaseModel):
    """An explicit airport change between flights.

    Duration is the entire connection interval, including ground travel and
    waiting. It is not an extra flight segment or an additional flight stop.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    arrival_airport: AirportCode
    departure_airport: AirportCode
    duration_minutes: int = Field(ge=0, strict=True)

    @model_validator(mode="after")
    def validate_airport_change(self) -> Self:
        if self.arrival_airport == self.departure_airport:
            raise ValueError("airport transfer endpoints must differ; use Layover")
        return self

    @property
    def requires_airport_transfer(self) -> bool:
        return True


class FlightItinerary(BaseModel):
    """One direction with fully specified segments and explicit connections.

    Connection i explains the gap between segments i and i + 1. The existing
    layovers field holds both same-airport layovers and airport transfers.
    No price is assigned to an individual direction.
    Metrics are derived properties, so they cannot disagree with the segments.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    segments: tuple[FlightSegment, ...] = Field(min_length=1)
    layovers: tuple[Layover | AirportTransfer, ...] = ()

    @model_validator(mode="after")
    def validate_connections(self) -> Self:
        if len(self.layovers) != len(self.segments) - 1:
            raise ValueError(
                "one layover or airport transfer \
                is required between each pair of segments"
            )
        for previous, following, connection in zip(
            self.segments, self.segments[1:], self.layovers
        ):
            if isinstance(connection, Layover):
                if previous.destination != following.origin:
                    raise ValueError(
                        "ordinary layovers require the same airport; "
                        "use AirportTransfer to explain an airport change"
                    )
                if connection.airport != previous.destination:
                    raise ValueError(
                        "layover airport must match the connection airport"
                    )
            elif (
                connection.arrival_airport != previous.destination
                or connection.departure_airport != following.origin
            ):
                raise ValueError("transfer endpoints must match the adjacent flights")
        return self

    @property
    def connections(self) -> tuple[Layover | AirportTransfer, ...]:
        """Ordered connection data; an alias, not a duplicate collection."""
        return self.layovers

    @property
    def origin(self) -> str:
        return self.segments[0].origin

    @property
    def destination(self) -> str:
        return self.segments[-1].destination

    @property
    def duration_minutes(self) -> int:
        return sum(segment.duration_minutes for segment in self.segments) + sum(
            connection.duration_minutes for connection in self.connections
        )

    @property
    def stops(self) -> int:
        return len(self.segments) - 1

    @property
    def airlines(self) -> list[str]:
        return list(dict.fromkeys(segment.airline for segment in self.segments))

    @property
    def flight_numbers(self) -> list[str]:
        return [segment.flight_number for segment in self.segments]


class RoundTripItinerary(BaseModel):
    """Selected outbound and return (inbound) directions with one total price.

    Complete means both directions are specified, not that a booking exists.
    This initial model supports a return between the same two endpoint airports.
    Provider lookup tokens belong to search workflow state, not this itinerary.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    outbound: FlightItinerary
    inbound: FlightItinerary
    total_price: Decimal = Field(ge=0, allow_inf_nan=False)
    currency: CurrencyCode = "USD"

    @model_validator(mode="after")
    def validate_return_route(self) -> Self:
        if (
            self.outbound.origin != self.inbound.destination
            or self.outbound.destination != self.inbound.origin
            or self.outbound.origin == self.outbound.destination
        ):
            raise ValueError("inbound must reverse the outbound endpoint airports")
        return self

    @property
    def airlines(self) -> list[str]:
        return list(dict.fromkeys(self.outbound.airlines + self.inbound.airlines))

    @property
    def flight_numbers(self) -> list[str]:
        return self.outbound.flight_numbers + self.inbound.flight_numbers


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

    model_config = ConfigDict(frozen=True, extra="forbid")

    outbound: FlightItinerary
    price: Decimal = Field(
        ge=0,
        allow_inf_nan=False,
        description="Quoted total round-trip price; the return has not been selected.",
    )
    currency: CurrencyCode = "USD"
    departure_token: str | None = Field(
        default=None,
        repr=False,
        description="Opaque provider workflow state retained for return lookup.",
    )

    @property
    def airline(self) -> str:
        return " / ".join(self.airlines)

    @property
    def airlines(self) -> list[str]:
        return self.outbound.airlines

    @property
    def flight_numbers(self) -> list[str]:
        return self.outbound.flight_numbers

    @property
    def origin(self) -> str:
        return self.outbound.origin

    @property
    def destination(self) -> str:
        return self.outbound.destination

    @property
    def duration_minutes(self) -> int:
        return self.outbound.duration_minutes

    @property
    def stops(self) -> int:
        return self.outbound.stops

    @property
    def max_layover_minutes(self) -> int:
        return max(
            (connection.duration_minutes for connection in self.outbound.connections),
            default=0,
        )
