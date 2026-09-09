"""Placeholder for the future SerpApi Google Flights integration."""

from faresentry.models import FlightOption, TripQuery


class SerpApiFlightProvider:
    """Provider scaffold; HTTP retrieval and normalization are not implemented."""

    def search(self, query: TripQuery) -> list[FlightOption]:
        """Fail explicitly until live flight retrieval is implemented."""
        raise NotImplementedError("SerpApi flight search is not implemented yet")
