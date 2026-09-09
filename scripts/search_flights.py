"""Manual, live SerpApi smoke test. Run from an installed development checkout."""

import argparse
import sys
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from pydantic import ValidationError

from faresentry.models import TripQuery
from faresentry.providers.base import FlightProvider, FlightProviderError
from faresentry.providers.serpapi import SerpApiFlightProvider


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Find outbound economy choices with round-trip USD prices."
    )
    parser.add_argument("--origin", required=True, help="Origin airport code, e.g. LAX")
    parser.add_argument(
        "--destination", required=True, help="Destination code, e.g. HND"
    )
    parser.add_argument("--outbound", required=True, type=date.fromisoformat)
    parser.add_argument("--return-date", required=True, type=date.fromisoformat)
    args = parser.parse_args(argv)
    try:
        query = TripQuery(
            origin=args.origin.strip().upper(),
            destination=args.destination.strip().upper(),
            outbound_date=args.outbound,
            return_date=args.return_date,
        )
    except ValidationError:
        parser.error(
            "Use distinct three-letter airports and a return date >= outbound."
        )

    # Only this local entry point loads .env; exported environment values take priority.
    load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)
    provider: FlightProvider = SerpApiFlightProvider()
    try:
        options = provider.search(query)
    except FlightProviderError as error:
        print(f"Flight search failed: {error}", file=sys.stderr)
        return 1

    if not options:
        print("No usable flight options returned for this search.")
        return 0

    print("Outbound choices with round-trip prices (USD). Return flights not selected.")
    for option in options:
        hours, minutes = divmod(option.duration_minutes, 60)
        print(f"\n{option.airline}")
        print(f"Price: ${option.price:,.2f} (round trip)")
        print(
            f"Route: {option.origin or 'Unknown'} -> {option.destination or 'Unknown'}"
        )
        print(f"Duration: {hours}h {minutes}m (outbound)")
        print(f"Stops: {option.stops}")
        layover = option.max_layover_minutes
        print(
            f"Max layover: {str(layover) + 'm' if layover is not None else 'Unknown'}"
        )
        print(f"Flights: {', '.join(option.flight_numbers) or 'Unknown'}")
        token_status = "available" if option.departure_token else "unavailable"
        print(f"Return lookup token: {token_status}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
