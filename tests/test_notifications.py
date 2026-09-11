import sqlite3
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from faresentry.alerts import AlertPolicy
from faresentry.history import FareWatch
from faresentry.models import (
    FlightItinerary,
    FlightOption,
    FlightSegment,
    HardTravelConstraints,
    Recommendation,
    RecommendationTradeoff,
    RoundTripItinerary,
    TravelerSoftPreferences,
    TripQuery,
)
from faresentry.monitoring import MonitoringRunResult, Recommender, run_monitoring_cycle
from faresentry.notification_models import DeliveryReceipt, NotificationDelivery
from faresentry.notifications import (
    NotificationHistory,
    NotificationProvider,
    NotificationProviderError,
    NotificationService,
    build_notification_message,
)
from faresentry.persistence import SQLiteFareHistory
from faresentry.providers.base import FlightProvider, FlightProviderError
from faresentry.scheduling import MonitoringScheduler, ScheduledWatch

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def watch(destination: str = "SIN") -> FareWatch:
    return FareWatch(
        origin="LAX",
        destination=destination,
        outbound_date="2026-12-01",
        return_date="2026-12-15",
    )


def fare(destination: str = "SIN", price: str = "800") -> RoundTripItinerary:
    def direction(origin: str, destination: str) -> FlightItinerary:
        return FlightItinerary(
            segments=(
                FlightSegment(
                    origin=origin,
                    destination=destination,
                    airline="Example Air",
                    flight_number="EA 1",
                    duration_minutes=300,
                ),
            )
        )

    return RoundTripItinerary(
        outbound=direction("LAX", destination),
        inbound=direction(destination, "LAX"),
        total_price=price,
    )


def choose(
    candidates: Mapping[str, RoundTripItinerary], *args: object, **kwargs: object
) -> Recommendation:
    selected = next(iter(candidates))
    return Recommendation(
        selected_candidate_id=selected,
        recommendation="This itinerary matches your preferences.",
        confidence="high",
        key_tradeoffs=(
            RecommendationTradeoff(
                category="overall_value",
                candidate_ids=(selected,),
                favored_candidate_id=selected,
                explanation="A good fit.",
            ),
        ),
    )


class Setup:
    def __init__(self, path: Path) -> None:
        self.repo = SQLiteFareHistory(path)
        self.history = Mock(spec=NotificationHistory, wraps=self.repo)
        self.sender = Mock(spec=NotificationProvider)
        self.sender.send.return_value = DeliveryReceipt(
            provider="fake_email", provider_message_id="message-1"
        )
        self.service = NotificationService(
            history=self.history, provider=self.sender, clock=lambda: NOW
        )
        self.flights = Mock(spec=FlightProvider)
        self.flights.search.side_effect = lambda query: [
            FlightOption(
                outbound=fare(query.destination).outbound,
                price="800",
                departure_token="opaque-secret-token",
            )
        ]
        self.flights.get_return_options.side_effect = lambda option, query: [
            fare(query.destination)
        ]
        self.recommender = Mock(spec=Recommender)
        self.recommender.recommend.side_effect = choose

    def run(
        self,
        *,
        alert: bool = True,
        empty: bool = False,
        target_price: str | None = None,
    ) -> MonitoringRunResult:
        if empty:
            self.flights.search.side_effect = lambda query: []
        return run_monitoring_cycle(
            TripQuery.model_validate(watch().model_dump()),
            constraints=HardTravelConstraints(),
            preferences=TravelerSoftPreferences(),
            policy=AlertPolicy(
                currency="USD",
                first_observation="alert" if alert else "suppress",
                minimum_absolute_improvement="100",
                target_price=target_price,
            ),
            provider=self.flights,
            history=self.repo,
            recommender=self.recommender,
            clock=lambda: NOW,
        )


@pytest.fixture
def setup(tmp_path: Path) -> Setup:
    return Setup(tmp_path / "notifications.sqlite3")


@pytest.mark.parametrize(
    ("alert", "empty", "expected"),
    [
        (False, True, "not_applicable"),
        (False, False, "not_needed"),
        (True, False, "delivered"),
    ],
)
def test_alert_gating(setup: Setup, alert: bool, empty: bool, expected: str) -> None:
    result = setup.run(alert=alert, empty=empty)
    outcome = setup.service.handle_monitoring_result(watch(), result)
    assert outcome.status == expected
    assert setup.sender.send.call_count == (1 if alert else 0)
    assert setup.history.record_notification_delivery.call_count == (1 if alert else 0)
    if not alert:
        setup.history.get_notification_delivery.assert_not_called()


def test_formatting_uses_structured_facts_and_does_not_requery(
    setup: Setup, monkeypatch: pytest.MonkeyPatch
) -> None:
    prior = setup.repo.create_run(watch(), observed_at=NOW - timedelta(hours=6))
    setup.repo.record_observation(watch(), fare(price="1000"), run=prior)
    setup.repo.mark_run_completed(watch(), run=prior)
    result = setup.run()
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "do-not-disclose-aws")
    message = build_notification_message(watch(), result)
    assert message == build_notification_message(watch(), result)
    assert message.subject == "FareSentry: worthwhile fare found for LAX -> SIN"
    for text in (
        "2026-12-01",
        "2026-12-15",
        "Selected round-trip fare: USD 800",
        "Previous fare for this itinerary: USD 1000",
        "Prior eligible watch low: USD 1000",
        "USD 200",
        "20.00%",
        "0 stops; 300 minutes",
        "1 of 1 configured improvement thresholds met",
        result.recommendation.recommendation,
    ):
        assert text in message.body
    for secret in (
        "opaque-secret-token",
        "departure_token",
        "do-not-disclose-aws",
        "test-key-not-a-real-credential",
        result.watch_id,
        result.selected_candidate_id,
    ):
        assert secret not in message.model_dump_json()
    setup.flights.search.assert_called_once()
    setup.flights.get_return_options.assert_called_once()
    setup.recommender.recommend.assert_called_once()


@pytest.mark.parametrize("display", ["selected", "other", "safe", "similar_prefix"])
def test_optional_recommendation_omits_current_candidate_ids_only(
    setup: Setup, display: str
) -> None:
    selected_fare = fare()
    other_fare = RoundTripItinerary(
        outbound=selected_fare.outbound,
        inbound=FlightItinerary(
            segments=(
                selected_fare.inbound.segments[0].model_copy(
                    update={"flight_number": "EA 2"}
                ),
            )
        ),
        total_price="900",
    )
    setup.flights.get_return_options.side_effect = lambda option, query: [
        selected_fare,
        other_fare,
    ]

    def recommend(
        candidates: Mapping[str, RoundTripItinerary], *args: object, **kwargs: object
    ) -> Recommendation:
        selected, other = candidates
        prose = {
            "selected": f"Choose {selected} because it has the best tradeoff.",
            "other": f"This is a better fit than {other}.",
            "safe": "This itinerary matches your preferences.",
            "similar_prefix": "An itinerary-v1-style comparison favors this trip.",
        }[display]
        return Recommendation(
            selected_candidate_id=selected,
            recommendation=prose,
            confidence="high",
            key_tradeoffs=(
                RecommendationTradeoff(
                    category="overall_value",
                    candidate_ids=(selected, other),
                    favored_candidate_id=selected,
                    explanation="The selected itinerary is a better fit.",
                ),
            ),
        )

    setup.recommender.recommend.side_effect = recommend
    result = setup.run()
    original = result.recommendation
    before = original.model_dump_json()
    assert result.alert_decision.should_alert
    assert result.eligible_candidates == 2

    outcome = setup.service.handle_monitoring_result(watch(), result)

    assert outcome.status == "delivered"
    setup.sender.send.assert_called_once()
    message = setup.sender.send.call_args.args[0]
    assert message.subject == "FareSentry: worthwhile fare found for LAX -> SIN"
    assert "Trip: LAX -> SIN" in message.body
    assert "Selected round-trip fare: USD 800" in message.body
    assert "Why this alert was approved:" in message.body
    assert any(
        signal.satisfied and signal.explanation in message.body
        for signal in result.alert_decision.signals
    )
    for candidate_id in original.key_tradeoffs[0].candidate_ids:
        assert candidate_id not in message.model_dump_json()
    if display in ("selected", "other"):
        assert "Recommendation:" not in message.body
        assert original.recommendation not in message.body
    else:
        assert message.body.endswith("\n\nRecommendation:\n" + original.recommendation)
    assert result.recommendation is original
    assert original.model_dump_json() == before


def test_duplicate_delivery_survives_reconstruction_and_is_run_scoped(
    setup: Setup,
) -> None:
    result = setup.run()
    first = setup.service.handle_monitoring_result(watch(), result)
    reconstructed = NotificationService(
        history=SQLiteFareHistory(setup.repo.database_path), provider=setup.sender
    )
    repeated = reconstructed.handle_monitoring_result(watch(), result)
    assert first.status == "delivered"
    assert repeated.status == "already_delivered"
    assert repeated.delivery == first.delivery
    setup.sender.send.assert_called_once()
    next_run = setup.run()
    # This separate run has no unchanged-fare trigger and needs no email.
    assert (
        reconstructed.handle_monitoring_result(watch(), next_run).status == "not_needed"
    )
    assert setup.repo.get_notification_delivery(watch(), run_id=next_run.run_id) is None
    approved_later_run = setup.run(target_price="900")
    assert (
        reconstructed.handle_monitoring_result(watch(), approved_later_run).status
        == "delivered"
    )
    assert setup.sender.send.call_count == 2
    assert (
        setup.repo.get_notification_delivery(
            watch(), run_id=approved_later_run.run_id
        ).run_id
        == approved_later_run.run_id
    )
    with closing(sqlite3.connect(setup.repo.database_path)) as connection:
        assert {
            row[1]
            for row in connection.execute("PRAGMA table_info(notification_deliveries)")
        } == {"run_id", "delivered_at", "provider", "provider_message_id"}
        assert (
            connection.execute(
                "SELECT COUNT(*) FROM notification_deliveries"
            ).fetchone()[0]
            == 2
        )


def test_provider_failure_preserves_monitoring_and_allows_explicit_retry(
    setup: Setup,
) -> None:
    result = setup.run()
    before_result = result.model_dump_json()
    before_fares = setup.repo.get_recent_observations(watch())
    setup.sender.send.side_effect = NotificationProviderError(
        "private exception payload"
    )
    failed = setup.service.handle_monitoring_result(watch(), result)
    assert failed.status == "failed"
    assert failed.failure_stage == "provider"
    assert failed.error_type == "NotificationProviderError"
    assert "private exception payload" not in failed.model_dump_json()
    assert setup.repo.get_notification_delivery(watch(), run_id=result.run_id) is None
    assert setup.repo.get_recent_observations(watch()) == before_fares
    assert result.model_dump_json() == before_result
    with closing(sqlite3.connect(setup.repo.database_path)) as connection:
        assert connection.execute(
            "SELECT completed FROM monitoring_runs"
        ).fetchall() == [(1,)]
    setup.history.record_notification_delivery.assert_not_called()
    setup.sender.send.side_effect = None
    assert setup.service.handle_monitoring_result(watch(), result).status == "delivered"
    assert setup.sender.send.call_count == 2


@pytest.mark.parametrize(
    "stage", ["get_notification_delivery", "record_notification_delivery"]
)
def test_persistence_failure_is_distinct_and_does_not_record_success(
    setup: Setup, stage: str
) -> None:
    result = setup.run()
    getattr(setup.history, stage).side_effect = sqlite3.OperationalError(
        "database locked"
    )
    outcome = setup.service.handle_monitoring_result(watch(), result)
    assert outcome.status == "failed"
    assert outcome.failure_stage == (
        "history" if stage.startswith("get") else "recording"
    )
    assert setup.sender.send.call_count == (0 if stage.startswith("get") else 1)
    assert setup.repo.get_notification_delivery(watch(), run_id=result.run_id) is None
    with closing(sqlite3.connect(setup.repo.database_path)) as connection:
        assert connection.execute(
            "SELECT completed FROM monitoring_runs"
        ).fetchall() == [(1,)]


@pytest.mark.parametrize(
    "change", ["watch", "price", "run", "incomplete", "missing_selection"]
)
def test_invalid_result_or_incomplete_run_never_sends(
    setup: Setup, change: str
) -> None:
    result = setup.run()
    context = watch()
    if change == "watch":
        context = watch("HND")
    elif change == "price":
        result = result.model_copy(update={"selected_itinerary": fare(price="1")})
    elif change == "run":
        result = result.model_copy(update={"run_id": 999})
    elif change == "missing_selection":
        result = result.model_copy(update={"selected_itinerary": None})
    else:
        with (
            closing(sqlite3.connect(setup.repo.database_path)) as connection,
            connection,
        ):
            connection.execute("UPDATE monitoring_runs SET completed = 0")
    assert setup.service.handle_monitoring_result(context, result).status == "failed"
    setup.sender.send.assert_not_called()


def test_delivery_repository_validates_run_and_preserves_first_receipt(
    setup: Setup,
) -> None:
    result = setup.run()
    delivery = NotificationDelivery(
        run_id=result.run_id,
        delivered_at=NOW,
        provider="fake",
        provider_message_id="one",
    )
    assert setup.repo.record_notification_delivery(watch(), delivery) == delivery
    assert setup.repo.record_notification_delivery(watch(), delivery) == delivery
    with pytest.raises(ValueError, match="Conflicting"):
        setup.repo.record_notification_delivery(
            watch(), delivery.model_copy(update={"provider_message_id": "two"})
        )
    for context, run_id in ((watch("HND"), result.run_id), (watch(), 999)):
        with pytest.raises(ValueError, match="completed run"):
            setup.repo.get_notification_delivery(context, run_id=run_id)
        with pytest.raises(ValueError, match="completed run"):
            setup.repo.record_notification_delivery(
                context, delivery.model_copy(update={"run_id": run_id})
            )
    incomplete = setup.repo.create_run(watch(), observed_at=NOW)
    with pytest.raises(ValueError, match="completed run"):
        setup.repo.record_notification_delivery(
            watch(), delivery.model_copy(update={"run_id": incomplete.run_id})
        )


def test_scheduler_notifications_preserve_monitoring_and_isolate_delivery_failures(
    setup: Setup,
) -> None:
    configs = [
        ScheduledWatch(
            watch=watch(destination),
            constraints=HardTravelConstraints(),
            preferences=TravelerSoftPreferences(),
            policy=AlertPolicy(
                currency="USD",
                first_observation="suppress" if destination == "CDG" else "alert",
            ),
            check_interval=timedelta(hours=6),
        )
        for destination in ("SIN", "HND", "CDG", "LHR")
    ]
    search = setup.flights.search.side_effect

    def fail_lhr(query: TripQuery) -> list[FlightOption]:
        if query.destination == "LHR":
            raise FlightProviderError("scripted failure")
        return search(query)

    setup.flights.search.side_effect = fail_lhr
    scheduler = MonitoringScheduler(
        configs,
        history=setup.repo,
        provider=setup.flights,
        recommender=setup.recommender,
        clock=lambda: NOW,
    )
    batch = scheduler.run_due_watches()
    before = batch.model_dump_json()
    setup.sender.send.side_effect = [
        NotificationProviderError("mail failure"),
        DeliveryReceipt(provider="fake", provider_message_id="two"),
    ]
    outcomes = setup.service.handle_scheduler_result(
        batch, watches={item.watch.watch_id: item.watch for item in configs}
    )
    assert [item.status for item in outcomes] == [
        "failed",
        "delivered",
        "not_needed",
        "not_applicable",
    ]
    assert setup.sender.send.call_count == 2
    assert setup.flights.search.call_count == 4
    assert setup.recommender.recommend.call_count == 3
    assert batch.watches_succeeded == 3 and batch.watches_failed == 1
    assert batch.model_dump_json() == before
    assert batch.outcomes[0].result is not None
    assert batch.outcomes[0].result.alert_decision.should_alert
