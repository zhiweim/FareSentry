from datetime import date

import pytest

from faresentry.models import TripQuery
from faresentry.providers.base import FlightProvider
from faresentry.providers.serpapi import SerpApiFlightProvider


def test_serpapi_placeholder_fails_explicitly() -> None:
    provider: FlightProvider = SerpApiFlightProvider()
    query = TripQuery(
        origin="SFO",
        destination="NRT",
        outbound_date=date(2026, 12, 1),
        return_date=date(2026, 12, 15),
    )
    with pytest.raises(NotImplementedError, match="not implemented yet"):
        provider.search(query)
