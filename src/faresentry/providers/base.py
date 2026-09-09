"""Common contract for flight-data providers."""

from typing import Protocol

from faresentry.models import FlightOption, TripQuery


class FlightProvider(Protocol):
    """Retrieve normalized flight options for a trip."""

    def search(self, query: TripQuery) -> list[FlightOption]:
        """Return matching options, or an empty list if none are available."""
        ...
