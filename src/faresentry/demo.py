"""Synthetic fixtures and thin orchestration for the local judging demo.

No real provider, agent SDK, or email adapter is imported here. Every execution
owns a temporary database; callers cannot point demo setup at a live database.
"""

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Literal

from pydantic import BaseModel, ConfigDict

from faresentry.alerts import AlertPolicy
from faresentry.history import FareObservation, FareWatch
from faresentry.models import (
    FlightItinerary,
    FlightOption,
    FlightSegment,
    HardTravelConstraints,
    Layover,
    Recommendation,
    RecommendationTradeoff,
    RoundTripItinerary,
    TravelerSoftPreferences,
    TripQuery,
)
from faresentry.monitoring import MonitoringRunResult, run_monitoring_cycle
from faresentry.notification_models import (
    DeliveryReceipt,
    NotificationMessage,
    NotificationOutcome,
)
from faresentry.notifications import NotificationService
from faresentry.persistence import SQLiteFareHistory
from faresentry.scheduling import MonitoringScheduler, ScheduledWatch, is_watch_due

DemoScenario = Literal["routine", "opportunity"]
DEMO_NOW = datetime(2026, 9, 10, 18, tzinfo=UTC)


class CheckReport(BaseModel):
    """Retain application results for rerendering without rerunning a check."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    configuration: ScheduledWatch
    checked_at: datetime
    result: MonitoringRunResult | None = None
    notification: NotificationOutcome | None = None
    history: tuple[FareObservation, ...] = ()
    current_candidate_ids: tuple[str, ...] | None = None
    error: str | None = None
    history_error: bool = False
    simulated: bool = False
    simulated_sends: int = 0
    due_after_check: bool | None = None


def demo_watch() -> ScheduledWatch:
    return ScheduledWatch(
        watch=FareWatch.from_query(
            TripQuery(
                origin="LAX",
                destination="SIN",
                outbound_date="2026-12-01",
                return_date="2026-12-15",
            )
        ),
        constraints=HardTravelConstraints(
            max_stops_per_direction=1,
            max_duration_minutes_per_direction=1400,
            max_connection_duration_minutes=240,
            allow_airport_transfers=False,
        ),
        preferences=TravelerSoftPreferences(
            preferred_max_stops_per_direction=0,
            prefer_shorter_total_travel_time=True,
            prefer_shorter_connections=True,
            willingness_to_pay_more="moderate",
            preferred_airlines=("Demo Pacific",),
        ),
        policy=AlertPolicy(
            currency="USD",
            first_observation="suppress",
            minimum_absolute_improvement="100",
        ),
        check_interval=timedelta(hours=6),
    )


def _direction(airports: tuple[str, ...], prefix: str) -> FlightItinerary:
    return FlightItinerary(
        segments=tuple(
            FlightSegment(
                origin=origin,
                destination=destination,
                airline="Demo Pacific",
                flight_number=f"DP {prefix}{index}",
                duration_minutes=900 if len(airports) == 2 else 450,
            )
            for index, (origin, destination) in enumerate(zip(airports, airports[1:]))
        ),
        layovers=tuple(
            Layover(airport=airport, duration_minutes=120) for airport in airports[1:-1]
        ),
    )


class DemoFlightProvider:
    """Three synthetic round trips; outbound prescreen rejects the two-stop fare."""

    def __init__(self, *, opportunity: bool = False) -> None:
        self.search_calls = 0
        self.return_calls = 0
        self.trips = tuple(
            RoundTripItinerary(
                outbound=_direction(route, f"{index}O"),
                inbound=_direction(tuple(reversed(route)), f"{index}R"),
                total_price=price,
            )
            for index, (route, price) in enumerate(
                (
                    (("LAX", "SIN"), "800" if opportunity else "1000"),
                    (("LAX", "NRT", "SIN"), "760" if opportunity else "920"),
                    (("LAX", "SFO", "NRT", "SIN"), "600"),
                )
            )
        )

    def search(self, query: TripQuery) -> list[FlightOption]:
        self.search_calls += 1
        return [
            FlightOption(
                outbound=trip.outbound,
                price=trip.total_price,
                departure_token=f"synthetic-token-{index}",
            )
            for index, trip in enumerate(self.trips)
        ]

    def get_return_options(
        self, outbound_option: FlightOption, query: TripQuery
    ) -> list[RoundTripItinerary]:
        self.return_calls += 1
        return [
            trip for trip in self.trips if trip.outbound == outbound_option.outbound
        ]


class DemoRecommender:
    """Scripted subjective choice, explicitly a simulation of the agent adapter."""

    def recommend(
        self,
        acceptable_candidates: Mapping[str, RoundTripItinerary],
        preferences: TravelerSoftPreferences,
        *,
        constraints: HardTravelConstraints,
    ) -> Recommendation:
        selected = next(iter(acceptable_candidates))
        return Recommendation(
            selected_candidate_id=selected,
            confidence="high",
            recommendation=(
                "The nonstop trip is the better fit for this traveler, who values "
                "shorter travel and is willing to pay more for convenience."
            ),
            key_tradeoffs=(
                RecommendationTradeoff(
                    category="overall_value",
                    candidate_ids=tuple(acceptable_candidates),
                    favored_candidate_id=selected,
                    explanation=(
                        "A simpler journey is worth the premium for this traveler."
                    ),
                ),
            ),
        )


class DemoNotificationProvider:
    def __init__(self) -> None:
        self.messages: list[NotificationMessage] = []

    def send(self, message: NotificationMessage) -> DeliveryReceipt:
        self.messages.append(message)
        return DeliveryReceipt(
            provider="demo", provider_message_id=f"simulated-{len(self.messages)}"
        )


def run_demo(scenario: DemoScenario) -> CheckReport:
    if scenario not in ("routine", "opportunity"):
        raise ValueError("Choose a supported demo scenario")
    configuration = demo_watch()
    baseline_time = DEMO_NOW - configuration.check_interval
    with TemporaryDirectory(prefix="faresentry-demo-") as directory:
        history = SQLiteFareHistory(Path(directory) / "demo.sqlite3")
        run_monitoring_cycle(
            TripQuery.model_validate(configuration.watch.model_dump()),
            constraints=configuration.constraints,
            preferences=configuration.preferences,
            policy=configuration.policy,
            provider=DemoFlightProvider(),
            history=history,
            recommender=DemoRecommender(),
            clock=lambda: baseline_time,
        )
        scheduler = MonitoringScheduler(
            [configuration],
            history=history,
            provider=DemoFlightProvider(opportunity=scenario == "opportunity"),
            recommender=DemoRecommender(),
            clock=lambda: DEMO_NOW,
        )
        batch = scheduler.run_due_watches()
        sender = DemoNotificationProvider()
        service = NotificationService(
            history=history, provider=sender, clock=lambda: DEMO_NOW
        )
        notification = service.handle_scheduler_result(
            batch, watches={configuration.watch.watch_id: configuration.watch}
        )[0]
        outcome = batch.outcomes[0]
        latest = history.get_latest_run(configuration.watch)
        observations = tuple(history.get_recent_observations(configuration.watch))
        return CheckReport(
            configuration=configuration,
            checked_at=DEMO_NOW,
            result=outcome.result,
            notification=notification,
            history=observations,
            current_candidate_ids=current_run_candidate_ids(
                outcome.result, observations
            ),
            simulated=True,
            simulated_sends=len(sender.messages),
            error="The demo check could not complete."
            if outcome.status == "failed"
            else None,
            due_after_check=is_watch_due(
                configuration,
                now=DEMO_NOW,
                last_attempt_at=latest.observed_at if latest else None,
            ),
        )


def current_run_candidate_ids(
    result: MonitoringRunResult | None, observations: tuple[FareObservation, ...]
) -> tuple[str, ...] | None:
    """Collect persisted current candidates before trimming visible history."""
    if result is None:
        return None
    ids = {
        item.itinerary_id
        for item in observations
        if item.run_id == result.run_id and item.watch_id == result.watch_id
    }
    # Every eligible candidate is persisted by a successfully completed cycle.
    return tuple(sorted(ids)) if len(ids) == result.eligible_candidates else None


def safe_recommendation_text(
    result: MonitoringRunResult,
    text: str,
    *,
    current_candidate_ids: tuple[str, ...] | None,
) -> str | None:
    """Omit optional prose when IDs are present or complete coverage is unavailable."""
    if current_candidate_ids is None:
        return None
    ids = {result.watch_id, result.selected_candidate_id, *current_candidate_ids}
    if result.recommendation is not None:
        ids.update(
            candidate_id
            for tradeoff in result.recommendation.key_tradeoffs
            for candidate_id in tradeoff.candidate_ids
        )
    return None if any(value and value in text for value in ids) else text


def history_rows(observations: tuple[FareObservation, ...]) -> list[dict[str, str]]:
    """Explicit display fields, never raw observation serialization."""
    return [
        {
            "Observed (UTC)": item.observed_at.strftime("%Y-%m-%d %H:%M"),
            "Fare": f"{item.currency} {item.total_price:,.2f}",
            "Route": f"{item.outbound.origin} → {item.outbound.destination}",
            "Outbound / return stops": f"{item.outbound.stops} / {item.inbound.stops}",
        }
        for item in observations
    ]
