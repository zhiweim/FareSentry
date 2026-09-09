"""Common contract for flight-data providers."""

from typing import Protocol

from faresentry.models import FlightOption, RoundTripItinerary, TripQuery


class FlightProviderError(RuntimeError):
    """A flight search failed; the message is safe to display to the user."""


class FlightProvider(Protocol):
    """Retrieve normalized flight options for a trip."""

    def search(self, query: TripQuery) -> list[FlightOption]:
        """Return outbound choices, or []; raise FlightProviderError on failure."""
        ...

    def get_return_options(
        self, outbound_option: FlightOption, query: TripQuery
    ) -> list[RoundTripItinerary]:
        """Complete a choice using its original query and provider workflow state.

        Return [] for no usable returns; raise FlightProviderError on failure,
        including missing workflow state or incompatible query context.
        """
        ...
