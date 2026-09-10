from decimal import Decimal

import pytest
from pydantic import ValidationError

from faresentry.constraints import evaluate_hard_constraints
from faresentry.models import (
    AirportTransfer,
    ConstraintEvaluation,
    FlightItinerary,
    FlightSegment,
    HardTravelConstraints,
    Layover,
    RoundTripItinerary,
    TravelDirection,
)


def make_direction(
    direction: TravelDirection,
    connections: tuple[Layover | AirportTransfer, ...] = (),
) -> FlightItinerary:
    origin, destination = ("LAX", "SIN") if direction == "outbound" else ("SIN", "LAX")
    origins = [origin, *(item.departure_airport for item in connections)]
    destinations = [*(item.arrival_airport for item in connections), destination]
    return FlightItinerary(
        segments=tuple(
            FlightSegment(
                origin=start,
                destination=end,
                airline="Example Air",
                flight_number=f"EA {index + 1}",
                duration_minutes=60,
            )
            for index, (start, end) in enumerate(zip(origins, destinations))
        ),
        layovers=connections,
    )


def make_trip(
    direction: TravelDirection = "outbound",
    connections: tuple[Layover | AirportTransfer, ...] = (),
) -> RoundTripItinerary:
    return RoundTripItinerary(
        outbound=make_direction(
            "outbound", connections if direction == "outbound" else ()
        ),
        inbound=make_direction(
            "inbound", connections if direction == "inbound" else ()
        ),
        total_price=Decimal("900"),
    )


@pytest.fixture
def mixed_connections() -> tuple[Layover | AirportTransfer, ...]:
    return (
        AirportTransfer(
            arrival_airport="HND", departure_airport="NRT", duration_minutes=90
        ),
        Layover(airport="ICN", duration_minutes=120),
        AirportTransfer(
            arrival_airport="JFK", departure_airport="LGA", duration_minutes=150
        ),
    )


@pytest.fixture
def mixed_trip(
    mixed_connections: tuple[Layover | AirportTransfer, ...],
) -> RoundTripItinerary:
    return RoundTripItinerary(
        outbound=make_direction("outbound", mixed_connections),
        inbound=make_direction("inbound", mixed_connections),
        total_price=Decimal("900"),
    )


def test_all_constraints_passing(mixed_trip: RoundTripItinerary) -> None:
    result = evaluate_hard_constraints(
        mixed_trip,
        HardTravelConstraints(
            max_stops_per_direction=4,
            max_duration_minutes_per_direction=700,
            max_connection_duration_minutes=180,
            allow_airport_transfers=True,
        ),
    )
    assert result.passes is True
    assert result.violations == ()


def test_default_constraints_are_unrestricted(mixed_trip: RoundTripItinerary) -> None:
    assert evaluate_hard_constraints(mixed_trip, HardTravelConstraints()).passes


@pytest.mark.parametrize("direction", ["outbound", "inbound"])
@pytest.mark.parametrize(
    ("constraint_type", "maximum", "actual"),
    [
        ("max_stops_per_direction", 0, 1),
        ("max_duration_minutes_per_direction", 149, 150),
    ],
)
def test_direction_limits(
    direction: TravelDirection, constraint_type: str, maximum: int, actual: int
) -> None:
    trip = make_trip(direction, (Layover(airport="HND", duration_minutes=30),))
    result = evaluate_hard_constraints(
        trip, HardTravelConstraints.model_validate({constraint_type: maximum})
    )
    assert result.passes is False
    assert len(result.violations) == 1
    violation = result.violations[0]
    assert violation.constraint_type == constraint_type
    assert violation.direction == direction
    assert violation.actual_value == actual
    assert violation.allowed_value == maximum
    assert violation.connection_index is None
    assert violation.explanation.startswith(direction.capitalize())
    assert f"{actual} exceeds the maximum of {maximum}" in violation.explanation


@pytest.mark.parametrize("direction", ["outbound", "inbound"])
@pytest.mark.parametrize(
    "connection",
    [
        Layover(airport="HND", duration_minutes=121),
        AirportTransfer(
            arrival_airport="HND", departure_airport="NRT", duration_minutes=121
        ),
    ],
    ids=["layover", "airport_transfer"],
)
def test_excessive_connection_duration(
    direction: TravelDirection, connection: Layover | AirportTransfer
) -> None:
    result = evaluate_hard_constraints(
        make_trip(direction, (connection,)),
        HardTravelConstraints(max_connection_duration_minutes=120),
    )
    assert result.passes is False
    assert len(result.violations) == 1
    violation = result.violations[0]
    assert violation.constraint_type == "max_connection_duration_minutes"
    assert violation.direction == direction
    assert violation.actual_value == 121
    assert violation.allowed_value == 120
    assert violation.connection_index == 0
    assert "HND" in violation.explanation
    assert "121 minutes exceeds the maximum of 120 minutes" in violation.explanation
    if isinstance(connection, AirportTransfer):
        assert "airport transfer HND to NRT" in violation.explanation
    else:
        assert "layover at HND" in violation.explanation


@pytest.mark.parametrize("direction", ["outbound", "inbound"])
def test_prohibited_airport_transfer(direction: TravelDirection) -> None:
    result = evaluate_hard_constraints(
        make_trip(
            direction,
            (
                AirportTransfer(
                    arrival_airport="HND",
                    departure_airport="NRT",
                    duration_minutes=0,
                ),
            ),
        ),
        HardTravelConstraints(allow_airport_transfers=False),
    )
    assert result.passes is False
    assert len(result.violations) == 1
    violation = result.violations[0]
    assert violation.constraint_type == "allow_airport_transfers"
    assert violation.direction == direction
    assert violation.actual_value is True
    assert violation.allowed_value is False
    assert violation.connection_index == 0
    assert violation.explanation == (
        f"{direction.capitalize()} connection 1 (airport transfer HND to NRT): "
        "airport transfers are prohibited."
    )


def test_multiple_failures_include_every_connection_in_both_directions(
    mixed_trip: RoundTripItinerary,
) -> None:
    constraints = HardTravelConstraints(
        max_stops_per_direction=0,
        max_duration_minutes_per_direction=500,
        max_connection_duration_minutes=60,
        allow_airport_transfers=False,
    )
    original_trip = mixed_trip.model_dump()
    original_constraints = constraints.model_dump()
    result = evaluate_hard_constraints(mixed_trip, constraints)
    assert result.passes is False
    assert [
        (
            item.direction,
            item.constraint_type,
            item.connection_index,
            item.actual_value,
            item.allowed_value,
        )
        for item in result.violations
    ] == [
        (direction, rule, index, actual, allowed)
        for direction in ("outbound", "inbound")
        for rule, index, actual, allowed in (
            ("max_stops_per_direction", None, 3, 0),
            ("max_duration_minutes_per_direction", None, 600, 500),
            ("max_connection_duration_minutes", 0, 90, 60),
            ("allow_airport_transfers", 0, True, False),
            ("max_connection_duration_minutes", 1, 120, 60),
            ("max_connection_duration_minutes", 2, 150, 60),
            ("allow_airport_transfers", 2, True, False),
        )
    ]
    assert evaluate_hard_constraints(mixed_trip, constraints) == result
    assert mixed_trip.model_dump() == original_trip
    assert constraints.model_dump() == original_constraints
    assert ConstraintEvaluation.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize("direction", ["outbound", "inbound"])
def test_exact_boundaries(direction: TravelDirection) -> None:
    trip = make_trip(
        direction,
        (
            Layover(airport="ICN", duration_minutes=120),
            AirportTransfer(
                arrival_airport="HND", departure_airport="NRT", duration_minutes=120
            ),
        ),
    )
    result = evaluate_hard_constraints(
        trip,
        HardTravelConstraints(
            max_stops_per_direction=2,
            max_duration_minutes_per_direction=420,
            max_connection_duration_minutes=120,
            allow_airport_transfers=True,
        ),
    )
    assert result.passes is True
    assert result.violations == ()


def test_nonstop_and_zero_connection_limits() -> None:
    assert evaluate_hard_constraints(
        make_trip(),
        HardTravelConstraints(
            max_stops_per_direction=0,
            max_duration_minutes_per_direction=60,
            max_connection_duration_minutes=0,
            allow_airport_transfers=False,
        ),
    ).passes
    assert evaluate_hard_constraints(
        make_trip(connections=(Layover(airport="HND", duration_minutes=0),)),
        HardTravelConstraints(
            max_connection_duration_minutes=0, allow_airport_transfers=False
        ),
    ).passes


@pytest.mark.parametrize(
    "field",
    [
        "max_stops_per_direction",
        "max_duration_minutes_per_direction",
        "max_connection_duration_minutes",
    ],
)
@pytest.mark.parametrize("value", [-1, True, 1.5, "120"])
def test_invalid_numeric_constraint(field: str, value: object) -> None:
    with pytest.raises(ValidationError, match=field):
        HardTravelConstraints.model_validate({field: value})


@pytest.mark.parametrize("value", [0, 1, "false", None])
def test_transfer_permission_requires_boolean(value: object) -> None:
    with pytest.raises(ValidationError, match="allow_airport_transfers"):
        HardTravelConstraints.model_validate({"allow_airport_transfers": value})


def test_unknown_constraint_rejected() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        HardTravelConstraints.model_validate({"max_stops": 1})


def test_evaluation_rejects_inconsistent_pass_flag() -> None:
    with pytest.raises(ValidationError, match="passes must be true"):
        ConstraintEvaluation(passes=False)
    failure = evaluate_hard_constraints(
        make_trip(), HardTravelConstraints(max_duration_minutes_per_direction=0)
    )
    with pytest.raises(ValidationError, match="passes must be true"):
        ConstraintEvaluation(passes=True, violations=failure.violations)
