"""Google Flights outbound search and compatible return-flight retrieval."""

import os
from collections.abc import Iterator

import requests
from pydantic import ValidationError

from faresentry.models import (
    AirportTransfer,
    FlightItinerary,
    FlightOption,
    FlightSegment,
    Layover,
    RoundTripItinerary,
    TripQuery,
)
from faresentry.providers._logging import redact_transport_secrets
from faresentry.providers.base import FlightProviderError

_SEARCH_URL = "https://serpapi.com/search.json"
_TIMEOUT_SECONDS = 30


class SerpApiFlightProvider:
    """Retrieve outbound choices and completed round-trip USD quotes from SerpApi."""

    def search(self, query: TripQuery) -> list[FlightOption]:
        """Normalize best and other choices; skip individually malformed results."""
        payload = self._request(query)
        options: list[FlightOption] = []
        for result in _flight_results(payload):
            option = _normalize(result)
            if option is not None:
                options.append(option)
        return options

    def get_return_options(
        self, outbound_option: FlightOption, query: TripQuery
    ) -> list[RoundTripItinerary]:
        """Fetch compatible returns using the unchanged original search query.

        Each return result's price is the complete round-trip quote, not a
        return-leg supplement. Never substitute the earlier outbound quote.
        """
        token = outbound_option.departure_token
        if not token or not token.strip():
            raise FlightProviderError(
                "This outbound choice has no return lookup token."
            )
        if (
            outbound_option.origin != query.origin
            or outbound_option.destination != query.destination
            or outbound_option.currency != query.currency
        ):
            raise FlightProviderError(
                "Outbound choice does not match the original query."
            )
        payload = self._request(query, departure_token=token)
        options: list[RoundTripItinerary] = []
        for result in _flight_results(payload):
            inbound = _normalize_itinerary(result)
            if inbound is None:
                continue
            # A contradictory explicit type cannot be treated as a round-trip fare.
            if result.get("type", "Round trip") != "Round trip":
                continue
            try:
                options.append(
                    RoundTripItinerary.model_validate(
                        {
                            "outbound": outbound_option.outbound,
                            "inbound": inbound,
                            "total_price": result.get("price"),
                            "currency": query.currency,
                        }
                    )
                )
            except ValidationError:
                # Includes unusable prices and incompatible return endpoints.
                continue
        return options

    def _request(
        self, query: TripQuery, *, departure_token: str | None = None
    ) -> dict[str, object]:
        """Shared request parameters, transport protections, and safe failures."""
        api_key = os.environ.get("SERPAPI_API_KEY", "").strip()
        if not api_key:
            raise FlightProviderError(
                "Set SERPAPI_API_KEY before searching for flights."
            )
        if query.currency != "USD":
            raise FlightProviderError("SerpApi searches currently support USD only.")

        params = {
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
        }
        if departure_token is not None:
            params["departure_token"] = departure_token
        try:
            with redact_transport_secrets(api_key, departure_token):
                response = requests.get(
                    _SEARCH_URL,
                    params=params,
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

        return payload


def _flight_results(payload: dict[str, object]) -> Iterator[dict[str, object]]:
    for group in ("best_flights", "other_flights"):
        results = payload.get(group)
        if isinstance(results, list):
            for result in results:
                if isinstance(result, dict):
                    yield result


def _text(value: object) -> str | None:
    """Treat missing, blank, or malformed optional text as unknown."""
    return value.strip() or None if isinstance(value, str) else None


def _airport_id(value: object) -> str | None:
    return _text(value.get("id")) if isinstance(value, dict) else None


def _normalize_itinerary(result: object) -> FlightItinerary | None:
    """Normalize one direction identically for outbound and return choices."""
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
        itinerary = FlightItinerary(segments=segments, layovers=tuple(connections))
        # Never invent connection time or keep two contradictory duration values.
        if itinerary.duration_minutes != result["total_duration"]:
            return None
        return itinerary
    except ValidationError:
        return None


def _normalize(result: dict[str, object]) -> FlightOption | None:
    outbound = _normalize_itinerary(result)
    if outbound is None:
        return None
    try:
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
