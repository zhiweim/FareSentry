from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from sqlite3 import OperationalError, connect
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

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
from faresentry.monitoring import Recommender, run_monitoring_cycle
from faresentry.persistence import SQLiteFareHistory
from faresentry.providers.base import FlightProvider, FlightProviderError
from faresentry.scheduling import (
    MonitoringScheduler,
    ScheduledWatch,
    SchedulingHistory,
    is_watch_due,
)

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def configuration(destination: str = "SIN", *, enabled: bool = True) -> ScheduledWatch:
    return ScheduledWatch(
        watch=FareWatch(
            origin="LAX",
            destination=destination,
            outbound_date="2026-12-01",
            return_date="2026-12-15",
        ),
        constraints=HardTravelConstraints(max_stops_per_direction=1),
        preferences=TravelerSoftPreferences(),
        policy=AlertPolicy(currency="USD", first_observation="alert"),
        enabled=enabled,
        check_interval=timedelta(hours=6),
        max_return_lookups=2,
    )


class FakeTime:
    def __init__(self) -> None:
        self.now = NOW
        self.sleeps: list[float] = []

    def __call__(self) -> datetime:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += timedelta(seconds=seconds)


class Environment:
    def __init__(self, path: Path) -> None:
        self.repo = SQLiteFareHistory(path)
        self.history = Mock(spec=SchedulingHistory, wraps=self.repo)
        self.provider = Mock(spec=FlightProvider)
        self.provider.search.return_value = []
        self.recommender = Mock(spec=Recommender)
        self.cycle = Mock(wraps=run_monitoring_cycle)
        self.time = FakeTime()

    def scheduler(self, *watches: ScheduledWatch) -> MonitoringScheduler:
        return MonitoringScheduler(
            watches,
            history=self.history,
            provider=self.provider,
            recommender=self.recommender,
            monitoring_cycle=self.cycle,
            clock=self.time,
        )


@pytest.fixture
def environment(tmp_path: Path) -> Environment:
    return Environment(tmp_path / "schedule.sqlite3")


@pytest.mark.parametrize(
    ("enabled", "elapsed", "expected"),
    [
        (True, None, True),
        (True, timedelta(hours=5, minutes=59, seconds=59), False),
        (True, timedelta(hours=6), True),
        (True, timedelta(days=4), True),
        (True, timedelta(seconds=-1), False),
        (False, None, False),
        (False, timedelta(days=4), False),
    ],
)
def test_due_logic(enabled: bool, elapsed: timedelta | None, expected: bool) -> None:
    config = configuration(enabled=enabled)
    previous = None if elapsed is None else NOW - elapsed
    assert is_watch_due(config, now=NOW, last_attempt_at=previous) is expected
    assert is_watch_due(config, now=NOW, last_attempt_at=previous) is expected


def test_due_logic_uses_aware_utc_instants() -> None:
    local_now = NOW.astimezone(timezone(timedelta(hours=-7)))
    prior = (NOW - timedelta(hours=6)).astimezone(timezone(timedelta(hours=3)))
    assert is_watch_due(configuration(), now=local_now, last_attempt_at=prior)
    with pytest.raises(ValueError, match="timezone-aware"):
        is_watch_due(
            configuration(), now=NOW.replace(tzinfo=None), last_attempt_at=None
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        is_watch_due(configuration(), now=NOW, last_attempt_at=NOW.replace(tzinfo=None))


@pytest.mark.parametrize("interval", [timedelta(0), timedelta(seconds=-1)])
def test_invalid_watch_interval_is_rejected(interval: timedelta) -> None:
    with pytest.raises(ValidationError):
        ScheduledWatch.model_validate(
            configuration().model_dump() | {"check_interval": interval}
        )


def test_watch_policy_currency_is_validated_before_scheduling() -> None:
    with pytest.raises(ValidationError, match="currencies"):
        ScheduledWatch.model_validate(
            configuration().model_dump()
            | {"policy": AlertPolicy(currency="EUR", first_observation="suppress")}
        )


def test_pass_forwards_config_once_and_returns_existing_monitoring_result(
    environment: Environment,
) -> None:
    config = configuration()
    result = environment.scheduler(config).run_due_watches()
    assert result.watches_examined == result.watches_succeeded == 1
    assert result.watches_skipped == result.watches_failed == 0
    outcome = result.outcomes[0]
    assert outcome.status == "succeeded"
    assert outcome.checked_at == NOW
    assert outcome.watch_id == config.watch.watch_id
    assert outcome.failure is None
    assert outcome.result is not None
    assert outcome.result.status == "no_outbound_options"
    environment.cycle.assert_called_once()
    call = environment.cycle.call_args
    assert call.args == (TripQuery.model_validate(config.watch.model_dump()),)
    assert call.kwargs == {
        "constraints": config.constraints,
        "preferences": config.preferences,
        "policy": config.policy,
        "provider": environment.provider,
        "history": environment.history,
        "recommender": environment.recommender,
        "max_return_lookups": 2,
        "clock": call.kwargs["clock"],
    }
    assert call.kwargs["clock"]() == NOW
    environment.provider.search.assert_called_once_with(call.args[0])
    environment.provider.get_return_options.assert_not_called()
    environment.recommender.recommend.assert_not_called()
    with closing(connect(environment.repo.database_path)) as connection:
        assert connection.execute(
            "SELECT completed FROM monitoring_runs"
        ).fetchall() == [(1,)]


def test_recent_and_disabled_watches_do_not_invoke_monitoring(
    environment: Environment,
) -> None:
    recent, disabled = configuration(), configuration("HND", enabled=False)
    environment.repo.create_run(recent.watch, observed_at=NOW - timedelta(hours=1))
    result = environment.scheduler(recent, disabled).run_due_watches()
    assert [outcome.status for outcome in result.outcomes] == ["not_due", "disabled"]
    assert result.watches_examined == result.watches_skipped == 2
    assert result.watches_succeeded == result.watches_failed == 0
    environment.history.get_latest_run.assert_called_once_with(recent.watch)
    environment.cycle.assert_not_called()
    environment.provider.search.assert_not_called()


def test_multiple_due_watches_run_once_in_order_without_catchup(
    environment: Environment,
) -> None:
    configs = [configuration(), configuration("HND")]
    environment.repo.create_run(configs[0].watch, observed_at=NOW - timedelta(days=30))
    result = environment.scheduler(*configs).run_due_watches()
    assert result.watches_succeeded == 2
    assert [call.args[0].destination for call in environment.cycle.call_args_list] == [
        "SIN",
        "HND",
    ]
    assert environment.provider.search.call_count == 2
    assert [outcome.watch_id for outcome in result.outcomes] == [
        item.watch.watch_id for item in configs
    ]


def test_duplicate_watch_identity_rejected_before_io(environment: Environment) -> None:
    first = configuration()
    duplicate = first.model_copy(update={"check_interval": timedelta(hours=1)})
    with pytest.raises(ValueError, match="only once"):
        environment.scheduler(first, duplicate)
    environment.history.get_latest_run.assert_not_called()
    environment.cycle.assert_not_called()


def test_failure_isolated_and_persisted_attempt_defers_even_after_restart(
    environment: Environment,
) -> None:
    first, second = configuration(), configuration("HND")

    def search(query: TripQuery) -> list[FlightOption]:
        if query.destination == "SIN":
            raise FlightProviderError("scripted failure with private provider data")
        return []

    environment.provider.search.side_effect = search
    scheduler = environment.scheduler(first, second)
    result = scheduler.run_due_watches()
    assert result.watches_failed == result.watches_succeeded == 1
    assert [item.status for item in result.outcomes] == ["failed", "succeeded"]
    assert result.outcomes[0].result is None
    assert result.outcomes[0].failure is not None
    assert result.outcomes[0].failure.stage == "monitoring"
    assert result.outcomes[0].failure.error_type == "FlightProviderError"
    assert "private provider data" not in result.model_dump_json()
    with closing(connect(environment.repo.database_path)) as connection:
        assert connection.execute(
            "SELECT completed FROM monitoring_runs ORDER BY run_id"
        ).fetchall() == [(0,), (1,)]
    environment.time.now += timedelta(seconds=60)
    assert scheduler.run_due_watches().watches_skipped == 2
    assert environment.scheduler(first, second).run_due_watches().watches_skipped == 2
    assert environment.cycle.call_count == environment.provider.search.call_count == 2
    environment.time.now = NOW + timedelta(hours=6)
    assert scheduler.run_due_watches().watches_examined == 2
    assert environment.cycle.call_count == environment.provider.search.call_count == 4


@pytest.mark.parametrize("stage", ["get_latest_run", "create_run"])
def test_database_failure_before_run_creation_is_throttled_in_process(
    environment: Environment,
    stage: str,
) -> None:
    first, second = configuration(), configuration("HND")
    original = getattr(environment.repo, stage)

    def fail_first(watch: FareWatch, **kwargs: object) -> object:
        if watch == first.watch:
            raise OperationalError("database unavailable")
        return original(watch, **kwargs)

    getattr(environment.history, stage).side_effect = fail_first
    scheduler = environment.scheduler(first, second)
    initial = scheduler.run_due_watches()
    assert initial.watches_failed == initial.watches_succeeded == 1
    assert initial.outcomes[0].failure is not None
    assert initial.outcomes[0].failure.stage == (
        "history" if stage == "get_latest_run" else "monitoring"
    )
    assert environment.repo.get_latest_run(first.watch) is None
    count = getattr(environment.history, stage).call_count
    environment.time.now += timedelta(seconds=60)
    assert scheduler.run_due_watches().watches_skipped == 2
    assert getattr(environment.history, stage).call_count == count
    environment.time.now = NOW + timedelta(hours=6)
    assert scheduler.run_due_watches().watches_failed == 1
    assert getattr(environment.history, stage).call_count == count + 2


def test_later_watch_uses_its_actual_attempt_time(environment: Environment) -> None:
    configs = [configuration(), configuration("HND")]

    def slow_search(query: TripQuery) -> list[FlightOption]:
        environment.time.now += timedelta(minutes=10)
        return []

    environment.provider.search.side_effect = slow_search
    result = environment.scheduler(*configs).run_due_watches()
    assert [item.checked_at for item in result.outcomes] == [
        NOW,
        NOW + timedelta(minutes=10),
    ]
    assert [call.kwargs["clock"]() for call in environment.cycle.call_args_list] == [
        NOW,
        NOW + timedelta(minutes=10),
    ]
    assert environment.repo.get_latest_run(
        configs[1].watch
    ).observed_at == NOW + timedelta(minutes=10)


def test_polling_only_runs_due_watches_and_never_really_sleeps(
    environment: Environment,
) -> None:
    config = configuration().model_copy(
        update={"check_interval": timedelta(seconds=120)}
    )
    scheduler = environment.scheduler(config)
    results = list(
        scheduler.poll(
            poll_interval_seconds=60, sleep=environment.time.sleep, max_polls=4
        )
    )
    assert [item.watches_succeeded for item in results] == [1, 0, 1, 0]
    assert environment.cycle.call_count == environment.provider.search.call_count == 2
    assert environment.time.sleeps == [60, 60, 60]
    assert len(results) == 4


@pytest.mark.parametrize("interval", [0, -1, float("inf"), float("nan"), True])
def test_invalid_poll_interval(environment: Environment, interval: float) -> None:
    with pytest.raises(ValueError, match="positive and finite"):
        list(
            environment.scheduler(configuration()).poll(
                poll_interval_seconds=interval, max_polls=1
            )
        )
    environment.cycle.assert_not_called()


@pytest.mark.parametrize("limit", [-1, 1.5, True])
def test_invalid_poll_count(environment: Environment, limit: int) -> None:
    with pytest.raises(ValueError, match="nonnegative integer"):
        list(environment.scheduler(configuration()).poll(max_polls=limit))
    environment.cycle.assert_not_called()


def test_zero_polls_and_empty_configuration(environment: Environment) -> None:
    scheduler = environment.scheduler()
    assert list(scheduler.poll(sleep=environment.time.sleep, max_polls=0)) == []
    assert environment.time.sleeps == []
    result = scheduler.run_due_watches()
    assert (
        result.watches_examined
        == result.watches_skipped
        == result.watches_succeeded
        == result.watches_failed
        == 0
    )


def test_interrupt_propagates(environment: Environment) -> None:
    environment.cycle.side_effect = KeyboardInterrupt()
    with pytest.raises(KeyboardInterrupt):
        environment.scheduler(configuration()).run_due_watches()


def test_scheduler_preserves_true_alert_result_without_delivery(
    environment: Environment,
) -> None:
    def direction(origin: str, destination: str) -> FlightItinerary:
        return FlightItinerary(
            segments=(
                FlightSegment(
                    origin=origin,
                    destination=destination,
                    airline="Example",
                    flight_number="E1",
                    duration_minutes=300,
                ),
            )
        )

    trip = RoundTripItinerary(
        outbound=direction("LAX", "SIN"),
        inbound=direction("SIN", "LAX"),
        total_price="800",
    )
    environment.provider.search.return_value = [
        FlightOption(outbound=trip.outbound, price="800", departure_token="opaque")
    ]
    environment.provider.get_return_options.return_value = [trip]

    def choose(
        candidates: dict[str, RoundTripItinerary], *args: object, **kwargs: object
    ) -> Recommendation:
        selected = next(iter(candidates))
        return Recommendation(
            selected_candidate_id=selected,
            recommendation="Choose this option.",
            confidence="high",
            key_tradeoffs=(
                RecommendationTradeoff(
                    category="overall_value",
                    candidate_ids=(selected,),
                    favored_candidate_id=selected,
                    explanation="Matches preferences.",
                ),
            ),
        )

    environment.recommender.recommend.side_effect = choose
    result = environment.scheduler(configuration()).run_due_watches()
    monitoring = result.outcomes[0].result
    assert monitoring is not None and monitoring.alert_decision is not None
    assert monitoring.alert_decision.should_alert
    assert monitoring.selected_itinerary == trip
    environment.cycle.assert_called_once()
    environment.provider.search.assert_called_once()
    environment.provider.get_return_options.assert_called_once()
    environment.recommender.recommend.assert_called_once()
