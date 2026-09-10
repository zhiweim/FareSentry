"""Pure hard-constraint evaluation using only normalized domain data."""

from faresentry.models import (
    AirportTransfer,
    ConstraintEvaluation,
    ConstraintType,
    ConstraintViolation,
    FlightItinerary,
    HardTravelConstraints,
    RoundTripItinerary,
    TravelDirection,
)


def evaluate_hard_constraints(
    itinerary: RoundTripItinerary,
    constraints: HardTravelConstraints,
) -> ConstraintEvaluation:
    """Return every failure without modifying inputs or performing I/O.

    Order is outbound then inbound; within each direction, stops, total
    duration, then connections in travel order. Each connection is checked
    for duration before transfer permission. Equal-to-maximum values pass.
    """
    violations = tuple(
        violation
        for direction, flight in (
            ("outbound", itinerary.outbound),
            ("inbound", itinerary.inbound),
        )
        for violation in evaluate_direction_constraints(
            flight, constraints, direction=direction
        ).violations
    )
    return ConstraintEvaluation(passes=not violations, violations=violations)


def evaluate_direction_constraints(
    flight: FlightItinerary,
    constraints: HardTravelConstraints,
    *,
    direction: TravelDirection,
) -> ConstraintEvaluation:
    """Evaluate one normalized direction, including outbound lookup prescreening."""
    violations: list[ConstraintViolation] = []
    limits: tuple[tuple[ConstraintType, int, int | None, str], ...] = (
        (
            "max_stops_per_direction",
            flight.stops,
            constraints.max_stops_per_direction,
            "stops",
        ),
        (
            "max_duration_minutes_per_direction",
            flight.duration_minutes,
            constraints.max_duration_minutes_per_direction,
            "total duration (minutes)",
        ),
    )
    for constraint_type, actual, allowed, label in limits:
        if allowed is not None and actual > allowed:
            violations.append(
                ConstraintViolation(
                    constraint_type=constraint_type,
                    direction=direction,
                    actual_value=actual,
                    allowed_value=allowed,
                    explanation=(
                        f"{direction.capitalize()} {label}: {actual} exceeds "
                        f"the maximum of {allowed}."
                    ),
                )
            )

    for index, connection in enumerate(flight.connections):
        is_transfer = isinstance(connection, AirportTransfer)
        label = (
            f"airport transfer {connection.arrival_airport} to "
            f"{connection.departure_airport}"
            if is_transfer
            else f"layover at {connection.arrival_airport}"
        )
        context = f"{direction.capitalize()} connection {index + 1} ({label})"
        maximum = constraints.max_connection_duration_minutes
        if maximum is not None and connection.duration_minutes > maximum:
            violations.append(
                ConstraintViolation(
                    constraint_type="max_connection_duration_minutes",
                    direction=direction,
                    actual_value=connection.duration_minutes,
                    allowed_value=maximum,
                    explanation=(
                        f"{context}: {connection.duration_minutes} minutes "
                        f"exceeds the maximum of {maximum} minutes."
                    ),
                    connection_index=index,
                )
            )
        if is_transfer and not constraints.allow_airport_transfers:
            violations.append(
                ConstraintViolation(
                    constraint_type="allow_airport_transfers",
                    direction=direction,
                    actual_value=True,
                    allowed_value=False,
                    explanation=f"{context}: airport transfers are prohibited.",
                    connection_index=index,
                )
            )

    return ConstraintEvaluation(passes=not violations, violations=tuple(violations))
