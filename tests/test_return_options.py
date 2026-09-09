"""Synthetic return-response fixtures; all HTTP is mocked and blocked by conftest."""

import traceback
from copy import deepcopy
from decimal import Decimal
from unittest.mock import Mock

import pytest
import requests

from faresentry.models import (
    AirportTransfer,
    FlightItinerary,
    FlightOption,
    FlightSegment,
    Layover,
    RoundTripItinerary,
    TripQuery,
)
from faresentry.providers.base import FlightProvider, FlightProviderError
from faresentry.providers.serpapi import SerpApiFlightProvider


@pytest.fixture
def query() -> TripQuery:
    return TripQuery.model_validate(
        dict(
            origin="LAX",
            destination="HND",
            outbound_date="2026-10-12",
            return_date="2026-10-27",
        )
    )


@pytest.fixture
def outbound() -> FlightOption:
    return FlightOption(
        outbound=FlightItinerary(
            segments=(
                FlightSegment(
                    origin="LAX",
                    destination="HND",
                    airline="ANA",
                    flight_number="NH 105",
                    duration_minutes=725,
                ),
            )
        ),
        price=Decimal("768"),
        departure_token="synthetic-return-token+/=",
    )


@pytest.fixture
def http_get(monkeypatch: pytest.MonkeyPatch) -> Mock:
    mock = Mock(return_value=Mock(status_code=200))
    monkeypatch.setattr(requests, "get", mock)
    return mock


@pytest.fixture
def returning() -> dict[str, object]:
    # Reduced synthetic form of the official returning-flights example:
    # https://serpapi.com/google-flights-results#api-examples
    # flights is the return direction; price/type quote the entire round trip.
    return {
        "flights": [
            {
                "departure_airport": {"id": "HND", "time": "2026-10-27 18:00"},
                "arrival_airport": {"id": "LAX", "time": "2026-10-27 11:00"},
                "duration": 600,
                "airline": "ANA",
                "flight_number": "NH 106",
            }
        ],
        "total_duration": 600,
        "price": 950,
        "type": "Round trip",
        "booking_token": "synthetic-booking-token-not-retained",
    }


def test_second_request_preserves_context_and_uses_return_round_trip_price(
    query: TripQuery,
    http_get: Mock,
    returning: dict[str, object],
    capsys: pytest.CaptureFixture[str],
) -> None:
    initial = deepcopy(returning)
    initial["flights"][0].update(
        {
            "departure_airport": {"id": "LAX"},
            "arrival_airport": {"id": "HND"},
            "duration": 725,
            "flight_number": "NH 105",
        }
    )
    initial.update(total_duration=725, price=768, departure_token="selected-token")
    http_get.return_value.json.side_effect = [
        {"best_flights": [initial]},
        {"best_flights": [returning]},
    ]
    provider: FlightProvider = SerpApiFlightProvider()
    (selected,) = provider.search(query)
    (trip,) = provider.get_return_options(selected, query)
    assert http_get.call_count == 2
    first, second = http_get.call_args_list
    assert second.args == first.args == ("https://serpapi.com/search.json",)
    assert second.kwargs == first.kwargs | {
        "params": first.kwargs["params"] | {"departure_token": "selected-token"}
    }
    assert second.kwargs["params"] == {
        "api_key": "test-key-not-a-real-credential",
        "engine": "google_flights",
        "departure_id": "LAX",
        "arrival_id": "HND",
        "outbound_date": "2026-10-12",
        "return_date": "2026-10-27",
        "type": 1,
        "travel_class": 1,
        "currency": "USD",
        "hl": "en",
        "gl": "us",
        "departure_token": "selected-token",
    }
    assert trip.outbound is selected.outbound
    assert trip.total_price == Decimal("950")  # Not 768, nor 768 + 950.
    assert selected.price == Decimal("768")
    assert trip.currency == selected.currency == query.currency == "USD"
    assert trip.inbound == FlightItinerary(
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
    assert trip.inbound.connections == ()
    assert trip.inbound.stops == 0
    assert trip.flight_numbers == ["NH 105", "NH 106"]
    assert trip.airlines == ["ANA"]
    assert "selected-token" not in repr(selected)
    assert "token" not in trip.model_dump_json()
    assert "departure_airport" not in trip.model_dump_json()
    assert RoundTripItinerary.model_validate_json(trip.model_dump_json()) == trip
    assert capsys.readouterr() == ("", "")


def test_multiple_returns_keep_individual_total_quotes(
    query: TripQuery,
    outbound: FlightOption,
    http_get: Mock,
    returning: dict[str, object],
) -> None:
    http_get.return_value.json.return_value = {
        "best_flights": [returning],
        "other_flights": [returning | {"price": "1000.25"}],
    }
    trips = SerpApiFlightProvider().get_return_options(outbound, query)
    assert [trip.total_price for trip in trips] == [Decimal("950"), Decimal("1000.25")]
    assert all(trip.outbound is outbound.outbound for trip in trips)


@pytest.mark.parametrize("transfer", [False, True])
@pytest.mark.parametrize("duration", [0, 120, 1500])
def test_return_connections_use_the_same_typed_normalization(
    query: TripQuery,
    outbound: FlightOption,
    http_get: Mock,
    returning: dict[str, object],
    transfer: bool,
    duration: int,
) -> None:
    returning["flights"] = [
        dict(
            departure_airport={"id": "HND"},
            arrival_airport={"id": "SFO"},
            airline="ANA",
            flight_number="NH 108",
            duration=600,
        ),
        dict(
            departure_airport={"id": "OAK" if transfer else "SFO"},
            arrival_airport={"id": "LAX"},
            airline="United",
            flight_number="UA 100",
            duration=90,
        ),
    ]
    returning["layovers"] = [{"id": "SFO", "duration": duration}]
    returning["total_duration"] = 690 + duration
    http_get.return_value.json.return_value = {"other_flights": [returning]}
    (trip,) = SerpApiFlightProvider().get_return_options(outbound, query)
    expected = (
        AirportTransfer(
            arrival_airport="SFO", departure_airport="OAK", duration_minutes=duration
        )
        if transfer
        else Layover(airport="SFO", duration_minutes=duration)
    )
    assert trip.inbound.connections == (expected,)
    assert trip.inbound.duration_minutes == 690 + duration
    assert trip.inbound.stops == 1
    assert trip.inbound.airlines == ["ANA", "United"]
    assert trip.inbound.flight_numbers == ["NH 108", "UA 100"]
    assert trip.outbound is outbound.outbound


@pytest.mark.parametrize(
    "update",
    [
        {"price": None},
        {"price": -1},
        {"price": "NaN"},
        {"price": "Infinity"},
        {"price": "unknown"},
        {"flights": []},
        {"flights": [None]},
        {"total_duration": 601},
        {"layovers": [{"duration": 1}]},
        {"type": "One way"},
    ],
)
def test_malformed_returns_do_not_hide_valid_siblings(
    query: TripQuery,
    outbound: FlightOption,
    http_get: Mock,
    returning: dict[str, object],
    update: dict[str, object],
) -> None:
    http_get.return_value.json.return_value = {
        "best_flights": [None, "invalid", returning | update, returning],
    }
    (trip,) = SerpApiFlightProvider().get_return_options(outbound, query)
    assert trip.total_price == Decimal("950")


def test_return_with_wrong_endpoints_is_skipped(
    query: TripQuery,
    outbound: FlightOption,
    http_get: Mock,
    returning: dict[str, object],
) -> None:
    wrong = deepcopy(returning)
    wrong["flights"][0]["arrival_airport"]["id"] = "SFO"
    http_get.return_value.json.return_value = {"best_flights": [wrong, returning]}
    assert len(SerpApiFlightProvider().get_return_options(outbound, query)) == 1


@pytest.mark.parametrize(
    "payload",
    [{}, {"best_flights": []}, {"best_flights": None, "other_flights": "invalid"}],
)
def test_no_usable_returns(
    query: TripQuery, outbound: FlightOption, http_get: Mock, payload: dict[str, object]
) -> None:
    http_get.return_value.json.return_value = payload
    assert SerpApiFlightProvider().get_return_options(outbound, query) == []


@pytest.mark.parametrize("token", [None, "", "   "])
def test_missing_token_fails_before_http(
    query: TripQuery, outbound: FlightOption, http_get: Mock, token: str | None
) -> None:
    selected = outbound.model_copy(update={"departure_token": token})
    with pytest.raises(FlightProviderError, match="no return lookup token"):
        SerpApiFlightProvider().get_return_options(selected, query)
    http_get.assert_not_called()


@pytest.mark.parametrize(
    "update", [{"origin": "SFO"}, {"destination": "NRT"}, {"currency": "EUR"}]
)
def test_mismatched_context_fails_before_http(
    query: TripQuery, outbound: FlightOption, http_get: Mock, update: dict[str, str]
) -> None:
    with pytest.raises(FlightProviderError, match="does not match"):
        SerpApiFlightProvider().get_return_options(
            outbound, query.model_copy(update=update)
        )
    http_get.assert_not_called()


@pytest.mark.parametrize("failure", ["timeout", "connection", "http", "json", "api"])
def test_return_failures_are_safe(
    query: TripQuery,
    outbound: FlightOption,
    http_get: Mock,
    failure: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret_text = (
        "api_key=test-key-not-a-real-credential&"
        f"departure_token={outbound.departure_token}"
    )
    if failure == "timeout":
        http_get.side_effect = requests.Timeout(secret_text)
    elif failure == "connection":
        http_get.side_effect = requests.ConnectionError(secret_text)
    elif failure == "http":
        http_get.return_value.status_code = 429
    elif failure == "json":
        http_get.return_value.json.side_effect = ValueError(secret_text)
    else:
        http_get.return_value.json.return_value = {"error": secret_text}
    with pytest.raises(FlightProviderError) as caught:
        SerpApiFlightProvider().get_return_options(outbound, query)
    output = repr(caught.value) + "".join(traceback.format_exception(caught.value))
    assert outbound.departure_token not in output
    assert "test-key-not-a-real-credential" not in output
    assert capsys.readouterr() == ("", "")
