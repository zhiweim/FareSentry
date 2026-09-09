import traceback
from copy import deepcopy
from datetime import date
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
    TripQuery,
)
from faresentry.providers.base import FlightProvider, FlightProviderError
from faresentry.providers.serpapi import SerpApiFlightProvider


@pytest.fixture
def query() -> TripQuery:
    return TripQuery(
        origin="LAX",
        destination="HND",
        outbound_date=date(2026, 10, 12),
        return_date=date(2026, 10, 27),
    )


@pytest.fixture
def http_get(monkeypatch: pytest.MonkeyPatch) -> Mock:
    mock = Mock(return_value=Mock(status_code=200))
    monkeypatch.setattr(requests, "get", mock)
    return mock


@pytest.fixture
def nonstop() -> dict[str, object]:
    # Synthetic, reduced example of the documented initial-search schema.
    return {
        "flights": [
            {
                "departure_airport": {"id": "LAX", "time": "2026-10-12 00:50"},
                "arrival_airport": {"id": "HND", "time": "2026-10-13 04:55"},
                "airline": "ANA",
                "flight_number": "NH 105",
                "duration": 725,
            }
        ],
        "total_duration": 725,
        "price": 768,
        "type": "Round trip",
        "departure_token": "synthetic-return-lookup-token",
    }


@pytest.fixture
def connecting() -> dict[str, object]:
    return {
        "flights": [
            {
                "departure_airport": {"id": origin},
                "arrival_airport": {"id": destination},
                "airline": airline,
                "flight_number": number,
                "duration": duration,
            }
            for origin, destination, airline, number, duration in [
                ("LAX", "SFO", "United", "UA 100", 90),
                ("SFO", "KIX", "ANA", "NH 107", 660),
                ("KIX", "HND", "ANA", "NH 999", 80),
            ]
        ],
        "layovers": [{"id": "SFO", "duration": 90}, {"id": "KIX", "duration": 180}],
        "total_duration": 1100,
        "price": "900.25",
        "departure_token": "synthetic-connecting-token",
    }


@pytest.fixture
def one_stop(connecting: dict[str, object]) -> dict[str, object]:
    result = deepcopy(connecting)
    result["flights"] = result["flights"][:2]
    result["flights"][1]["arrival_airport"]["id"] = "HND"
    result["layovers"] = result["layovers"][:1]
    result["total_duration"] = 840
    return result


@pytest.fixture
def airport_transfer(one_stop: dict[str, object]) -> dict[str, object]:
    result = deepcopy(one_stop)
    result["flights"][0]["arrival_airport"]["id"] = "HND"
    result["flights"][0]["duration"] = 725
    result["flights"][1]["departure_airport"]["id"] = "NRT"
    result["flights"][1]["arrival_airport"]["id"] = "SIN"
    result["flights"][1]["duration"] = 420
    result["layovers"] = [{"id": "HND", "duration": 300}]
    result["total_duration"] = 1445
    return result


def test_search_parameters_and_combined_normalization(
    query: TripQuery,
    http_get: Mock,
    nonstop: dict[str, object],
    connecting: dict[str, object],
) -> None:
    http_get.return_value.json.return_value = {
        "best_flights": [nonstop],
        "other_flights": [connecting],
    }
    provider: FlightProvider = SerpApiFlightProvider()
    options = provider.search(query)
    http_get.assert_called_once_with(
        "https://serpapi.com/search.json",
        params={
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
        },
        timeout=30,
        allow_redirects=False,
    )
    assert len(options) == 2
    assert all(isinstance(option, FlightOption) for option in options)
    direct, connection = options
    assert direct.price == Decimal("768")
    assert direct.currency == "USD"
    assert direct.airline == "ANA"
    assert direct.airlines == ["ANA"]
    assert direct.flight_numbers == ["NH 105"]
    assert (direct.origin, direct.destination) == ("LAX", "HND")
    assert direct.duration_minutes == 725
    assert direct.stops == 0
    assert direct.max_layover_minutes == 0
    assert direct.outbound == FlightItinerary(
        segments=(
            FlightSegment(
                origin="LAX",
                destination="HND",
                airline="ANA",
                flight_number="NH 105",
                duration_minutes=725,
            ),
        )
    )
    assert direct.departure_token == nonstop["departure_token"]
    assert direct.departure_token not in repr(direct)
    assert "return_flights" not in direct.model_dump()
    assert connection.price == Decimal("900.25")
    assert connection.airline == "United / ANA"
    assert connection.airlines == ["United", "ANA"]
    assert connection.flight_numbers == ["UA 100", "NH 107", "NH 999"]
    assert (connection.origin, connection.destination) == ("LAX", "HND")
    assert connection.duration_minutes == 1100
    assert connection.stops == 2
    assert connection.max_layover_minutes == 180
    assert [segment.duration_minutes for segment in connection.outbound.segments] == [
        90,
        660,
        80,
    ]
    assert [
        (segment.origin, segment.destination)
        for segment in connection.outbound.segments
    ] == [("LAX", "SFO"), ("SFO", "KIX"), ("KIX", "HND")]
    assert connection.outbound.connections == (
        Layover(airport="SFO", duration_minutes=90),
        Layover(airport="KIX", duration_minutes=180),
    )


@pytest.mark.parametrize("group", ["best_flights", "other_flights"])
def test_either_group_can_be_missing(
    query: TripQuery, http_get: Mock, nonstop: dict[str, object], group: str
) -> None:
    http_get.return_value.json.return_value = {group: [nonstop]}
    assert len(SerpApiFlightProvider().search(query)) == 1


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"best_flights": []},
        {"other_flights": []},
        {"best_flights": None, "other_flights": []},
    ],
)
def test_empty_results(query: TripQuery, http_get: Mock, payload: object) -> None:
    http_get.return_value.json.return_value = payload
    assert SerpApiFlightProvider().search(query) == []


@pytest.mark.parametrize("bad_group", [None, "invalid", {}, 12])
def test_bad_group_does_not_hide_valid_group(
    query: TripQuery, http_get: Mock, nonstop: dict[str, object], bad_group: object
) -> None:
    http_get.return_value.json.return_value = {
        "best_flights": bad_group,
        "other_flights": [nonstop],
    }
    assert len(SerpApiFlightProvider().search(query)) == 1


@pytest.mark.parametrize("bad_result", [None, "invalid", {}, {"flights": []}])
def test_malformed_result_does_not_hide_valid_result(
    query: TripQuery, http_get: Mock, nonstop: dict[str, object], bad_result: object
) -> None:
    http_get.return_value.json.return_value = {"best_flights": [bad_result, nonstop]}
    assert len(SerpApiFlightProvider().search(query)) == 1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("price", None),
        ("price", -10),
        ("price", "NaN"),
        ("price", "Infinity"),
        ("price", "not a price"),
        ("total_duration", None),
        ("total_duration", 0),
        ("total_duration", -1),
        ("total_duration", True),
        ("flights", [None]),
        ("flights", [{}]),
        ("flights", "invalid"),
        ("flights", [{"duration": 725}, None]),
    ],
)
def test_invalid_required_data_is_skipped(
    query: TripQuery,
    http_get: Mock,
    nonstop: dict[str, object],
    field: str,
    value: object,
) -> None:
    malformed = nonstop | {field: value}
    http_get.return_value.json.return_value = {"best_flights": [malformed, nonstop]}
    assert len(SerpApiFlightProvider().search(query)) == 1


@pytest.mark.parametrize(
    "layovers",
    [
        None,
        [],
        [{"duration": 90}],
        [{"duration": 90}, {}],
        [{"duration": 90}, {"duration": -1}],
        "invalid",
    ],
)
def test_incomplete_layovers_skip_only_the_affected_option(
    query: TripQuery, http_get: Mock, connecting: dict[str, object], layovers: object
) -> None:
    http_get.return_value.json.return_value = {
        "other_flights": [connecting | {"layovers": layovers}, connecting]
    }
    (option,) = SerpApiFlightProvider().search(query)
    assert option.stops == 2
    assert option.max_layover_minutes == 180


def test_missing_token_does_not_discard_a_complete_outbound(
    query: TripQuery, http_get: Mock, nonstop: dict[str, object]
) -> None:
    http_get.return_value.json.return_value = {
        "best_flights": [nonstop | {"departure_token": None}]
    }
    (option,) = SerpApiFlightProvider().search(query)
    assert option.airline == "ANA"
    assert len(option.outbound.segments) == 1
    assert option.departure_token is None


@pytest.mark.parametrize("duration", [0, 90, 1500])
def test_one_stop_layover_normalization(
    query: TripQuery, http_get: Mock, one_stop: dict[str, object], duration: int
) -> None:
    one_stop["layovers"][0]["duration"] = duration
    one_stop["total_duration"] = 750 + duration
    http_get.return_value.json.return_value = {"best_flights": [one_stop]}
    (option,) = SerpApiFlightProvider().search(query)
    assert option.outbound.connections == (
        Layover(airport="SFO", duration_minutes=duration),
    )
    assert option.stops == 1
    assert option.duration_minutes == 750 + duration
    assert option.max_layover_minutes == duration
    assert option.flight_numbers == ["UA 100", "NH 107"]


def test_airport_transfer_normalization_and_serialization(
    query: TripQuery, http_get: Mock, airport_transfer: dict[str, object]
) -> None:
    http_get.return_value.json.return_value = {"best_flights": [airport_transfer]}
    (option,) = SerpApiFlightProvider().search(query)
    assert option.outbound.connections == (
        AirportTransfer(
            arrival_airport="HND", departure_airport="NRT", duration_minutes=300
        ),
    )
    assert option.outbound.connections[0].requires_airport_transfer
    assert (option.origin, option.destination) == ("LAX", "SIN")
    assert (option.duration_minutes, option.stops, option.max_layover_minutes) == (
        1445,
        1,
        300,
    )
    assert option.outbound.model_dump()["layovers"] == (
        {"arrival_airport": "HND", "departure_airport": "NRT", "duration_minutes": 300},
    )
    restored = FlightOption.model_validate_json(option.model_dump_json())
    assert restored == option
    assert isinstance(restored.outbound.connections[0], AirportTransfer)
    assert restored.departure_token == airport_transfer["departure_token"]
    assert restored.departure_token not in repr(restored)


def test_multi_segment_with_transfer_and_ordinary_layover(
    query: TripQuery, http_get: Mock, connecting: dict[str, object]
) -> None:
    connecting["flights"][1]["departure_airport"]["id"] = "OAK"
    http_get.return_value.json.return_value = {"best_flights": [connecting]}
    (option,) = SerpApiFlightProvider().search(query)
    assert option.outbound.connections == (
        AirportTransfer(
            arrival_airport="SFO", departure_airport="OAK", duration_minutes=90
        ),
        Layover(airport="KIX", duration_minutes=180),
    )
    assert option.duration_minutes == 1100
    assert option.stops == 2
    assert option.airlines == ["United", "ANA"]
    restored = FlightOption.model_validate_json(option.model_dump_json())
    assert restored == option
    assert isinstance(restored.outbound.connections[1], Layover)


@pytest.mark.parametrize(
    "layovers",
    [
        None,
        [],
        [{}],
        [None],
        [{"duration": True}],
        [{"duration": "300"}],
        [{"duration": -1}],
        [{"id": "KIX", "duration": 300}],
        [{"duration": 300}, {"duration": 0}],
    ],
)
def test_transfer_requires_complete_consistent_connection_data(
    query: TripQuery,
    http_get: Mock,
    airport_transfer: dict[str, object],
    nonstop: dict[str, object],
    layovers: object,
) -> None:
    http_get.return_value.json.return_value = {
        "best_flights": [airport_transfer | {"layovers": layovers}, nonstop]
    }
    (option,) = SerpApiFlightProvider().search(query)
    assert option.stops == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("departure_airport", None),
        ("arrival_airport", {}),
        ("arrival_airport", {"id": "bad"}),
        ("airline", " "),
        ("flight_number", None),
        ("duration", None),
        ("duration", True),
        ("duration", 0),
        ("duration", -1),
        ("duration", "725"),
    ],
)
def test_incomplete_segment_is_not_silently_dropped(
    query: TripQuery,
    http_get: Mock,
    connecting: dict[str, object],
    nonstop: dict[str, object],
    field: str,
    value: object,
) -> None:
    connecting["flights"][1][field] = value
    http_get.return_value.json.return_value = {"best_flights": [connecting, nonstop]}
    (option,) = SerpApiFlightProvider().search(query)
    assert option.airline == "ANA"
    assert option.stops == 0


def test_inconsistent_total_duration_is_not_repaired(
    query: TripQuery, http_get: Mock, one_stop: dict[str, object]
) -> None:
    http_get.return_value.json.return_value = {
        "best_flights": [one_stop | {"total_duration": 900}, one_stop]
    }
    (option,) = SerpApiFlightProvider().search(query)
    assert option.duration_minutes == 840


def test_raw_response_does_not_escape_or_mutate_domain_data(
    query: TripQuery, http_get: Mock, one_stop: dict[str, object]
) -> None:
    one_stop["unknown_metadata"] = {"raw_only": "ignored"}
    one_stop["flights"][0]["airplane"] = "raw-only aircraft metadata"
    http_get.return_value.json.return_value = {"best_flights": [one_stop]}
    (option,) = SerpApiFlightProvider().search(query)
    before = option.model_dump_json()
    assert set(option.model_dump()) == {
        "outbound",
        "price",
        "currency",
        "departure_token",
    }
    assert set(option.outbound.model_dump()) == {"segments", "layovers"}
    assert set(option.outbound.segments[0].model_dump()) == {
        "origin",
        "destination",
        "airline",
        "flight_number",
        "duration_minutes",
    }
    assert "raw_only" not in before
    assert "airplane" not in before
    one_stop["flights"][0]["duration"] = 1
    one_stop["layovers"][0]["duration"] = 1
    assert option.model_dump_json() == before


@pytest.mark.parametrize("key", [None, "", "   "])
def test_missing_api_key(
    query: TripQuery, http_get: Mock, monkeypatch: pytest.MonkeyPatch, key: str | None
) -> None:
    if key is None:
        monkeypatch.delenv("SERPAPI_API_KEY")
    else:
        monkeypatch.setenv("SERPAPI_API_KEY", key)
    with pytest.raises(FlightProviderError, match="Set SERPAPI_API_KEY"):
        SerpApiFlightProvider().search(query)
    http_get.assert_not_called()


def test_non_usd_query_is_rejected(query: TripQuery, http_get: Mock) -> None:
    query.currency = "EUR"
    with pytest.raises(FlightProviderError, match="USD only"):
        SerpApiFlightProvider().search(query)
    http_get.assert_not_called()


@pytest.mark.parametrize(
    ("exception", "message"),
    [
        (requests.Timeout, "timed out"),
        (requests.ConnectionError, "connect"),
        (requests.RequestException, "connect"),
    ],
)
def test_request_failures_hide_credentials(
    query: TripQuery,
    http_get: Mock,
    exception: type[requests.RequestException],
    message: str,
) -> None:
    http_get.side_effect = exception("https://serpapi.com?api_key=test-secret")
    with pytest.raises(FlightProviderError, match=message) as caught:
        SerpApiFlightProvider().search(query)
    assert "test-secret" not in "".join(traceback.format_exception(caught.value))


@pytest.mark.parametrize("status", [301, 400, 401, 403, 429, 500])
def test_non_success_http_status(query: TripQuery, http_get: Mock, status: int) -> None:
    http_get.return_value.status_code = status
    with pytest.raises(FlightProviderError, match=f"HTTP {status}"):
        SerpApiFlightProvider().search(query)
    http_get.return_value.json.assert_not_called()


@pytest.mark.parametrize(
    "payload",
    [{"error": "Invalid key: test-secret"}, {"search_metadata": {"status": "Error"}}],
)
def test_api_error_response_is_safe(
    query: TripQuery, http_get: Mock, payload: object
) -> None:
    http_get.return_value.json.return_value = payload
    with pytest.raises(FlightProviderError, match="search error") as caught:
        SerpApiFlightProvider().search(query)
    assert "test-secret" not in str(caught.value)


@pytest.mark.parametrize("payload", [None, [], "invalid"])
def test_invalid_response_shape(
    query: TripQuery, http_get: Mock, payload: object
) -> None:
    http_get.return_value.json.return_value = payload
    with pytest.raises(FlightProviderError, match="invalid response format"):
        SerpApiFlightProvider().search(query)


def test_invalid_json_is_safe(query: TripQuery, http_get: Mock) -> None:
    http_get.return_value.json.side_effect = ValueError("test-secret")
    with pytest.raises(FlightProviderError, match="invalid JSON") as caught:
        SerpApiFlightProvider().search(query)
    assert "test-secret" not in "".join(traceback.format_exception(caught.value))
