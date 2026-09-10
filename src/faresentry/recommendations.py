"""Deterministic recommendation inputs and output validation; no SDK or I/O."""

from collections.abc import Mapping
from decimal import ROUND_HALF_UP, Decimal
from itertools import permutations

from pydantic import BaseModel, ConfigDict, ValidationError

from faresentry.constraints import evaluate_hard_constraints
from faresentry.models import (
    AirportCode,
    CurrencyCode,
    FlightItinerary,
    FlightLabel,
    HardTravelConstraints,
    Recommendation,
    RoundTripItinerary,
    TravelerSoftPreferences,
)


class RecommendationError(RuntimeError):
    """Recommendation failed; the message is safe to display."""


class InvalidRecommendationError(RecommendationError):
    """The model did not return a valid choice from the supplied candidates."""


class _Summary(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class ConnectionSummary(_Summary):
    arrival_airport: AirportCode
    departure_airport: AirportCode
    duration_minutes: int
    requires_airport_transfer: bool


class DirectionSummary(_Summary):
    origin: AirportCode
    destination: AirportCode
    duration_minutes: int
    stops: int
    stops_above_preference: int | None
    connections: tuple[ConnectionSummary, ...]
    total_connection_minutes: int
    max_connection_minutes: int
    airport_transfer_count: int
    airlines: tuple[FlightLabel, ...]
    preferred_airline_matches: tuple[FlightLabel, ...]
    flight_numbers: tuple[FlightLabel, ...]


class CandidateSummary(_Summary):
    candidate_id: FlightLabel
    total_round_trip_price: Decimal
    currency: CurrencyCode
    outbound: DirectionSummary
    inbound: DirectionSummary
    total_travel_minutes: int
    total_stops: int
    total_connection_minutes: int
    max_connection_minutes: int
    airport_transfer_count: int
    airlines: tuple[FlightLabel, ...]
    flight_numbers: tuple[FlightLabel, ...]


class CandidateComparison(_Summary):
    """Signed candidate minus reference differences, computed in Python.

    Percent uses the reference price and is rounded to two decimal places,
    half up. It is undefined (None) when the reference price is zero.
    Both ordered directions are supplied so the model need not invert values.
    """

    candidate_id: FlightLabel
    reference_candidate_id: FlightLabel
    price_difference: Decimal
    price_difference_percent: Decimal | None
    outbound_duration_difference_minutes: int
    inbound_duration_difference_minutes: int
    total_travel_difference_minutes: int
    outbound_stops_difference: int
    inbound_stops_difference: int
    total_stops_difference: int
    total_connection_difference_minutes: int
    max_connection_difference_minutes: int
    airport_transfer_count_difference: int


class RecommendationRequest(_Summary):
    """Built from acceptable trips for one search; prices serialize as strings."""

    preferences: TravelerSoftPreferences
    candidates: tuple[CandidateSummary, ...]
    comparisons: tuple[CandidateComparison, ...]


def _summarize_direction(
    itinerary: FlightItinerary, preferences: TravelerSoftPreferences
) -> DirectionSummary:
    maximum = preferences.preferred_max_stops_per_direction
    preferred = {airline.casefold() for airline in preferences.preferred_airlines}
    connections = tuple(
        ConnectionSummary(
            arrival_airport=item.arrival_airport,
            departure_airport=item.departure_airport,
            duration_minutes=item.duration_minutes,
            requires_airport_transfer=item.requires_airport_transfer,
        )
        for item in itinerary.connections
    )
    return DirectionSummary(
        origin=itinerary.origin,
        destination=itinerary.destination,
        duration_minutes=itinerary.duration_minutes,
        stops=itinerary.stops,
        stops_above_preference=(
            None if maximum is None else max(0, itinerary.stops - maximum)
        ),
        connections=connections,
        total_connection_minutes=sum(item.duration_minutes for item in connections),
        max_connection_minutes=max(
            (item.duration_minutes for item in connections), default=0
        ),
        airport_transfer_count=sum(
            item.requires_airport_transfer for item in connections
        ),
        airlines=tuple(itinerary.airlines),
        preferred_airline_matches=tuple(
            airline for airline in itinerary.airlines if airline.casefold() in preferred
        ),
        flight_numbers=tuple(itinerary.flight_numbers),
    )


def _summarize_candidate(
    candidate_id: str,
    itinerary: RoundTripItinerary,
    preferences: TravelerSoftPreferences,
) -> CandidateSummary:
    outbound = _summarize_direction(itinerary.outbound, preferences)
    inbound = _summarize_direction(itinerary.inbound, preferences)
    return CandidateSummary(
        candidate_id=candidate_id,
        total_round_trip_price=itinerary.total_price,
        currency=itinerary.currency,
        outbound=outbound,
        inbound=inbound,
        total_travel_minutes=outbound.duration_minutes + inbound.duration_minutes,
        total_stops=outbound.stops + inbound.stops,
        total_connection_minutes=(
            outbound.total_connection_minutes + inbound.total_connection_minutes
        ),
        max_connection_minutes=max(
            outbound.max_connection_minutes, inbound.max_connection_minutes
        ),
        airport_transfer_count=(
            outbound.airport_transfer_count + inbound.airport_transfer_count
        ),
        airlines=tuple(itinerary.airlines),
        flight_numbers=tuple(itinerary.flight_numbers),
    )


def _compare(
    candidate: CandidateSummary, reference: CandidateSummary
) -> CandidateComparison:
    difference = candidate.total_round_trip_price - reference.total_round_trip_price
    return CandidateComparison(
        candidate_id=candidate.candidate_id,
        reference_candidate_id=reference.candidate_id,
        price_difference=difference,
        price_difference_percent=(
            (difference * 100 / reference.total_round_trip_price).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
            if reference.total_round_trip_price
            else None
        ),
        outbound_duration_difference_minutes=(
            candidate.outbound.duration_minutes - reference.outbound.duration_minutes
        ),
        inbound_duration_difference_minutes=(
            candidate.inbound.duration_minutes - reference.inbound.duration_minutes
        ),
        total_travel_difference_minutes=(
            candidate.total_travel_minutes - reference.total_travel_minutes
        ),
        outbound_stops_difference=candidate.outbound.stops - reference.outbound.stops,
        inbound_stops_difference=candidate.inbound.stops - reference.inbound.stops,
        total_stops_difference=candidate.total_stops - reference.total_stops,
        total_connection_difference_minutes=(
            candidate.total_connection_minutes - reference.total_connection_minutes
        ),
        max_connection_difference_minutes=(
            candidate.max_connection_minutes - reference.max_connection_minutes
        ),
        airport_transfer_count_difference=(
            candidate.airport_transfer_count - reference.airport_transfer_count
        ),
    )


def build_recommendation_request(
    acceptable_candidates: Mapping[str, RoundTripItinerary],
    preferences: TravelerSoftPreferences,
    *,
    constraints: HardTravelConstraints,
) -> RecommendationRequest:
    """Summarize a small set from the same search in mapping insertion order.

    Callers supply already-filtered candidates and their active constraints.
    Recheck defensively and reject the entire request if any candidate fails.
    Constraints never enter the model prompt. No currency conversion is done.
    Dates are absent from RoundTripItinerary, so matching search dates remain a
    caller precondition. IDs must be nonblank and have no surrounding whitespace.
    """
    if not acceptable_candidates:
        raise ValueError("At least one acceptable candidate is required")
    summaries: list[CandidateSummary] = []
    for candidate_id, itinerary in acceptable_candidates.items():
        if (
            not isinstance(candidate_id, str)
            or not candidate_id.strip()
            or candidate_id != candidate_id.strip()
        ):
            raise ValueError("Candidate IDs must be nonblank without outer whitespace")
        if not evaluate_hard_constraints(itinerary, constraints).passes:
            raise ValueError("All candidates must pass the active hard constraints")
        summaries.append(_summarize_candidate(candidate_id, itinerary, preferences))
    if len({item.currency for item in summaries}) != 1:
        raise ValueError("Candidates must use the same currency")
    if (
        len({(item.outbound.origin, item.outbound.destination) for item in summaries})
        != 1
    ):
        raise ValueError("Candidates must use the same round-trip endpoints")
    return RecommendationRequest(
        preferences=preferences,
        candidates=tuple(summaries),
        comparisons=tuple(_compare(a, b) for a, b in permutations(summaries, 2)),
    )


def parse_recommendation(
    output: object, *, candidate_ids: tuple[str, ...]
) -> Recommendation:
    """Validate all machine-readable references in a structured object or JSON.

    This is the request-aware boundary used by the adapter. Pydantic validates
    category and reference relationships; this function validates membership in
    the actual request. Prose is display-only and is never parsed for decisions.
    """
    try:
        if isinstance(output, Recommendation):
            # Revalidate even instances created with model_construct/model_copy.
            output = output.model_dump(warnings=False)
        if isinstance(output, str):
            recommendation = Recommendation.model_validate_json(output)
        else:
            recommendation = Recommendation.model_validate(output)
    except ValidationError:
        raise InvalidRecommendationError(
            "Model returned an invalid recommendation"
        ) from None
    if recommendation.selected_candidate_id not in candidate_ids:
        raise InvalidRecommendationError("Model selected an unknown candidate ID")
    allowed = set(candidate_ids)
    for tradeoff in recommendation.key_tradeoffs:
        if not allowed.issuperset(tradeoff.candidate_ids) or (
            tradeoff.favored_candidate_id is not None
            and tradeoff.favored_candidate_id not in allowed
        ):
            raise InvalidRecommendationError(
                "Model tradeoff references an unknown candidate ID"
            )
    return recommendation
