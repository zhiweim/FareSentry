import sqlite3
from collections.abc import Mapping
from contextlib import closing
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from unittest.mock import Mock

import pytest

from faresentry.alerts import AlertPolicy
from faresentry.history import (
    FareObservation,
    FareWatch,
    MonitoringRun,
    itinerary_identity,
)
from faresentry.models import (
    AirportTransfer,
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
from faresentry.monitoring import (
    ConflictingItineraryError,
    FareHistoryRepository,
    MonitoringRunResult,
    Recommender,
    run_monitoring_cycle,
)
from faresentry.persistence import SQLiteFareHistory
from faresentry.providers.base import FlightProvider, FlightProviderError
from faresentry.recommendations import InvalidRecommendationError, RecommendationError

NOW = datetime(2026, 9, 10, tzinfo=UTC)


def direction(
    origin: str = "LAX",
    destination: str = "SIN",
    *,
    number: str = "EA 1",
    minutes: int = 300,
) -> FlightItinerary:
    return FlightItinerary(
        segments=(
            FlightSegment(
                origin=origin,
                destination=destination,
                airline="Example Air",
                flight_number=number,
                duration_minutes=minutes,
            ),
        )
    )


def fare(
    price: str = "800", *, number: str = "EA 2", inbound_minutes: int = 300
) -> RoundTripItinerary:
    return RoundTripItinerary(
        outbound=direction(),
        inbound=direction("SIN", "LAX", number=number, minutes=inbound_minutes),
        total_price=Decimal(price),
    )


def option(token: str | None = "lookup", *, price: str = "800") -> FlightOption:
    return FlightOption(
        outbound=direction(), price=Decimal(price), departure_token=token
    )


def recommendation(selected_id: str) -> Recommendation:
    return Recommendation(
        selected_candidate_id=selected_id,
        recommendation="This option fits the preferences.",
        confidence="medium",
        key_tradeoffs=(
            RecommendationTradeoff(
                category="overall_value",
                candidate_ids=(selected_id,),
                favored_candidate_id=selected_id,
                explanation="Fits the supplied preferences.",
            ),
        ),
    )


class Cycle:
    def __init__(self, path: Path) -> None:
        self.query = TripQuery(
            origin="LAX",
            destination="SIN",
            outbound_date="2026-12-01",
            return_date="2026-12-15",
        )
        self.watch = FareWatch.from_query(self.query)
        self.repo = SQLiteFareHistory(path)
        self.history = Mock(spec=FareHistoryRepository, wraps=self.repo)
        self.provider = Mock(spec=FlightProvider)
        self.provider.search.return_value = [option()]
        self.provider.get_return_options.return_value = [fare()]
        self.recommender = Mock(spec=Recommender)
        self.recommender.recommend.side_effect = self.choose
        self.clock = Mock(return_value=NOW)
        self.constraints = HardTravelConstraints(max_duration_minutes_per_direction=600)
        self.preferences = TravelerSoftPreferences(preferred_airlines=("Example Air",))
        self.policy = AlertPolicy(
            currency="USD",
            first_observation="suppress",
            minimum_absolute_improvement="100",
        )

    def choose(
        self,
        candidates: Mapping[str, RoundTripItinerary],
        preferences: TravelerSoftPreferences,
        *,
        constraints: HardTravelConstraints,
    ) -> Recommendation:
        assert preferences == self.preferences
        assert constraints == self.constraints
        # The model call happens only after every eligible observation is stored.
        run = self.history.record_observation.call_args.kwargs["run"]
        stored = [
            item
            for item in self.repo.get_recent_observations(self.watch)
            if item.run_id == run.run_id
        ]
        assert {item.itinerary_id for item in stored} == set(candidates)
        return recommendation(next(iter(candidates)))

    def run(self, *, max_return_lookups: int = 3) -> MonitoringRunResult:
        return run_monitoring_cycle(
            self.query,
            constraints=self.constraints,
            preferences=self.preferences,
            policy=self.policy,
            provider=self.provider,
            history=self.history,
            recommender=self.recommender,
            clock=self.clock,
            max_return_lookups=max_return_lookups,
        )


@pytest.fixture
def cycle(tmp_path: Path) -> Cycle:
    return Cycle(tmp_path / "fares.sqlite3")


def assert_empty(cycle: Cycle, result: MonitoringRunResult, status: str) -> None:
    assert result.status == status
    assert result.recommendation is None
    assert result.alert_decision is None
    assert result.selected_candidate_id is None
    assert result.selected_itinerary is None
    assert result.eligible_candidates == 0
    assert result.watch_id == cycle.watch.watch_id
    cycle.history.create_run.assert_called_once_with(cycle.watch, observed_at=NOW)
    cycle.history.mark_run_completed.assert_called_once()
    cycle.provider.search.assert_called_once_with(cycle.query)
    cycle.recommender.recommend.assert_not_called()
    cycle.history.record_observation.assert_not_called()
    cycle.history.get_prior_observations.assert_not_called()
    with sqlite3.connect(cycle.repo.database_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM monitoring_runs"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT completed FROM monitoring_runs"
        ).fetchone() == (1,)


def test_complete_cycle_and_prior_history(cycle: Cycle) -> None:
    # Earlier run tied on timestamp, future run created before current, and legacy
    # observations exercise run boundaries independently of insertion order.
    prior = cycle.repo.create_run(cycle.watch, observed_at=NOW)
    cycle.repo.record_observation(cycle.watch, fare("1000"), run=prior)
    cycle.repo.record_observation(cycle.watch, fare("1200", number="EA 3"), run=prior)
    cycle.repo.record_observation(
        cycle.watch, fare("1", number="EA bad", inbound_minutes=900), run=prior
    )
    future = cycle.repo.create_run(cycle.watch, observed_at=NOW + timedelta(days=1))
    cycle.repo.record_observation(cycle.watch, fare("2"), run=future)
    for run in (prior, future):
        cycle.repo.mark_run_completed(cycle.watch, run=run)
    cycle.repo.record_observation(
        cycle.watch, fare("3"), observed_at=NOW - timedelta(days=1)
    )
    selected, alternative = fare("800"), fare("600", number="EA 4")
    rejected = fare("5", number="EA 5", inbound_minutes=900)
    choices = [option(None), option("first"), option("second"), option("over-budget")]
    cycle.provider.search.return_value = choices
    cycle.provider.get_return_options.side_effect = [
        [selected, rejected],
        [selected, alternative],
    ]

    result = cycle.run(max_return_lookups=2)

    assert result.status == "completed"
    assert result.outbound_choices_found == 4
    assert result.return_lookups_attempted == 2
    assert result.completed_round_trips == 3
    assert result.duplicate_round_trips == 1
    assert result.rejected_by_constraints == 1
    assert result.eligible_candidates == 2
    assert result.selected_candidate_id == itinerary_identity(cycle.watch, selected)
    assert result.selected_itinerary == selected
    cycle.provider.search.assert_called_once_with(cycle.query)
    assert [
        call.args[0] for call in cycle.provider.get_return_options.call_args_list
    ] == (choices[1:3])
    assert all(
        call.args[1] == cycle.query
        for call in cycle.provider.get_return_options.call_args_list
    )
    cycle.clock.assert_called_once_with()
    cycle.history.create_run.assert_called_once_with(cycle.watch, observed_at=NOW)
    cycle.recommender.recommend.assert_called_once()
    candidates = cycle.recommender.recommend.call_args.args[0]
    assert list(candidates.values()) == [selected, alternative]
    assert all(
        key == itinerary_identity(cycle.watch, trip) for key, trip in candidates.items()
    )
    stored = [
        item
        for item in cycle.repo.get_recent_observations(cycle.watch)
        if item.run_id == result.run_id
    ]
    assert {item.itinerary_id for item in stored} == set(candidates)
    assert cycle.history.record_observation.call_count == 2
    current_run = cycle.history.record_observation.call_args.kwargs["run"]
    cycle.history.mark_run_completed.assert_called_once_with(
        cycle.watch, run=current_run
    )
    cycle.history.get_prior_observations.assert_called_once_with(
        cycle.watch, before_run=current_run
    )
    decision = result.alert_decision
    assert decision is not None
    assert decision.current_price == Decimal("800")
    assert decision.prior_observation_count == 2
    assert decision.prior_same_itinerary_count == 1
    assert decision.previous_same_itinerary_price == Decimal("1000")
    assert decision.prior_watch_low == Decimal("1000")
    assert decision.prior_watch_average == Decimal("1100")
    assert decision.absolute_improvement == Decimal("200")
    assert decision.should_alert
    assert "departure_token" not in result.model_dump_json()
    with sqlite3.connect(cycle.repo.database_path) as connection:
        assert (
            connection.execute("SELECT COUNT(*) FROM monitoring_runs").fetchone()[0]
            == 3
        )


def test_no_outbound_options(cycle: Cycle) -> None:
    cycle.provider.search.return_value = []
    result = cycle.run()
    assert_empty(cycle, result, "no_outbound_options")
    cycle.provider.get_return_options.assert_not_called()


@pytest.mark.parametrize("token", [None, "", " \t "])
def test_no_usable_tokens(cycle: Cycle, token: str | None) -> None:
    cycle.provider.search.return_value = [option(token)]
    result = cycle.run()
    assert_empty(cycle, result, "no_usable_departure_tokens")
    cycle.provider.get_return_options.assert_not_called()


def test_no_compatible_returns(cycle: Cycle) -> None:
    cycle.provider.get_return_options.return_value = []
    result = cycle.run()
    assert_empty(cycle, result, "no_completed_itineraries")
    assert result.return_lookups_attempted == 1
    assert result.completed_round_trips == 0


def test_all_completed_fail_constraints(cycle: Cycle) -> None:
    cycle.provider.get_return_options.return_value = [fare(inbound_minutes=900)]
    result = cycle.run()
    assert_empty(cycle, result, "no_eligible_candidates")
    assert result.rejected_by_constraints == result.completed_round_trips == 1


@pytest.mark.parametrize("limit", [1, 2, 3, 5])
def test_lookup_budget_uses_provider_order_and_skips_tokens(
    cycle: Cycle, limit: int
) -> None:
    choices = [option(None), option(""), option("   ")] + [
        option(str(index), price=str(1000 - index)) for index in range(5)
    ]
    cycle.provider.search.return_value = choices
    result = cycle.run(max_return_lookups=limit)
    assert result.return_lookups_attempted == limit
    assert cycle.provider.get_return_options.call_count == limit
    assert [
        call.args[0] for call in cycle.provider.get_return_options.call_args_list
    ] == (choices[3 : 3 + limit])
    assert result.completed_round_trips == result.eligible_candidates == 1
    assert result.duplicate_round_trips == limit - 1
    cycle.recommender.recommend.assert_called_once()


def test_default_lookup_budget(cycle: Cycle) -> None:
    cycle.provider.search.return_value = [option(str(index)) for index in range(10)]
    assert cycle.run().return_lookups_attempted == 3


@pytest.mark.parametrize("rule", ["stops", "duration", "connection", "transfer"])
@pytest.mark.parametrize("include_passing", [False, True])
def test_outbound_prescreen_reuses_each_constraint(
    cycle: Cycle, rule: str, include_passing: bool
) -> None:
    transfer = rule == "transfer"
    connection = (
        AirportTransfer(
            arrival_airport="NRT", departure_airport="HND", duration_minutes=90
        )
        if transfer
        else Layover(airport="NRT", duration_minutes=90)
    )
    outbound = FlightItinerary(
        segments=(
            direction("LAX", "NRT").segments[0],
            direction("HND" if transfer else "NRT", "SIN").segments[0],
        ),
        layovers=(connection,),
    )
    cycle.constraints = {
        "stops": HardTravelConstraints(max_stops_per_direction=0),
        "duration": HardTravelConstraints(max_duration_minutes_per_direction=600),
        "connection": HardTravelConstraints(max_connection_duration_minutes=60),
        "transfer": HardTravelConstraints(allow_airport_transfers=False),
    }[rule]
    rejected = FlightOption(outbound=outbound, price="1", departure_token="reject")
    cycle.provider.search.return_value = [rejected] + (
        [option()] if include_passing else []
    )
    result = cycle.run(max_return_lookups=1)
    assert result.outbound_choices_rejected == 1
    if include_passing:
        assert result.status == "completed"
        cycle.provider.get_return_options.assert_called_once_with(option(), cycle.query)
    else:
        assert_empty(cycle, result, "no_eligible_outbound_options")
        cycle.provider.get_return_options.assert_not_called()


def test_duplicate_decimal_scale_is_equal(cycle: Cycle) -> None:
    cycle.provider.get_return_options.return_value = [fare("800"), fare("800.00")]
    result = cycle.run()
    assert result.duplicate_round_trips == 1
    assert result.eligible_candidates == 1
    cycle.history.record_observation.assert_called_once()


def test_conflicting_fares_fail_before_observations(cycle: Cycle) -> None:
    cycle.provider.search.return_value = [option("a"), option("b")]
    cycle.provider.get_return_options.side_effect = [[fare("800")], [fare("900")]]
    with pytest.raises(ConflictingItineraryError, match="fare products"):
        cycle.run()
    cycle.history.record_observation.assert_not_called()
    cycle.recommender.recommend.assert_not_called()


def test_only_eligible_fares_become_next_cycles_history(cycle: Cycle) -> None:
    cycle.provider.get_return_options.return_value = [
        fare("1000"),
        fare("1", number="bad", inbound_minutes=900),
    ]
    first = cycle.run()
    cycle.provider.get_return_options.return_value = [fare("800", number="new")]
    second = cycle.run()
    assert first.alert_decision is not None
    assert first.alert_decision.prior_observation_count == 0
    assert not first.alert_decision.should_alert
    assert second.alert_decision is not None
    assert second.alert_decision.prior_observation_count == 1
    assert second.alert_decision.prior_same_itinerary_count == 0
    assert second.alert_decision.improvement_reference_type == "prior_watch_low"
    assert second.alert_decision.prior_watch_low == Decimal("1000")
    assert second.alert_decision.should_alert
    assert len(cycle.repo.get_recent_observations(cycle.watch)) == 2


def test_repeated_inputs_with_identical_injected_context_are_deterministic(
    tmp_path: Path,
) -> None:
    first = Cycle(tmp_path / "first.sqlite3")
    second = Cycle(tmp_path / "second.sqlite3")
    assert first.run() == second.run()


@pytest.mark.parametrize("stage", ["search", "get_return_options"])
def test_provider_failure_propagates(cycle: Cycle, stage: str) -> None:
    error = FlightProviderError("Flight request failed")
    getattr(cycle.provider, stage).side_effect = error
    with pytest.raises(FlightProviderError) as raised:
        cycle.run()
    assert raised.value is error
    cycle.history.create_run.assert_called_once()
    cycle.history.record_observation.assert_not_called()
    cycle.recommender.recommend.assert_not_called()
    cycle.history.mark_run_completed.assert_not_called()
    with closing(sqlite3.connect(cycle.repo.database_path)) as connection:
        assert connection.execute(
            "SELECT completed FROM monitoring_runs"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "stage", ["create_run", "record_observation", "get_prior_observations"]
)
def test_persistence_failure_propagates(cycle: Cycle, stage: str) -> None:
    error = sqlite3.OperationalError("database is locked")
    getattr(cycle.history, stage).side_effect = error
    with pytest.raises(sqlite3.OperationalError) as raised:
        cycle.run()
    assert raised.value is error
    if stage == "create_run":
        cycle.provider.search.assert_not_called()
    if stage != "get_prior_observations":
        cycle.recommender.recommend.assert_not_called()


def test_recommendation_failure_preserves_observations(cycle: Cycle) -> None:
    error = RecommendationError("Recommendation model request failed")
    cycle.recommender.recommend.side_effect = error
    with pytest.raises(RecommendationError) as raised:
        cycle.run()
    assert raised.value is error
    assert len(cycle.repo.get_recent_observations(cycle.watch)) == 1
    cycle.history.get_prior_observations.assert_not_called()
    cycle.recommender.recommend.assert_called_once()


@pytest.mark.parametrize("invalid_reference", ["selected", "tradeoff", "rejected"])
def test_invalid_recommendation_reference_is_rejected(
    cycle: Cycle, invalid_reference: str
) -> None:
    rejected = fare("1", number="bad", inbound_minutes=900)
    cycle.provider.get_return_options.return_value = [fare(), rejected]
    output = recommendation(itinerary_identity(cycle.watch, fare())).model_dump()
    if invalid_reference == "tradeoff":
        output["key_tradeoffs"][0]["candidate_ids"] = ("unknown",)
    else:
        output = recommendation(
            itinerary_identity(cycle.watch, rejected)
            if invalid_reference == "rejected"
            else "unknown"
        ).model_dump()
    cycle.recommender.recommend.side_effect = None
    cycle.recommender.recommend.return_value = output
    with pytest.raises(InvalidRecommendationError):
        cycle.run()
    cycle.history.get_prior_observations.assert_not_called()


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "3"])
def test_invalid_budget_fails_before_io(cycle: Cycle, limit: object) -> None:
    with pytest.raises(ValueError, match="positive integer"):
        cycle.run(max_return_lookups=limit)
    cycle.history.create_run.assert_not_called()
    cycle.provider.search.assert_not_called()


def test_policy_currency_mismatch_fails_before_io(cycle: Cycle) -> None:
    cycle.policy = AlertPolicy(currency="EUR", first_observation="suppress")
    with pytest.raises(ValueError, match="currencies"):
        cycle.run()
    cycle.history.create_run.assert_not_called()
    cycle.provider.search.assert_not_called()


@pytest.mark.parametrize("stage", ["outbound", "completed"])
def test_incompatible_provider_output_is_a_failure(cycle: Cycle, stage: str) -> None:
    if stage == "outbound":
        cycle.provider.search.return_value = [
            option().model_copy(update={"currency": "EUR"})
        ]
    else:
        cycle.provider.get_return_options.return_value = [
            fare().model_copy(update={"outbound": direction(number="wrong flight")})
        ]
    with pytest.raises(FlightProviderError):
        cycle.run()
    cycle.history.record_observation.assert_not_called()
    cycle.recommender.recommend.assert_not_called()


@pytest.mark.parametrize(
    "stage",
    [
        "recommendation",
        "alert",
        "partial_persistence",
        "prior_history",
        "validation",
        "result",
        "completion",
    ],
)
def test_failed_cycle_cannot_advance_future_alert_baseline(
    cycle: Cycle, monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    cycle.provider.get_return_options.return_value = [fare("1000")]
    first = cycle.run()
    assert first.alert_decision is not None
    assert not first.alert_decision.should_alert  # A normal false decision completes.
    cycle.provider.get_return_options.return_value = [fare("800")]
    error: Exception = RuntimeError("scripted failure")
    with monkeypatch.context() as patch:
        if stage == "recommendation":
            error = RecommendationError("scripted failure")
            patch.setattr(cycle.recommender.recommend, "side_effect", error)
        elif stage == "alert":
            patch.setattr(
                "faresentry.monitoring.evaluate_alert", Mock(side_effect=error)
            )
        elif stage == "partial_persistence":
            error = sqlite3.OperationalError("scripted failure")
            cycle.provider.get_return_options.return_value = [
                fare("800"),
                fare("700", number="other"),
            ]

            def partial_write(
                watch: FareWatch, itinerary: RoundTripItinerary, *, run: MonitoringRun
            ) -> FareObservation:
                if itinerary.total_price == Decimal("700"):
                    raise error
                return cycle.repo.record_observation(watch, itinerary, run=run)

            patch.setattr(
                cycle.history.record_observation, "side_effect", partial_write
            )
        elif stage == "prior_history":
            error = sqlite3.OperationalError("scripted failure")
            patch.setattr(cycle.history.get_prior_observations, "side_effect", error)
        elif stage == "validation":
            error = InvalidRecommendationError("unknown ID")
            patch.setattr(cycle.recommender.recommend, "side_effect", None)
            patch.setattr(
                cycle.recommender.recommend, "return_value", recommendation("unknown")
            )
        elif stage == "result":
            patch.setattr(
                "faresentry.monitoring.MonitoringRunResult", Mock(side_effect=error)
            )
        else:
            # A real SQLite write failure must leave the marker uncommitted.
            error = sqlite3.IntegrityError("completion failure")
            with (
                closing(sqlite3.connect(cycle.repo.database_path)) as connection,
                connection,
            ):
                connection.execute(
                    """CREATE TRIGGER reject_completion
                       BEFORE UPDATE OF completed ON monitoring_runs
                       BEGIN SELECT RAISE(ABORT, 'completion failure'); END"""
                )
        with pytest.raises(type(error)):
            cycle.run()

    with closing(sqlite3.connect(cycle.repo.database_path)) as connection, connection:
        assert connection.execute(
            "SELECT completed FROM monitoring_runs ORDER BY run_id"
        ).fetchall() == [(1,), (0,)]
        if stage == "completion":
            connection.execute("DROP TRIGGER reject_completion")
    stored = cycle.repo.get_recent_observations(cycle.watch)
    assert [item.total_price for item in stored] == [Decimal("1000"), Decimal("800")]
    assert stored[1].run_id != first.run_id

    cycle.provider.get_return_options.return_value = [fare("800")]
    recovered = cycle.run()
    decision = recovered.alert_decision
    assert decision is not None
    assert decision.prior_observation_count == decision.prior_same_itinerary_count == 1
    assert decision.previous_same_itinerary_price == Decimal("1000")
    assert decision.prior_watch_low == decision.prior_watch_average == Decimal("1000")
    assert decision.absolute_improvement == Decimal("200")
    assert decision.should_alert
    assert len(cycle.repo.get_recent_observations(cycle.watch)) == 3
    with closing(sqlite3.connect(cycle.repo.database_path)) as connection:
        assert connection.execute(
            "SELECT completed FROM monitoring_runs ORDER BY run_id"
        ).fetchall() == [(1,), (0,), (1,)]


def test_empty_result_completion_failure_propagates(cycle: Cycle) -> None:
    cycle.provider.search.return_value = []
    cycle.history.mark_run_completed.side_effect = sqlite3.OperationalError(
        "database is locked"
    )
    with pytest.raises(sqlite3.OperationalError):
        cycle.run()
    with closing(sqlite3.connect(cycle.repo.database_path)) as connection:
        assert connection.execute(
            "SELECT completed FROM monitoring_runs"
        ).fetchone() == (0,)
