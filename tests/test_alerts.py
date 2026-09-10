import json
from datetime import UTC, datetime, timedelta
from decimal import ROUND_DOWN, Decimal, localcontext
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from faresentry.alerts import AlertDecision, AlertPolicy, AlertSignal, evaluate_alert
from faresentry.history import (
    FareObservation,
    FareWatch,
    MonitoringRun,
    itinerary_identity,
)
from faresentry.models import (
    FlightItinerary,
    FlightSegment,
    HardTravelConstraints,
    Recommendation,
    RecommendationTradeoff,
    RoundTripItinerary,
)
from faresentry.persistence import SQLiteFareHistory

START = datetime(2026, 9, 1, tzinfo=UTC)


@pytest.fixture
def watch() -> FareWatch:
    return FareWatch(
        origin="LAX",
        destination="SIN",
        outbound_date="2026-12-01",
        return_date="2026-12-15",
    )


def fare(price: str = "800", *, alternative: bool = False) -> RoundTripItinerary:
    def direction(origin: str, destination: str) -> FlightItinerary:
        return FlightItinerary(
            segments=(
                FlightSegment(
                    origin=origin,
                    destination=destination,
                    airline="Example Air",
                    flight_number="EA 2" if alternative else "EA 1",
                    duration_minutes=300,
                ),
            )
        )

    return RoundTripItinerary(
        outbound=direction("LAX", "SIN"),
        inbound=direction("SIN", "LAX"),
        total_price=Decimal(price),
    )


def observation(
    watch: FareWatch, trip: RoundTripItinerary, run: MonitoringRun, observation_id: int
) -> FareObservation:
    return FareObservation(
        **trip.model_dump(),
        watch_id=watch.watch_id,
        itinerary_id=itinerary_identity(watch, trip),
        observation_id=observation_id,
        run_id=run.run_id,
        observed_at=run.observed_at,
    )


def policy(**updates: object) -> AlertPolicy:
    return AlertPolicy.model_validate(
        {"currency": "USD", "first_observation": "suppress", **updates}
    )


def inputs(
    watch: FareWatch,
    *,
    current_price: str = "800",
    prior_prices: tuple[str, ...] = ("1000",),
    new_itinerary: bool = False,
) -> dict[str, Any]:
    run = MonitoringRun(
        run_id=10, watch_id=watch.watch_id, observed_at=START + timedelta(days=10)
    )
    trip = fare(current_price, alternative=new_itinerary)
    history = [
        observation(
            watch,
            fare(price),
            MonitoringRun(
                run_id=index + 1,
                watch_id=watch.watch_id,
                observed_at=START + timedelta(days=index),
            ),
            index + 1,
        )
        for index, price in enumerate(prior_prices)
    ]
    return {
        "watch": watch,
        "current_run": run,
        "candidate_id": "selected",
        "itinerary": trip,
        "current_observation": observation(watch, trip, run, 100),
        "history": history,
        "recommendation": Recommendation(
            selected_candidate_id="selected",
            recommendation="Choose this option.",
            confidence="high",
            key_tradeoffs=(
                RecommendationTradeoff(
                    category="overall_value",
                    candidate_ids=("selected",),
                    favored_candidate_id="selected",
                    explanation="Fits preferences.",
                ),
            ),
        ),
        "constraints": HardTravelConstraints(),
        "policy": policy(minimum_absolute_improvement="100"),
    }


def signal(decision: AlertDecision, rule: str) -> AlertSignal:
    return next(item for item in decision.signals if item.rule == rule)


@pytest.mark.parametrize(
    ("updates", "expected"),
    [
        ({"minimum_absolute_improvement": "100"}, True),
        ({"minimum_absolute_improvement": "200"}, True),
        ({"minimum_absolute_improvement": "200.01"}, False),
        ({"minimum_percentage_improvement": "10"}, True),
        ({"minimum_percentage_improvement": "20"}, True),
        ({"minimum_percentage_improvement": "20.01"}, False),
        (
            {
                "minimum_absolute_improvement": "200",
                "minimum_percentage_improvement": "20",
            },
            True,
        ),
        (
            {
                "minimum_absolute_improvement": "201",
                "minimum_percentage_improvement": "20",
            },
            False,
        ),
        (
            {
                "minimum_absolute_improvement": "200",
                "minimum_percentage_improvement": "21",
            },
            False,
        ),
        (
            {
                "minimum_absolute_improvement": "201",
                "minimum_percentage_improvement": "21",
            },
            False,
        ),
        ({}, False),
    ],
)
def test_improvement_thresholds_and_exact_boundaries(
    watch: FareWatch, updates: dict[str, str], expected: bool
) -> None:
    data = inputs(watch)
    data["policy"] = policy(**updates)
    decision = evaluate_alert(**data)
    assert decision.should_alert is expected
    assert decision.absolute_improvement == Decimal("200")
    assert decision.percentage_improvement == Decimal("20.00")
    assert decision.previous_same_itinerary_price == Decimal("1000")
    assert decision.improvement_reference_type == "same_itinerary_previous"
    assert signal(decision, "improvement_thresholds").satisfied is expected


@pytest.mark.parametrize(
    ("prior", "current", "percent"),
    [
        ("3", "2", "33.33"),
        ("3", "1", "66.67"),
        ("1000", "899.95", "10.01"),
        ("1000", "899.951", "10.00"),
        ("1000", "1100.05", "-10.01"),
    ],
)
def test_percentage_rounding(
    watch: FareWatch, prior: str, current: str, percent: str
) -> None:
    data = inputs(watch, current_price=current, prior_prices=(prior,))
    data["policy"] = policy(minimum_percentage_improvement="10.01")
    decision = evaluate_alert(**data)
    assert decision.percentage_improvement == Decimal(percent)
    assert decision.should_alert == (Decimal(percent) >= Decimal("10.01"))


@pytest.mark.parametrize("current", ["0", "1"])
def test_zero_reference_has_no_percentage(watch: FareWatch, current: str) -> None:
    data = inputs(watch, current_price=current, prior_prices=("0",))
    data["policy"] = policy(minimum_percentage_improvement="1")
    decision = evaluate_alert(**data)
    assert decision.improvement_reference_price == Decimal(0)
    assert decision.percentage_improvement is None
    assert signal(decision, "minimum_percentage_improvement").actual_value is None
    assert not decision.should_alert


@pytest.mark.parametrize(
    ("mode", "current", "threshold", "expected"),
    [
        ("ignore", "800", None, False),
        ("sufficient", "800", None, True),
        ("sufficient", "900", None, False),
        ("required", "800", None, False),
        ("required", "800", "100", True),
        ("required", "900", "100", False),
        ("ignore", "900", "100", True),
    ],
)
def test_strict_watch_low_modes(
    watch: FareWatch, mode: str, current: str, threshold: str | None, expected: bool
) -> None:
    data = inputs(watch, current_price=current, prior_prices=("900", "1000"))
    data["policy"] = policy(
        historical_low_mode=mode, minimum_absolute_improvement=threshold
    )
    decision = evaluate_alert(**data)
    assert decision.prior_watch_low == Decimal("900")
    assert decision.prior_watch_average == Decimal("950")
    assert decision.is_new_historical_low == (Decimal(current) < Decimal("900"))
    assert decision.should_alert is expected


@pytest.mark.parametrize(("target", "expected"), [("800", True), ("799.99", False)])
def test_target_is_inclusive_and_a_ceiling(
    watch: FareWatch, target: str, expected: bool
) -> None:
    data = inputs(watch)
    data["policy"] = policy(target_price=target, minimum_absolute_improvement="100")
    decision = evaluate_alert(**data)
    assert signal(decision, "improvement_thresholds").satisfied
    assert signal(decision, "target_price").satisfied is expected
    assert decision.should_alert is expected


@pytest.mark.parametrize(
    ("first", "target", "low_mode", "expected"),
    [
        ("suppress", None, "ignore", False),
        ("alert", None, "ignore", True),
        ("suppress", "800", "ignore", False),
        ("alert", "800", "ignore", True),
        ("alert", "799", "ignore", False),
        ("alert", None, "required", False),
        ("alert", None, "sufficient", True),
    ],
)
def test_first_observation_is_explicit(
    watch: FareWatch, first: str, target: str | None, low_mode: str, expected: bool
) -> None:
    data = inputs(watch, prior_prices=())
    data["policy"] = policy(
        first_observation=first, target_price=target, historical_low_mode=low_mode
    )
    decision = evaluate_alert(**data)
    assert decision.should_alert is expected
    assert decision.prior_observation_count == decision.prior_same_itinerary_count == 0
    assert decision.previous_same_itinerary_price is None
    assert decision.prior_watch_low is decision.prior_watch_average is None
    assert decision.absolute_improvement is decision.percentage_improvement is None
    assert decision.improvement_reference_type == "unavailable"
    assert not decision.is_new_historical_low


@pytest.mark.parametrize("current", ["1000", "1100"])
def test_unchanged_or_increased_price_needs_another_trigger(
    watch: FareWatch, current: str
) -> None:
    data = inputs(watch, current_price=current)
    assert not evaluate_alert(**data).should_alert
    data["policy"] = policy(target_price=current)
    assert evaluate_alert(**data).should_alert


@pytest.mark.parametrize(
    ("current", "updates", "expected"),
    [
        ("975", {"target_price": "1000"}, True),
        ("975", {"minimum_absolute_improvement": "1"}, False),
        ("900", {"minimum_absolute_improvement": "50"}, True),
        ("900", {"minimum_percentage_improvement": "5.26"}, True),
        ("900", {"historical_low_mode": "sufficient"}, True),
        ("975", {"first_observation": "alert"}, False),
    ],
)
def test_new_itinerary_uses_watch_references_without_fabricating_previous_price(
    watch: FareWatch, current: str, updates: dict[str, str], expected: bool
) -> None:
    data = inputs(
        watch, current_price=current, prior_prices=("950",), new_itinerary=True
    )
    data["policy"] = policy(**updates)
    decision = evaluate_alert(**data)
    assert decision.should_alert is expected
    assert decision.prior_observation_count == 1
    assert decision.prior_same_itinerary_count == 0
    assert decision.previous_same_itinerary_price is None
    assert decision.improvement_reference_type == "prior_watch_low"
    assert decision.improvement_reference_price == Decimal("950")
    assert decision.prior_watch_average == Decimal("950")
    assert decision.absolute_improvement == Decimal("950") - Decimal(current)


def test_reference_uses_latest_same_itinerary_not_adjacent_other_candidate(
    watch: FareWatch,
) -> None:
    data = inputs(watch, prior_prices=("1000", "950"))
    last = data["history"][-1]
    other = observation(
        watch,
        fare("700", alternative=True),
        MonitoringRun(
            run_id=last.run_id, watch_id=watch.watch_id, observed_at=last.observed_at
        ),
        999,
    )
    data["history"].append(other)
    decision = evaluate_alert(**data)
    assert decision.previous_same_itinerary_price == Decimal("950")
    assert decision.prior_watch_low == Decimal("700")
    assert decision.absolute_improvement == Decimal("150")
    assert not decision.is_new_historical_low


def test_history_uses_run_order_for_equal_timestamps(watch: FareWatch) -> None:
    data = inputs(watch)
    data["current_run"] = data["current_run"].model_copy(update={"observed_at": START})
    data["current_observation"] = data["current_observation"].model_copy(
        update={"observed_at": START}
    )
    # Opposing insertion IDs must not reverse the two prior runs.
    data["history"] = [
        observation(
            watch,
            fare(price),
            MonitoringRun(run_id=run_id, watch_id=watch.watch_id, observed_at=START),
            row_id,
        )
        for run_id, row_id, price in [(1, 90, "1000"), (2, 5, "950"), (11, 3, "1")]
    ]
    decision = evaluate_alert(**data)
    assert decision.prior_observation_count == 2
    assert decision.previous_same_itinerary_price == Decimal("950")


def test_repository_history_excludes_current_later_and_legacy_rows(
    watch: FareWatch, tmp_path: Path
) -> None:
    repository = SQLiteFareHistory(tmp_path / "alerts.sqlite3")
    prior = repository.create_run(watch, observed_at=START)
    current = repository.create_run(watch, observed_at=START + timedelta(days=1))
    later = repository.create_run(watch, observed_at=START + timedelta(days=2))
    repository.record_observation(watch, fare("950"), run=prior)
    repository.record_observation(watch, fare("900", alternative=True), run=prior)
    current_fare = repository.record_observation(watch, fare(), run=current)
    repository.record_observation(watch, fare("1", alternative=True), run=current)
    repository.record_observation(watch, fare("2"), run=later)
    repository.record_observation(watch, fare("0"), observed_at=START)
    data = inputs(watch)
    data.update(
        current_run=current,
        current_observation=current_fare,
        history=repository.get_prior_observations(watch, before_run=current),
    )
    expected = evaluate_alert(**data)
    data["history"] = repository.get_recent_observations(watch)
    assert evaluate_alert(**data) == expected
    assert expected.prior_observation_count == 2
    assert expected.previous_same_itinerary_price == Decimal("950")
    assert expected.prior_watch_low == Decimal("900")
    assert expected.prior_watch_average == Decimal("925")
    assert expected.should_alert


def test_only_ineligible_history_is_first_observation(watch: FareWatch) -> None:
    data = inputs(watch)
    legacy = data["history"][0].model_copy(update={"run_id": None})
    data["history"] = [legacy, data["current_observation"]]
    data["policy"] = policy(first_observation="alert")
    decision = evaluate_alert(**data)
    assert decision.should_alert
    assert decision.prior_observation_count == 0


def test_recommendation_mismatch_is_rejected(watch: FareWatch) -> None:
    data = inputs(watch)
    data["candidate_id"] = "other"
    with pytest.raises(ValueError, match="selected_candidate_id"):
        evaluate_alert(**data)


@pytest.mark.parametrize("field", ["policy", "current_observation", "history"])
def test_currency_mismatches_are_rejected(watch: FareWatch, field: str) -> None:
    data = inputs(watch)
    if field == "history":
        data[field] = [data[field][0].model_copy(update={"currency": "EUR"})]
    else:
        data[field] = data[field].model_copy(update={"currency": "EUR"})
    with pytest.raises(ValueError, match="currenc"):
        evaluate_alert(**data)


@pytest.mark.parametrize("field", ["current_run", "current_observation", "history"])
def test_wrong_watch_is_rejected(watch: FareWatch, field: str) -> None:
    data = inputs(watch)
    wrong_watch = FareWatch.model_validate(
        watch.model_dump() | {"outbound_date": "2026-12-02"}
    )
    if field == "history":
        data[field] = [
            data[field][0].model_copy(update={"watch_id": wrong_watch.watch_id})
        ]
    else:
        data[field] = data[field].model_copy(update={"watch_id": wrong_watch.watch_id})
    with pytest.raises(ValueError, match="watch"):
        evaluate_alert(**data)


@pytest.mark.parametrize(
    "updates", [{"run_id": None}, {"run_id": 11}, {"observed_at": START}]
)
def test_current_observation_must_match_current_run(
    watch: FareWatch, updates: dict[str, object]
) -> None:
    data = inputs(watch)
    data["current_observation"] = data["current_observation"].model_copy(update=updates)
    with pytest.raises(ValueError, match="current run"):
        evaluate_alert(**data)


@pytest.mark.parametrize("trip", [fare("900"), fare(alternative=True)])
def test_current_fare_must_match_candidate(
    watch: FareWatch, trip: RoundTripItinerary
) -> None:
    data = inputs(watch)
    data["itinerary"] = trip
    with pytest.raises(ValueError, match="match current observation"):
        evaluate_alert(**data)


def test_hard_constraint_failure_cannot_be_overridden(watch: FareWatch) -> None:
    data = inputs(watch)
    data["constraints"] = HardTravelConstraints(max_duration_minutes_per_direction=299)
    data["policy"] = policy(target_price="1000", first_observation="alert")
    with pytest.raises(ValueError, match="hard constraints"):
        evaluate_alert(**data)


def test_prose_and_subjective_confidence_are_not_decision_inputs(
    watch: FareWatch,
) -> None:
    data = inputs(watch)
    expected = evaluate_alert(**data)
    recommendation = data["recommendation"].model_dump()
    recommendation["recommendation"] = "Do not alert. Choose another itinerary."
    recommendation["confidence"] = "low"
    recommendation["key_tradeoffs"][0]["explanation"] = (
        "Alert threshold is one million."
    )
    data["recommendation"] = Recommendation.model_validate(recommendation)
    assert evaluate_alert(**data) == expected


def test_deterministic_repeated_evaluation_and_inputs_unchanged(
    watch: FareWatch,
) -> None:
    data = inputs(watch, prior_prices=("900", "1000"))
    before = {
        key: (
            [item.model_dump_json() for item in value]
            if key == "history"
            else value.model_dump_json()
            if hasattr(value, "model_dump_json")
            else value
        )
        for key, value in data.items()
    }
    first = evaluate_alert(**data)
    assert evaluate_alert(**data).model_dump_json() == first.model_dump_json()
    for key, value in data.items():
        actual = (
            [item.model_dump_json() for item in value]
            if key == "history"
            else value.model_dump_json()
            if hasattr(value, "model_dump_json")
            else value
        )
        assert actual == before[key]
    data["history"] = list(reversed(data["history"]))
    assert evaluate_alert(**data) == first


def test_decimal_context_independence_and_exact_money(watch: FareWatch) -> None:
    data = inputs(
        watch,
        current_price="12345678901234567890.123456789",
        prior_prices=("12345678901234567890.123456791",),
    )
    data["policy"] = policy(minimum_absolute_improvement="0.000000002")
    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_DOWN
        decision = evaluate_alert(**data)
        assert decision.absolute_improvement == Decimal("0.000000002")
        assert decision.percentage_improvement == Decimal("0.00")
        assert decision.should_alert
        serialized = decision.model_dump_json()
    assert evaluate_alert(**data).model_dump_json() == serialized


def test_signals_serialization_and_derived_decision_cannot_be_supplied(
    watch: FareWatch,
) -> None:
    decision = evaluate_alert(**inputs(watch))
    payload = json.loads(decision.model_dump_json())
    assert payload["should_alert"] is True
    assert payload["absolute_improvement"] == "200"
    assert payload["percentage_improvement"] == "20.00"
    assert payload["selected_candidate_id"] == "selected"
    assert payload["run_id"] == 10
    assert len(payload["signals"]) == 7
    component = signal(decision, "minimum_absolute_improvement")
    assert component.actual_value == Decimal("200")
    assert component.reference_value == Decimal("100")
    assert AlertSignal.model_validate_json(component.model_dump_json()) == component
    assert (
        AlertDecision.model_validate_json(decision.model_dump_json(round_trip=True))
        == decision
    )
    state = decision.model_dump(round_trip=True)
    with pytest.raises(ValidationError, match="Extra inputs"):
        AlertDecision.model_validate(state | {"should_alert": False})
    with pytest.raises(ValidationError, match="Extra inputs"):
        AlertDecision.model_validate(state | {"signals": ()})
    with pytest.raises(ValidationError, match="frozen"):
        decision.current_price = Decimal("1")


@pytest.mark.parametrize(
    "field",
    ["minimum_absolute_improvement", "minimum_percentage_improvement", "target_price"],
)
@pytest.mark.parametrize("value", [1.5, True, "NaN", "Infinity", "-1"])
def test_policy_rejects_invalid_numeric_inputs(field: str, value: object) -> None:
    with pytest.raises(ValidationError):
        policy(**{field: value})


@pytest.mark.parametrize(
    "updates",
    [
        {"minimum_absolute_improvement": "0"},
        {"minimum_percentage_improvement": "0"},
        {"minimum_percentage_improvement": "100.01"},
        {"historical_low_mode": "sometimes"},
        {"first_observation": "maybe"},
        {"currency": "usd"},
    ],
)
def test_policy_rejects_invalid_thresholds_and_modes(updates: dict[str, str]) -> None:
    with pytest.raises(ValidationError):
        policy(**updates)


def test_zero_target_and_explicit_first_observation() -> None:
    assert policy(target_price="0").target_price == Decimal(0)
    with pytest.raises(ValidationError, match="first_observation"):
        AlertPolicy(currency="USD")


def test_multiple_simultaneous_positive_signals(watch: FareWatch) -> None:
    data = inputs(watch)
    data["policy"] = policy(
        minimum_absolute_improvement="200",
        minimum_percentage_improvement="20",
        target_price="800",
        historical_low_mode="sufficient",
    )
    decision = evaluate_alert(**data)
    assert decision.should_alert
    for rule in ("improvement_thresholds", "target_price", "historical_low"):
        assert signal(decision, rule).satisfied


def test_multiple_simultaneous_negative_signals(watch: FareWatch) -> None:
    data = inputs(watch, current_price="1100")
    data["policy"] = policy(
        minimum_absolute_improvement="200",
        minimum_percentage_improvement="20",
        target_price="800",
        historical_low_mode="required",
    )
    decision = evaluate_alert(**data)
    assert not decision.should_alert
    for rule in ("improvement_thresholds", "target_price", "historical_low"):
        assert not signal(decision, rule).satisfied


def test_duplicate_prior_history_is_rejected(watch: FareWatch) -> None:
    data = inputs(watch)
    data["history"] *= 2
    with pytest.raises(ValueError, match="canonical observation"):
        evaluate_alert(**data)


def test_inconsistent_run_timestamps_rejected(watch: FareWatch) -> None:
    data = inputs(watch)
    data["history"].append(
        data["history"][0].model_copy(
            update={"observed_at": START + timedelta(hours=1)}
        )
    )
    with pytest.raises(ValueError, match="share a timestamp"):
        evaluate_alert(**data)


def test_new_itinerary_reference_is_watch_low_not_latest_row_or_average(
    watch: FareWatch,
) -> None:
    data = inputs(
        watch, current_price="925", prior_prices=("900", "1000"), new_itinerary=True
    )
    data["policy"] = policy(minimum_absolute_improvement="50")
    decision = evaluate_alert(**data)
    assert decision.previous_same_itinerary_price is None
    assert decision.prior_watch_average == Decimal("950")
    assert decision.improvement_reference_price == Decimal("900")
    assert decision.absolute_improvement == Decimal("-25")
    assert not decision.should_alert


@pytest.mark.parametrize(
    "updates", [{"target_price": "800"}, {"historical_low_mode": "sufficient"}]
)
def test_independent_triggers_can_pass_when_improvement_thresholds_fail(
    watch: FareWatch, updates: dict[str, str]
) -> None:
    data = inputs(watch)
    data["policy"] = policy(
        minimum_absolute_improvement="300",
        minimum_percentage_improvement="30",
        **updates,
    )
    decision = evaluate_alert(**data)
    assert not signal(decision, "improvement_thresholds").satisfied
    assert decision.should_alert


def test_required_low_blocks_target_when_price_only_ties_low(watch: FareWatch) -> None:
    data = inputs(watch, current_price="900", prior_prices=("900", "1000"))
    data["policy"] = policy(target_price="900", historical_low_mode="required")
    decision = evaluate_alert(**data)
    assert signal(decision, "target_price").satisfied
    assert not signal(decision, "historical_low").satisfied
    assert not decision.should_alert


def test_invalid_policy_copy_revalidated_at_evaluation_boundary(
    watch: FareWatch,
) -> None:
    data = inputs(watch)
    data["policy"] = data["policy"].model_copy(update={"target_price": 1.5})
    with (
        pytest.warns(UserWarning, match="serializer warnings"),
        pytest.raises(ValidationError, match="float"),
    ):
        evaluate_alert(**data)


def test_observation_identity_revalidated(watch: FareWatch) -> None:
    data = inputs(watch)
    data["current_observation"] = data["current_observation"].model_copy(
        update={"itinerary_id": itinerary_identity(watch, fare(alternative=True))}
    )
    with pytest.raises(ValueError, match="identity"):
        evaluate_alert(**data)
