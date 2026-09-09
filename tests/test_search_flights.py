from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

from faresentry.models import FlightItinerary, FlightOption, FlightSegment
from faresentry.providers.base import FlightProviderError
from scripts import search_flights

ARGS = [
    "--origin",
    "LAX",
    "--destination",
    "HND",
    "--outbound",
    "2026-10-12",
    "--return-date",
    "2026-10-27",
]


@pytest.fixture
def provider(monkeypatch: pytest.MonkeyPatch) -> Mock:
    fake = Mock()
    monkeypatch.setattr(search_flights, "SerpApiFlightProvider", lambda: fake)
    monkeypatch.setattr(search_flights, "load_dotenv", Mock())
    return fake


def test_readable_output_hides_token(
    provider: Mock, capsys: pytest.CaptureFixture[str]
) -> None:
    provider.search.return_value = [
        FlightOption(
            outbound=FlightItinerary(
                segments=(
                    FlightSegment(
                        origin="LAX",
                        destination="HND",
                        airline="ANA",
                        flight_number="NH 105",
                        duration_minutes=725,
                    ),
                )
            ),
            price=Decimal("768"),
            departure_token="do-not-print-this-token",
        )
    ]
    assert search_flights.main(ARGS) == 0
    output = capsys.readouterr().out
    assert "ANA" in output
    assert "Price: $768.00 (round trip)" in output
    assert "Route: LAX -> HND" in output
    assert "Duration: 12h 5m (outbound)" in output
    assert "Stops: 0" in output
    assert "Flights: NH 105" in output
    assert "Return lookup token: available" in output
    assert "Return flights not selected" in output
    assert "do-not-print-this-token" not in output
    assert "test-key-not-a-real-credential" not in output
    search_flights.load_dotenv.assert_called_once_with(
        Path(search_flights.__file__).resolve().parents[1] / ".env", override=False
    )


def test_no_results(provider: Mock, capsys: pytest.CaptureFixture[str]) -> None:
    provider.search.return_value = []
    assert search_flights.main(ARGS) == 0
    assert "No usable flight options" in capsys.readouterr().out


def test_safe_failure(provider: Mock, capsys: pytest.CaptureFixture[str]) -> None:
    provider.search.side_effect = FlightProviderError("SerpApi request timed out.")
    assert search_flights.main(ARGS) == 1
    assert "Flight search failed: SerpApi request timed out." in capsys.readouterr().err


@pytest.mark.parametrize(
    "args",
    [
        [],
        ARGS[:-1] + ["not-a-date"],
        ARGS[:-1] + ["2026-10-01"],
        ["--origin", "LA"] + ARGS[2:],
    ],
)
def test_invalid_arguments_do_not_search(provider: Mock, args: list[str]) -> None:
    with pytest.raises(SystemExit) as caught:
        search_flights.main(args)
    assert caught.value.code == 2
    provider.search.assert_not_called()
