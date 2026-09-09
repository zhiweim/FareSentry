from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from faresentry.models import FlightItinerary, FlightOption, FlightSegment, TripQuery


@pytest.fixture
def outbound() -> FlightItinerary:
    return FlightItinerary(
        segments=(
            FlightSegment(
                origin="LAX",
                destination="HND",
                airline="Example Air",
                flight_number="EA 1",
                duration_minutes=660,
            ),
        )
    )


def test_trip_query_parses_dates() -> None:
    query = TripQuery.model_validate(
        {
            "origin": "SFO",
            "destination": "NRT",
            "outbound_date": "2026-12-01",
            "return_date": "2026-12-15",
        }
    )
    assert query.outbound_date == date(2026, 12, 1)
    assert query.return_date == date(2026, 12, 15)
    assert query.currency == "USD"


@pytest.mark.parametrize(
    ("updates", "message"),
    [
        ({"return_date": "2026-11-30"}, "return_date must be on or after"),
        ({"destination": "SFO"}, "origin and destination must differ"),
        ({"origin": "SF"}, "origin"),
        ({"destination": "nrt"}, "destination"),
        ({"currency": "US"}, "currency"),
    ],
)
def test_trip_query_rejects_invalid_input(
    updates: dict[str, str], message: str
) -> None:
    data = {
        "origin": "SFO",
        "destination": "NRT",
        "outbound_date": "2026-12-01",
        "return_date": "2026-12-15",
    }
    with pytest.raises(ValidationError, match=message):
        TripQuery.model_validate(data | updates)


def test_flight_option_preserves_decimal_price(outbound: FlightItinerary) -> None:
    option = FlightOption(
        outbound=outbound,
        price=Decimal("999.99"),
    )
    assert option.price == Decimal("999.99")
    assert option.currency == "USD"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price", "-1"),
        ("price", "NaN"),
        ("price", "Infinity"),
        ("currency", "usd"),
    ],
)
def test_flight_option_rejects_invalid_values(
    outbound: FlightItinerary, field: str, value: str
) -> None:
    data = {
        "outbound": outbound,
        "price": "999.99",
    }
    with pytest.raises(ValidationError, match=field):
        FlightOption.model_validate(data | {field: value})


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("stops", 1),
        ("duration_minutes", 700),
        ("airline", "Different Air"),
        ("airlines", ["Different Air"]),
        ("flight_numbers", ["EA 2"]),
        ("origin", "SFO"),
        ("destination", "NRT"),
        ("max_layover_minutes", 90),
    ],
)
def test_summary_values_cannot_override_outbound(
    outbound: FlightItinerary, field: str, value: object
) -> None:
    with pytest.raises(ValidationError, match=field):
        FlightOption.model_validate({"outbound": outbound, "price": 999, field: value})
    option = FlightOption(outbound=outbound, price=Decimal("999"))
    with pytest.raises(ValidationError, match="frozen"):
        setattr(option, field, value)


def test_option_requires_typed_outbound() -> None:
    with pytest.raises(ValidationError, match="outbound"):
        FlightOption.model_validate({"price": 999})


def test_option_summaries_are_derived_and_round_trip_serializes(
    outbound: FlightItinerary,
) -> None:
    option = FlightOption(outbound=outbound, price=Decimal("999"))
    option.airlines.append("Different Air")
    option.flight_numbers.clear()
    assert option.airlines == ["Example Air"]
    assert option.flight_numbers == ["EA 1"]
    assert option.airline == "Example Air"
    assert (option.origin, option.destination) == ("LAX", "HND")
    assert (option.duration_minutes, option.stops, option.max_layover_minutes) == (
        660,
        0,
        0,
    )
    assert set(option.model_dump()) == {
        "outbound",
        "price",
        "currency",
        "departure_token",
    }
    assert FlightOption.model_validate_json(option.model_dump_json()) == option
