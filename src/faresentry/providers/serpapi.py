"""Initial round-trip Google Flights searches, without return-flight selection."""

import os

import requests
from pydantic import ValidationError

from faresentry.models import (
    AirportTransfer,
    FlightItinerary,
    FlightOption,
    FlightSegment,
    Layover,
    TripQuery,
)
from faresentry.providers._logging import protect_transport_logs
from faresentry.providers.base import FlightProviderError

_SEARCH_URL = "https://serpapi.com/search.json"
_TIMEOUT_SECONDS = 30


class SerpApiFlightProvider:
    """Retrieve outbound choices carrying round-trip USD prices from SerpApi."""

    def search(self, query: TripQuery) -> list[FlightOption]:
        """Normalize best and other choices; skip individually malformed results."""
        api_key = os.environ.get("SERPAPI_API_KEY", "").strip()
        if not api_key:
            raise FlightProviderError(
                "Set SERPAPI_API_KEY before searching for flights."
            )
        if query.currency != "USD":
            raise FlightProviderError("SerpApi searches currently support USD only.")

        protect_transport_logs()
        try:
            response = requests.get(
                _SEARCH_URL,
                params={
                    "api_key": api_key,
                    "engine": "google_flights",
                    "departure_id": query.origin,
                    "arrival_id": query.destination,
                    "outbound_date": query.outbound_date.isoformat(),
                    "return_date": query.return_date.isoformat(),
                    "type": 1,
                    "travel_class": 1,
                    "currency": "USD",
                    "hl": "en",
                    "gl": "us",
                },
                timeout=_TIMEOUT_SECONDS,
                allow_redirects=False,
            )
        except requests.Timeout:
            raise FlightProviderError(
                "SerpApi request timed out. Try again later."
            ) from None
        except requests.RequestException:
            # Requests exceptions can contain the URL, including the API key.
            raise FlightProviderError("Could not connect to SerpApi.") from None

        if not 200 <= response.status_code < 300:
            raise FlightProviderError(
                f"SerpApi returned HTTP {response.status_code}. "
                "Check your API key, account quota, and request settings."
            )
        try:
            payload = response.json()
        except ValueError:
            raise FlightProviderError("SerpApi returned invalid JSON.") from None
        if not isinstance(payload, dict):
            raise FlightProviderError("SerpApi returned an invalid response format.")
        metadata = payload.get("search_metadata")
        if payload.get("error") or (
            isinstance(metadata, dict) and metadata.get("status") == "Error"
        ):
            # Never expose upstream error text, which may echo request credentials.
            raise FlightProviderError(
                "SerpApi reported a search error. Check your account and trip settings."
            )

        options: list[FlightOption] = []
        for group in ("best_flights", "other_flights"):
            results = payload.get(group)
            if not isinstance(results, list):
                continue
            for result in results:
                option = _normalize(result)
                if option is not None:
                    options.append(option)
        return options


def _text(value: object) -> str | None:
    """Treat missing, blank, or malformed optional text as unknown."""
    return value.strip() or None if isinstance(value, str) else None


def _airport_id(value: object) -> str | None:
    return _text(value.get("id")) if isinstance(value, dict) else None


def _normalize(result: object) -> FlightOption | None:
    if not isinstance(result, dict):
        return None
    if type(result.get("total_duration")) is not int:
        return None
    flights = result.get("flights")
    if not isinstance(flights, list) or not flights:
        return None
    # Dropping a malformed segment could falsely turn a connection into nonstop.
    if any(not isinstance(flight, dict) or not flight for flight in flights):
        return None

    layovers = result.get("layovers", [])
    if not isinstance(layovers, list) or len(layovers) != len(flights) - 1:
        return None
    try:
        segments = tuple(
            FlightSegment.model_validate(
                {
                    "origin": _airport_id(flight.get("departure_airport")),
                    "destination": _airport_id(flight.get("arrival_airport")),
                    "airline": _text(flight.get("airline")),
                    "flight_number": _text(flight.get("flight_number")),
                    "duration_minutes": flight.get("duration"),
                }
            )
            for flight in flights
        )
        connections: list[Layover | AirportTransfer] = []
        for previous, following, layover in zip(segments, segments[1:], layovers):
            if not isinstance(layover, dict):
                return None
            arrival, departure = previous.destination, following.origin
            # The ordered flights supply both endpoints. If a connection airport
            # is also supplied, it must agree with one of those endpoints.
            if "id" in layover and _airport_id(layover) not in (arrival, departure):
                return None
            if arrival == departure:
                connections.append(
                    Layover.model_validate(
                        {
                            "airport": arrival,
                            "duration_minutes": layover.get("duration"),
                        }
                    )
                )
            else:
                connections.append(
                    AirportTransfer.model_validate(
                        {
                            "arrival_airport": arrival,
                            "departure_airport": departure,
                            "duration_minutes": layover.get("duration"),
                        }
                    )
                )
        outbound = FlightItinerary(segments=segments, layovers=tuple(connections))
        # Never invent connection time or keep two contradictory duration values.
        if outbound.duration_minutes != result["total_duration"]:
            return None
        return FlightOption.model_validate(
            {
                "outbound": outbound,
                "price": result.get("price"),
                "currency": "USD",
                "departure_token": _text(result.get("departure_token")),
            }
        )
    except ValidationError:
        return None
