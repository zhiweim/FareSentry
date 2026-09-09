from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from faresentry.models import FlightOption, TripQuery


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


def test_flight_option_preserves_decimal_price() -> None:
    option = FlightOption(
        airline="Example Air",
        price=Decimal("999.99"),
        stops=0,
        duration_minutes=660,
    )
    assert option.price == Decimal("999.99")
    assert option.currency == "USD"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price", "-1"),
        ("price", "NaN"),
        ("price", "Infinity"),
        ("stops", -1),
        ("stops", 1.5),
        ("duration_minutes", 0),
        ("duration_minutes", -10),
        ("airline", ""),
        ("currency", "usd"),
    ],
)
def test_flight_option_rejects_invalid_values(
    field: str, value: str | int | float
) -> None:
    data = {
        "airline": "Example Air",
        "price": "999.99",
        "stops": 0,
        "duration_minutes": 660,
    }
    with pytest.raises(ValidationError, match=field):
        FlightOption.model_validate(data | {field: value})
