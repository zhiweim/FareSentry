"""Common contract for flight-data providers."""

from typing import Protocol

from faresentry.models import FlightOption, TripQuery


class FlightProviderError(RuntimeError):
    """A flight search failed; the message is safe to display to the user."""


class FlightProvider(Protocol):
    """Retrieve normalized flight options for a trip."""

    def search(self, query: TripQuery) -> list[FlightOption]:
        """Return outbound choices, or []; raise FlightProviderError on failure."""
        ...
