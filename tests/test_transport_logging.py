import io
import logging
import socket
import traceback
from datetime import date
from unittest.mock import Mock
from urllib.parse import parse_qs, quote_plus, urlsplit

import pytest
import requests
from urllib3.connectionpool import HTTPSConnectionPool
from urllib3.response import HTTPResponse

from faresentry.models import TripQuery
from faresentry.providers._logging import _FILTER, protect_transport_logs
from faresentry.providers.base import FlightProviderError
from faresentry.providers.serpapi import SerpApiFlightProvider

# Save during collection, before the autouse fixture blocks Session.request.
_REAL_SESSION_REQUEST = requests.Session.request


@pytest.mark.parametrize(
    "secret", ["synthetic-transport-secret", "synthetic+/= secret"]
)
@pytest.mark.parametrize("failure", [False, True])
def test_real_transport_logging_redacts_key_without_changing_request(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    secret: str,
    failure: bool,
) -> None:
    def no_network(*args: object, **kwargs: object) -> None:
        raise AssertionError("Network access is forbidden")

    # Allow the real Requests stack, but block both socket creation and DNS.
    monkeypatch.setattr(socket, "socket", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(requests.Session, "request", _REAL_SESSION_REQUEST)
    monkeypatch.setattr(requests.utils, "get_environ_proxies", lambda *a, **k: {})
    monkeypatch.setattr(requests.sessions, "get_environ_proxies", lambda *a, **k: {})
    monkeypatch.setenv("SERPAPI_API_KEY", secret)

    connection = Mock(
        is_closed=False,
        is_connected=True,
        is_verified=True,
        proxy_is_verified=True,
        proxy=None,
    )
    connection.getresponse.return_value = HTTPResponse(
        body=io.BytesIO(b'{"best_flights": []}'),
        status=200,
        headers={"Content-Type": "application/json"},
        preload_content=False,
    )
    if failure:
        connection.request.side_effect = OSError(f"transport failed: api_key={secret}")
    monkeypatch.setattr(HTTPSConnectionPool, "_get_conn", lambda *a, **k: connection)
    monkeypatch.setattr(HTTPSConnectionPool, "_validate_conn", lambda *a, **k: None)
    caplog.set_level(logging.DEBUG, logger="urllib3.connectionpool")
    caplog.set_level(logging.DEBUG, logger="urllib3.util.retry")

    provider = SerpApiFlightProvider()
    query = TripQuery(
        origin="LAX",
        destination="HND",
        outbound_date=date(2026, 10, 12),
        return_date=date(2026, 10, 27),
    )
    if failure:
        with pytest.raises(FlightProviderError) as caught:
            provider.search(query)
        assert secret not in "".join(traceback.format_exception(caught.value))
        assert secret not in repr(caught.value)
    else:
        assert provider.search(query) == []
        assert "api_key=REDACTED" in caplog.text
        assert "GET /search.json?" in caplog.text
        assert any(record.name == "urllib3.connectionpool" for record in caplog.records)

    # Assert against the request that reached the connection, not a requests.get mock.
    method, url = connection.request.call_args.args
    assert method == "GET"
    params = parse_qs(urlsplit(url).query)
    assert params["api_key"] == [secret]
    assert params["engine"] == ["google_flights"]
    assert "Authorization" not in connection.request.call_args.kwargs["headers"]
    assert secret not in caplog.text
    assert quote_plus(secret) not in caplog.text
    assert secret not in repr([record.__dict__ for record in caplog.records])
    assert secret not in repr(provider)
    assert secret not in repr(_FILTER.__dict__)


@pytest.mark.parametrize(
    "logger_name",
    [
        "urllib3.connectionpool",
        "urllib3.util.retry",
        "requests",
        "requests.packages.urllib3.connectionpool",
    ],
)
@pytest.mark.parametrize("representation", ["url", "dict", "exception"])
def test_transport_record_representations_are_redacted(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    logger_name: str,
    representation: str,
) -> None:
    secret = "synthetic-log-secret"
    monkeypatch.setenv("SERPAPI_API_KEY", secret)
    protect_transport_logs()
    caplog.set_level(logging.DEBUG, logger=logger_name)
    logger = logging.getLogger(logger_name)
    if representation == "url":
        logger.debug(
            "request %s", f"/search.json?api_key={secret}&engine=google_flights"
        )
    elif representation == "dict":
        logger.debug("params %r", {"api_key": secret, "engine": "google_flights"})
    else:
        try:
            raise ValueError(f"bad credential: {secret}")
        except ValueError:
            logger.exception("Request failed")
    assert "REDACTED" in caplog.text
    assert secret not in caplog.text
    assert secret not in repr([record.__dict__ for record in caplog.records])


def test_parameter_redaction_does_not_depend_on_current_key(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("SERPAPI_API_KEY")
    protect_transport_logs()
    caplog.set_level(logging.DEBUG, logger="urllib3.connectionpool")
    logging.getLogger("urllib3.connectionpool").debug(
        "late request /search.json?api_key=old-synthetic-secret&engine=google_flights"
    )
    assert "old-synthetic-secret" not in caplog.text
    assert "api_key=REDACTED&engine=google_flights" in caplog.text


def test_installation_is_idempotent_and_preserves_other_logging(
    caplog: pytest.LogCaptureFixture,
) -> None:
    logger = logging.getLogger("urllib3.connectionpool")
    settings = (logger.level, logger.disabled, logger.propagate, list(logger.handlers))
    protect_transport_logs()
    protect_transport_logs()
    assert logger.filters.count(_FILTER) == 1
    assert settings == (
        logger.level,
        logger.disabled,
        logger.propagate,
        list(logger.handlers),
    )
    caplog.set_level(logging.INFO)
    logging.getLogger("faresentry").info("Application logging still works")
    assert "Application logging still works" in caplog.text
