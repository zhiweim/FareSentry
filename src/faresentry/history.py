"""Fare-history domain data and deterministic calculations; no database or SDK."""

import hashlib
import json
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Context, Decimal, localcontext

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator

from faresentry.models import CurrencyCode, RoundTripItinerary, TripQuery


def _fingerprint(prefix: str, data: dict[str, object]) -> str:
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"))
    return prefix + hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class FareWatch(TripQuery):
    """Immutable search context. Identical queries intentionally share history.

    Covers all current variable search parameters. Add future cabin/passenger/
    search filters here and version the identity before supporting them. History
    describes observed fares, independently of traveler preference judgments.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    @classmethod
    def from_query(cls, query: TripQuery) -> "FareWatch":
        return cls.model_validate(query.model_dump())

    @property
    def watch_id(self) -> str:
        return _fingerprint("watch-v1-", self.model_dump(mode="json"))


def itinerary_identity(watch: FareWatch, itinerary: RoundTripItinerary) -> str:
    """Identity of normalized directions within a watch, excluding price.

    Includes flight numbers, airlines, airports, durations, and connections.
    Schedule timestamps are unavailable in the current itinerary model, so this
    is not a booking identity; duration/connection changes produce a new ID.
    """
    return _fingerprint(
        "itinerary-v1-",
        {
            "watch_id": watch.watch_id,
            "outbound": itinerary.outbound.model_dump(mode="json"),
            "inbound": itinerary.inbound.model_dump(mode="json"),
        },
    )


class FareObservation(RoundTripItinerary):
    """Stored complete fare with safe, normalized itinerary details.

    Inherited direction properties derive stops, durations, airlines, and
    connections without retaining provider workflow state or raw responses.
    """

    observation_id: int = Field(gt=0, strict=True)
    watch_id: str = Field(pattern=r"^watch-v1-[0-9a-f]{64}$")
    itinerary_id: str = Field(pattern=r"^itinerary-v1-[0-9a-f]{64}$")
    observed_at: AwareDatetime

    @field_validator("observed_at")
    @classmethod
    def normalize_timestamp(cls, value: datetime) -> datetime:
        return value.astimezone(UTC)


class PriceStatistics(BaseModel):
    """All observations in the requested scope; each record counts once.

    Current is last in (UTC timestamp, insertion ID) order, previous is the
    preceding record, even when timestamps tie. Historical low excludes current;
    minimum/maximum/average include it. Signed changes are current minus reference.
    Missing prices/comparisons are None, never zero. No currency conversion.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    currency: CurrencyCode
    observation_count: int = Field(ge=0)
    minimum_price: Decimal | None = None
    maximum_price: Decimal | None = None
    average_price: Decimal | None = None
    current_price: Decimal | None = None
    previous_price: Decimal | None = None
    historical_low_price: Decimal | None = None
    current_vs_previous: Decimal | None = None
    current_vs_historical_low: Decimal | None = None


def calculate_price_statistics(
    observations: list[FareObservation], *, currency: str
) -> PriceStatistics:
    """Compute using Decimal only, independent of the caller's Decimal context.

    Precision is at least 28 significant digits and grows to keep sums and
    differences exact. Nonterminating averages use ROUND_HALF_EVEN at that
    precision; no rounding to cents is imposed on stored prices.
    """
    if any(item.currency != currency for item in observations):
        raise ValueError("Cannot combine observations with different currencies")
    if len({item.watch_id for item in observations}) > 1:
        raise ValueError("Cannot combine observations from different watches")
    ordered = sorted(
        observations, key=lambda item: (item.observed_at, item.observation_id)
    )
    if not ordered:
        return PriceStatistics(currency=currency, observation_count=0)
    prices = [item.total_price for item in ordered]
    precision = max(
        28,
        max(price.adjusted() for price in prices)
        - min(int(price.as_tuple().exponent) for price in prices)
        + len(str(len(prices)))
        + 2,
    )
    current = prices[-1]
    previous = prices[-2] if len(prices) > 1 else None
    historical_low = min(prices[:-1]) if len(prices) > 1 else None
    with localcontext(Context(prec=precision, rounding=ROUND_HALF_EVEN)):
        return PriceStatistics(
            currency=currency,
            observation_count=len(prices),
            minimum_price=min(prices),
            maximum_price=max(prices),
            average_price=sum(prices, Decimal(0)) / Decimal(len(prices)),
            current_price=current,
            previous_price=previous,
            historical_low_price=historical_low,
            current_vs_previous=(current - previous if previous is not None else None),
            current_vs_historical_low=(
                current - historical_low if historical_low is not None else None
            ),
        )
