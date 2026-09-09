import traceback
from datetime import date
from decimal import Decimal
from unittest.mock import Mock

import pytest
import requests

from faresentry.models import FlightOption, TripQuery
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
            }
            for origin, destination, airline, number in [
                ("LAX", "SFO", "United", "UA 100"),
                ("SFO", "KIX", "ANA", "NH 107"),
                ("KIX", "HND", "ANA", "NH 999"),
            ]
        ],
        "layovers": [{"duration": 90}, {"duration": 180}],
        "total_duration": 1100,
        "price": "900.25",
        "departure_token": "synthetic-connecting-token",
    }


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
def test_incomplete_layovers_are_unknown(
    query: TripQuery, http_get: Mock, connecting: dict[str, object], layovers: object
) -> None:
    http_get.return_value.json.return_value = {
        "other_flights": [connecting | {"layovers": layovers}]
    }
    (option,) = SerpApiFlightProvider().search(query)
    assert option.stops == 2
    assert option.max_layover_minutes is None


def test_missing_optional_metadata_remains_unknown(
    query: TripQuery, http_get: Mock, nonstop: dict[str, object]
) -> None:
    http_get.return_value.json.return_value = {
        "best_flights": [
            nonstop | {"flights": [{"duration": 725}], "departure_token": None}
        ]
    }
    (option,) = SerpApiFlightProvider().search(query)
    assert option.airline == "Unknown airline"
    assert option.airlines == []
    assert option.flight_numbers == []
    assert option.origin is None
    assert option.destination is None
    assert option.departure_token is None


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
