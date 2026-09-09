from decimal import Decimal

import pytest
from pydantic import ValidationError

from faresentry.models import (
    AirportTransfer,
    FlightItinerary,
    FlightOption,
    FlightSegment,
    Layover,
    RoundTripItinerary,
)


@pytest.fixture
def outbound() -> FlightItinerary:
    return FlightItinerary(
        segments=(
            FlightSegment(
                origin="LAX",
                destination="SFO",
                airline="United",
                flight_number="UA 100",
                duration_minutes=90,
            ),
            FlightSegment(
                origin="SFO",
                destination="HND",
                airline="ANA",
                flight_number="NH 107",
                duration_minutes=660,
            ),
        ),
        layovers=(Layover(airport="SFO", duration_minutes=120),),
    )


@pytest.fixture
def inbound() -> FlightItinerary:
    return FlightItinerary(
        segments=(
            FlightSegment(
                origin="HND",
                destination="LAX",
                airline="ANA",
                flight_number="NH 106",
                duration_minutes=600,
            ),
        )
    )


def test_direction_derives_metrics_from_segments_and_layovers(
    outbound: FlightItinerary, inbound: FlightItinerary
) -> None:
    assert (outbound.origin, outbound.destination) == ("LAX", "HND")
    assert outbound.duration_minutes == 870
    assert outbound.stops == 1
    assert outbound.airlines == ["United", "ANA"]
    assert outbound.flight_numbers == ["UA 100", "NH 107"]
    assert outbound.layovers[0].airport == "SFO"
    assert inbound.duration_minutes == 600
    assert inbound.stops == 0
    assert inbound.layovers == ()
    # Derived values and prices are not duplicated in serialized direction data.
    assert set(outbound.model_dump()) == {"segments", "layovers"}


def test_completed_round_trip_keeps_directions_and_total_price_separate(
    outbound: FlightItinerary, inbound: FlightItinerary
) -> None:
    trip = RoundTripItinerary(
        outbound=outbound, inbound=inbound, total_price=Decimal("900.25")
    )
    assert trip.total_price == Decimal("900.25")
    assert trip.currency == "USD"
    assert trip.outbound.stops == 1
    assert trip.inbound.stops == 0
    assert trip.outbound.duration_minutes == 870
    assert trip.inbound.duration_minutes == 600
    assert trip.airlines == ["United", "ANA"]
    assert trip.flight_numbers == ["UA 100", "NH 107", "NH 106"]
    assert RoundTripItinerary.model_validate_json(trip.model_dump_json()) == trip
    assert "departure_token" not in trip.model_dump_json()


def test_incomplete_choice_remains_backwards_compatible(
    outbound: FlightItinerary,
) -> None:
    option = FlightOption(
        airline="United / ANA",
        price=Decimal("900.25"),
        stops=1,
        duration_minutes=870,
        departure_token="synthetic-workflow-token",
    )
    assert option.price == Decimal("900.25")
    assert option.departure_token == "synthetic-workflow-token"
    assert option.departure_token not in repr(option)
    assert not isinstance(option, (FlightItinerary, RoundTripItinerary))
    assert (
        "Quoted total round-trip price"
        in (FlightOption.model_json_schema()["properties"]["price"]["description"])
    )
    # An outbound choice, even with a round-trip quote, cannot imply a return.
    with pytest.raises(ValidationError, match="inbound"):
        RoundTripItinerary.model_validate(
            {
                "outbound": outbound,
                "total_price": option.price,
            }
        )


@pytest.mark.parametrize("missing", ["outbound", "inbound", "total_price"])
def test_completed_trip_requires_both_directions_and_price(
    outbound: FlightItinerary, inbound: FlightItinerary, missing: str
) -> None:
    data = {"outbound": outbound, "inbound": inbound, "total_price": "900.25"}
    del data[missing]
    with pytest.raises(ValidationError, match=missing):
        RoundTripItinerary.model_validate(data)


@pytest.mark.parametrize("price", ["-1", "NaN", "Infinity"])
def test_invalid_round_trip_price(
    outbound: FlightItinerary, inbound: FlightItinerary, price: str
) -> None:
    with pytest.raises(ValidationError, match="total_price"):
        RoundTripItinerary.model_validate(
            {
                "outbound": outbound,
                "inbound": inbound,
                "total_price": price,
            }
        )


def test_round_trip_rejects_unrelated_return(outbound: FlightItinerary) -> None:
    with pytest.raises(ValidationError, match="reverse the outbound"):
        RoundTripItinerary(
            outbound=outbound, inbound=outbound, total_price=Decimal("900")
        )


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("origin", "LA"),
        ("destination", "LAX"),
        ("airline", "   "),
        ("flight_number", ""),
        ("duration_minutes", 0),
        ("duration_minutes", -1),
        ("duration_minutes", True),
        ("duration_minutes", 1.5),
    ],
)
def test_segment_validation(field: str, value: object) -> None:
    data = {
        "origin": "LAX",
        "destination": "HND",
        "airline": "ANA",
        "flight_number": "NH 105",
        "duration_minutes": 725,
    }
    with pytest.raises(ValidationError):
        FlightSegment.model_validate(data | {field: value})


def test_empty_direction_is_rejected() -> None:
    with pytest.raises(ValidationError, match="segments"):
        FlightItinerary(segments=())


def test_missing_or_extra_layovers_are_rejected(
    outbound: FlightItinerary, inbound: FlightItinerary
) -> None:
    with pytest.raises(ValidationError, match="one layover"):
        FlightItinerary(segments=outbound.segments)
    with pytest.raises(ValidationError, match="one layover"):
        FlightItinerary(segments=inbound.segments, layovers=outbound.layovers)


def test_disconnected_segments_are_rejected(outbound: FlightItinerary) -> None:
    final_segment = FlightSegment(
        origin="SEA",
        destination="HND",
        airline="ANA",
        flight_number="NH 107",
        duration_minutes=660,
    )
    with pytest.raises(ValidationError, match="same airport"):
        FlightItinerary(
            segments=(outbound.segments[0], final_segment), layovers=outbound.layovers
        )


def test_layover_airport_must_match(outbound: FlightItinerary) -> None:
    with pytest.raises(ValidationError, match="layover airport"):
        FlightItinerary(
            segments=outbound.segments,
            layovers=(Layover(airport="SEA", duration_minutes=120),),
        )


def test_negative_layover_is_rejected() -> None:
    with pytest.raises(ValidationError, match="duration_minutes"):
        Layover(airport="SFO", duration_minutes=-1)


def test_no_raw_provider_state_or_manual_metrics(
    outbound: FlightItinerary, inbound: FlightItinerary
) -> None:
    with pytest.raises(ValidationError, match="departure_token"):
        RoundTripItinerary.model_validate(
            {
                "outbound": outbound,
                "inbound": inbound,
                "total_price": "900",
                "departure_token": "provider-workflow-state",
            }
        )
    with pytest.raises(ValidationError, match="stops"):
        FlightItinerary.model_validate(outbound.model_dump() | {"stops": 0})


def test_validated_direction_cannot_be_mutated(outbound: FlightItinerary) -> None:
    with pytest.raises(ValidationError, match="frozen"):
        outbound.segments[0].destination = "SEA"
    with pytest.raises(ValidationError, match="frozen"):
        outbound.layovers = ()


@pytest.fixture
def transfer_segments() -> tuple[FlightSegment, FlightSegment]:
    return (
        FlightSegment(
            origin="LAX",
            destination="HND",
            airline="ANA",
            flight_number="NH 105",
            duration_minutes=725,
        ),
        FlightSegment(
            origin="NRT",
            destination="SIN",
            airline="Singapore Airlines",
            flight_number="SQ 637",
            duration_minutes=420,
        ),
    )


def test_same_airport_connection_endpoints(outbound: FlightItinerary) -> None:
    assert outbound.connections is outbound.layovers
    connection = outbound.connections[0]
    assert connection.arrival_airport == "SFO"
    assert connection.departure_airport == "SFO"
    assert connection.duration_minutes == 120
    assert connection.requires_airport_transfer is False


def test_valid_airport_transfer_and_serialization(
    transfer_segments: tuple[FlightSegment, FlightSegment],
) -> None:
    itinerary = FlightItinerary(
        segments=transfer_segments,
        layovers=(
            AirportTransfer(
                arrival_airport="HND",
                departure_airport="NRT",
                duration_minutes=300,
            ),
        ),
    )
    assert (itinerary.origin, itinerary.destination) == ("LAX", "SIN")
    assert itinerary.connections[0].requires_airport_transfer is True
    assert itinerary.duration_minutes == 1445
    assert itinerary.stops == 1
    assert itinerary.airlines == ["ANA", "Singapore Airlines"]
    assert itinerary.flight_numbers == ["NH 105", "SQ 637"]
    assert itinerary.model_dump(mode="json")["layovers"] == [
        {
            "arrival_airport": "HND",
            "departure_airport": "NRT",
            "duration_minutes": 300,
        }
    ]
    restored = FlightItinerary.model_validate_json(itinerary.model_dump_json())
    assert restored == itinerary
    assert isinstance(restored.connections[0], AirportTransfer)
    assert restored.connections[0].requires_airport_transfer is True


def test_airport_gap_requires_explicit_transfer(
    transfer_segments: tuple[FlightSegment, FlightSegment],
) -> None:
    with pytest.raises(ValidationError, match="one layover"):
        FlightItinerary(segments=transfer_segments)
    with pytest.raises(ValidationError, match="use AirportTransfer"):
        FlightItinerary(
            segments=transfer_segments,
            layovers=(Layover(airport="HND", duration_minutes=300),),
        )


@pytest.mark.parametrize(
    ("arrival", "departure"),
    [("KIX", "NRT"), ("HND", "KIX"), ("NRT", "HND")],
)
def test_transfer_must_match_both_adjacent_endpoints(
    transfer_segments: tuple[FlightSegment, FlightSegment],
    arrival: str,
    departure: str,
) -> None:
    with pytest.raises(ValidationError, match="transfer endpoints must match"):
        FlightItinerary(
            segments=transfer_segments,
            layovers=(
                AirportTransfer(
                    arrival_airport=arrival,
                    departure_airport=departure,
                    duration_minutes=300,
                ),
            ),
        )


def test_transfer_cannot_masquerade_as_ordinary_layover() -> None:
    with pytest.raises(ValidationError, match="endpoints must differ"):
        AirportTransfer(
            arrival_airport="HND",
            departure_airport="HND",
            duration_minutes=120,
        )


@pytest.mark.parametrize("duration", [0, 1500])
def test_multi_segment_mixed_connections_include_full_duration(
    transfer_segments: tuple[FlightSegment, FlightSegment],
    duration: int,
) -> None:
    itinerary = FlightItinerary(
        segments=(
            *transfer_segments,
            FlightSegment(
                origin="SIN",
                destination="SYD",
                airline="Singapore Airlines",
                flight_number="SQ 221",
                duration_minutes=480,
            ),
        ),
        layovers=(
            AirportTransfer(
                arrival_airport="HND",
                departure_airport="NRT",
                duration_minutes=duration,
            ),
            Layover(airport="SIN", duration_minutes=duration),
        ),
    )
    assert itinerary.duration_minutes == 1625 + 2 * duration
    assert itinerary.stops == 2
    assert [item.requires_airport_transfer for item in itinerary.connections] == [
        True,
        False,
    ]
    assert itinerary.airlines == ["ANA", "Singapore Airlines"]
    assert itinerary.flight_numbers == ["NH 105", "SQ 637", "SQ 221"]
    restored = FlightItinerary.model_validate_json(itinerary.model_dump_json())
    assert restored == itinerary
    assert isinstance(restored.connections[0], AirportTransfer)
    assert isinstance(restored.connections[1], Layover)


@pytest.mark.parametrize("duration", [-1, True, 1.5])
def test_transfer_duration_validation(duration: object) -> None:
    with pytest.raises(ValidationError, match="duration_minutes"):
        AirportTransfer.model_validate(
            {
                "arrival_airport": "HND",
                "departure_airport": "NRT",
                "duration_minutes": duration,
            }
        )


def test_transfer_endpoints_cannot_be_mutated() -> None:
    connection = AirportTransfer(
        arrival_airport="HND",
        departure_airport="NRT",
        duration_minutes=300,
    )
    with pytest.raises(ValidationError, match="frozen"):
        connection.departure_airport = "HND"
