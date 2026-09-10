"""Deterministic opportunity decisions from normalized data; no SDK or I/O."""

from collections.abc import Sequence
from decimal import ROUND_HALF_UP, Context, Decimal, localcontext
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    StrictBool,
    StrictInt,
    computed_field,
    model_validator,
)

from faresentry.constraints import evaluate_hard_constraints
from faresentry.history import (
    FareObservation,
    FareWatch,
    MonitoringRun,
    calculate_price_statistics,
    itinerary_identity,
)
from faresentry.models import (
    CurrencyCode,
    FlightLabel,
    HardTravelConstraints,
    Recommendation,
    RoundTripItinerary,
)


def _decimal_input(value: object) -> object:
    if isinstance(value, (float, bool)):
        raise ValueError("Use Decimal, a decimal string, or an integer, not float/bool")
    return value


FiniteDecimal = Annotated[
    Decimal, BeforeValidator(_decimal_input), Field(allow_inf_nan=False)
]
Money = Annotated[FiniteDecimal, Field(ge=0)]


class _AlertModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class AlertPolicy(_AlertModel):
    """All enabled improvement thresholds must pass to trigger an improvement.

    Target is both a prerequisite ceiling and an independent trigger. Required
    low is a prerequisite; sufficient low is an independent trigger. With no
    prior watch observations, first_observation governs permission to alert.
    An empty set of improvement thresholds never supplies a trigger.
    """

    currency: CurrencyCode
    minimum_absolute_improvement: Annotated[FiniteDecimal, Field(gt=0)] | None = None
    minimum_percentage_improvement: (
        Annotated[FiniteDecimal, Field(gt=0, le=100)] | None
    ) = None
    target_price: Money | None = None
    historical_low_mode: Literal["ignore", "sufficient", "required"] = "ignore"
    first_observation: Literal["suppress", "alert"]


SignalType = Literal[
    "first_observation_allowed",
    "minimum_absolute_improvement",
    "minimum_percentage_improvement",
    "improvement_thresholds",
    "target_price",
    "historical_low",
    "first_observation",
]
SignalRole = Literal[
    "prerequisite", "trigger", "prerequisite_and_trigger", "component", "information"
]
SignalValue = FiniteDecimal | StrictInt | StrictBool | None


class AlertSignal(_AlertModel):
    rule: SignalType
    role: SignalRole
    satisfied: StrictBool
    actual_value: SignalValue
    reference_value: SignalValue
    explanation: FlightLabel


def _improvement(
    current: Decimal, reference: Decimal | None
) -> tuple[Decimal | None, Decimal | None]:
    """Positive means cheaper. Percent rounds to .01, half up; zero is undefined.

    Use a fresh Decimal context. Precision preserves the subtraction/multiply
    and adds reference coefficient digits plus guard digits for division before
    final quantization. No rounding to cents is applied to money.
    """
    if reference is None:
        return None, None
    prices = (current, reference)
    precision = max(
        28,
        max(price.adjusted() for price in prices)
        - min(int(price.as_tuple().exponent) for price in prices)
        + len(reference.as_tuple().digits)
        + 10,
    )
    with localcontext(Context(prec=precision, rounding=ROUND_HALF_UP)):
        difference = reference - current
        percentage = (
            (difference * Decimal(100) / reference).quantize(Decimal("0.01"))
            if reference != 0
            else None
        )
    return difference, percentage


class AlertDecision(_AlertModel):
    """Immutable facts plus derived arithmetic, signals, and final decision.

    Computed output fields cannot be supplied by callers. Use
    model_dump_json(round_trip=True) to serialize reloadable source state;
    ordinary serialization includes the complete derived decision and signals.
    Historical aggregates describe observed options, not past recommendations
    or proof that those past options passed today's hard constraints.
    """

    watch_id: str = Field(pattern=r"^watch-v1-[0-9a-f]{64}$")
    run_id: int = Field(gt=0, strict=True)
    selected_candidate_id: FlightLabel
    itinerary_id: str = Field(pattern=r"^itinerary-v1-[0-9a-f]{64}$")
    current_price: Money
    currency: CurrencyCode
    policy: AlertPolicy
    prior_observation_count: int = Field(ge=0, strict=True)
    prior_same_itinerary_count: int = Field(ge=0, strict=True)
    previous_same_itinerary_price: Money | None
    prior_watch_low: Money | None
    prior_watch_average: Money | None

    @model_validator(mode="after")
    def validate_references(self) -> Self:
        if self.currency != self.policy.currency:
            raise ValueError("Policy and decision currencies must match")
        if self.prior_same_itinerary_count > self.prior_observation_count:
            raise ValueError("Same-itinerary count cannot exceed watch count")
        if (self.prior_same_itinerary_count > 0) != (
            self.previous_same_itinerary_price is not None
        ):
            raise ValueError("Previous same-itinerary price must agree with its count")
        for value in (self.prior_watch_low, self.prior_watch_average):
            if (self.prior_observation_count > 0) != (value is not None):
                raise ValueError("Watch reference prices must agree with history count")
        if self.prior_watch_low is not None:
            for value in (self.prior_watch_average, self.previous_same_itinerary_price):
                if value is not None and value < self.prior_watch_low:
                    raise ValueError("Watch low cannot exceed other reference prices")
        return self

    @computed_field
    @property
    def improvement_reference_type(
        self,
    ) -> Literal["same_itinerary_previous", "prior_watch_low", "unavailable"]:
        if self.previous_same_itinerary_price is not None:
            return "same_itinerary_previous"
        return "prior_watch_low" if self.prior_watch_low is not None else "unavailable"

    @computed_field
    @property
    def improvement_reference_price(self) -> Decimal | None:
        if self.previous_same_itinerary_price is not None:
            return self.previous_same_itinerary_price
        return self.prior_watch_low

    @computed_field
    @property
    def absolute_improvement(self) -> Decimal | None:
        return _improvement(self.current_price, self.improvement_reference_price)[0]

    @computed_field
    @property
    def percentage_improvement(self) -> Decimal | None:
        return _improvement(self.current_price, self.improvement_reference_price)[1]

    @computed_field
    @property
    def is_new_historical_low(self) -> bool:
        return (
            self.prior_watch_low is not None
            and self.current_price < self.prior_watch_low
        )

    @computed_field
    @property
    def signals(self) -> tuple[AlertSignal, ...]:
        policy = self.policy
        first = self.prior_observation_count == 0
        first_allowed = not first or policy.first_observation == "alert"
        signals = [
            AlertSignal(
                rule="first_observation_allowed",
                role="prerequisite",
                satisfied=first_allowed,
                actual_value=self.prior_observation_count,
                reference_value=0,
                explanation=(
                    f"Prior watch observations: {self.prior_observation_count}; "
                    f"first-observation policy is {policy.first_observation}."
                ),
            )
        ]
        components: list[bool] = []
        absolute, percent = _improvement(
            self.current_price, self.improvement_reference_price
        )
        for rule, actual, threshold in (
            (
                "minimum_absolute_improvement",
                absolute,
                policy.minimum_absolute_improvement,
            ),
            (
                "minimum_percentage_improvement",
                percent,
                policy.minimum_percentage_improvement,
            ),
        ):
            satisfied = (
                threshold is not None and actual is not None and actual >= threshold
            )
            if threshold is not None:
                components.append(satisfied)
            signals.append(
                AlertSignal(
                    rule=rule,
                    role="component",
                    satisfied=satisfied,
                    actual_value=actual,
                    reference_value=threshold,
                    explanation=(
                        f"{rule} is disabled."
                        if threshold is None
                        else f"Improvement from {self.improvement_reference_type}: "
                        f"{actual if actual is not None else 'unavailable'}; "
                        f"minimum {threshold}; {'met' if satisfied else 'not met'}."
                    ),
                )
            )
        signals.append(
            AlertSignal(
                rule="improvement_thresholds",
                role="trigger",
                satisfied=bool(components) and all(components),
                actual_value=sum(components),
                reference_value=len(components),
                explanation=(
                    f"{sum(components)} of {len(components)} configured improvement "
                    "thresholds met; at least one must be enabled and all must pass."
                ),
            )
        )
        target = policy.target_price
        signals.append(
            AlertSignal(
                rule="target_price",
                role="information" if target is None else "prerequisite_and_trigger",
                satisfied=target is not None and self.current_price <= target,
                actual_value=self.current_price,
                reference_value=target,
                explanation=(
                    "No target price configured."
                    if target is None
                    else f"Price {self.current_price} must be at most {target}; "
                    f"{'met' if self.current_price <= target else 'not met'}."
                ),
            )
        )
        low_role: SignalRole = {
            "ignore": "information",
            "sufficient": "trigger",
            "required": "prerequisite",
        }[policy.historical_low_mode]
        signals.append(
            AlertSignal(
                rule="historical_low",
                role=low_role,
                satisfied=self.is_new_historical_low,
                actual_value=self.current_price,
                reference_value=self.prior_watch_low,
                explanation=(
                    f"Historical-low mode: {policy.historical_low_mode}; "
                    + (
                        "no prior watch low is available."
                        if self.prior_watch_low is None
                        else f"current {self.current_price} < prior watch low "
                        f"{self.prior_watch_low}: {self.is_new_historical_low}."
                    )
                ),
            )
        )
        signals.append(
            AlertSignal(
                rule="first_observation",
                role="trigger",
                satisfied=first and policy.first_observation == "alert",
                actual_value=first,
                reference_value=policy.first_observation == "alert",
                explanation=(
                    f"First watch observation: {first}; "
                    f"policy is {policy.first_observation}."
                ),
            )
        )
        return tuple(signals)

    @computed_field
    @property
    def should_alert(self) -> bool:
        signals = self.signals
        return all(
            signal.satisfied
            for signal in signals
            if signal.role in ("prerequisite", "prerequisite_and_trigger")
        ) and any(
            signal.satisfied
            for signal in signals
            if signal.role in ("trigger", "prerequisite_and_trigger")
        )


def _validate_observation(watch: FareWatch, observation: FareObservation) -> None:
    if observation.currency != watch.currency:
        raise ValueError("Observation and watch currencies must match")
    if observation.watch_id != watch.watch_id:
        raise ValueError("Observation must belong to the watch")
    if (
        observation.outbound.origin != watch.origin
        or observation.outbound.destination != watch.destination
        or observation.itinerary_id != itinerary_identity(watch, observation)
    ):
        raise ValueError(
            "Observation itinerary identity and route must match the watch"
        )


def evaluate_alert(
    *,
    watch: FareWatch,
    current_run: MonitoringRun,
    candidate_id: str,
    itinerary: RoundTripItinerary,
    current_observation: FareObservation,
    history: Sequence[FareObservation],
    recommendation: Recommendation,
    constraints: HardTravelConstraints,
    policy: AlertPolicy,
) -> AlertDecision:
    """Evaluate a recommended, already-approved candidate without I/O or mutation.

    Supply complete whole-watch history from the repository (prefer its
    get_prior_observations API without an itinerary filter or a record limit).
    Run-bound observation timestamps are run timestamps by the persistence
    contract. We defensively filter by (that timestamp, run_id), never row ID,
    and always exclude the current run and null membership. The caller supplies
    persisted data from one repository; this pure function cannot authenticate
    database provenance or detect missing history from a truncated input.

    Recheck current hard constraints using the existing evaluator, as the
    recommendation boundary does. Prior options are raw observed fares; they
    are not assumed to have passed current constraints or been recommended.
    """
    watch = FareWatch.model_validate(watch.model_dump())
    current_run = MonitoringRun.model_validate(current_run.model_dump())
    itinerary = RoundTripItinerary.model_validate(itinerary.model_dump())
    current = FareObservation.model_validate(current_observation.model_dump())
    policy = AlertPolicy.model_validate(policy.model_dump())
    constraints = HardTravelConstraints.model_validate(constraints.model_dump())
    recommendation = Recommendation.model_validate(recommendation.model_dump())
    if candidate_id != recommendation.selected_candidate_id:
        raise ValueError("Candidate must match recommendation selected_candidate_id")
    if policy.currency != watch.currency:
        raise ValueError("Policy and watch currencies must match")
    if current_run.watch_id != watch.watch_id:
        raise ValueError("Current run must belong to the watch")
    _validate_observation(watch, current)
    if (
        current.run_id != current_run.run_id
        or current.observed_at != current_run.observed_at
    ):
        raise ValueError(
            "Current observation must belong to the current run and timestamp"
        )
    if (
        itinerary.outbound != current.outbound
        or itinerary.inbound != current.inbound
        or itinerary.total_price != current.total_price
        or itinerary.currency != current.currency
    ):
        raise ValueError("Candidate itinerary and fare must match current observation")
    if not evaluate_hard_constraints(itinerary, constraints).passes:
        raise ValueError("Current candidate must pass the active hard constraints")

    prior: list[FareObservation] = []
    seen: set[tuple[int, str]] = set()
    run_times = {current_run.run_id: current_run.observed_at}
    for item in history:
        item = FareObservation.model_validate(item.model_dump())
        _validate_observation(watch, item)
        if item.run_id is None:
            continue
        if run_times.setdefault(item.run_id, item.observed_at) != item.observed_at:
            raise ValueError("Observations in the same run must share a timestamp")
        if (item.observed_at, item.run_id) >= (
            current_run.observed_at,
            current_run.run_id,
        ):
            continue
        key = (item.run_id, item.itinerary_id)
        if key in seen:
            raise ValueError(
                "History must contain one canonical observation per run/itinerary"
            )
        seen.add(key)
        prior.append(item)
    prior.sort(key=lambda item: (item.observed_at, item.run_id, item.observation_id))
    same = [item for item in prior if item.itinerary_id == current.itinerary_id]
    stats = calculate_price_statistics(prior, currency=watch.currency)
    return AlertDecision(
        watch_id=watch.watch_id,
        run_id=current_run.run_id,
        selected_candidate_id=candidate_id,
        itinerary_id=current.itinerary_id,
        current_price=current.total_price,
        currency=current.currency,
        policy=policy,
        prior_observation_count=len(prior),
        prior_same_itinerary_count=len(same),
        previous_same_itinerary_price=same[-1].total_price if same else None,
        prior_watch_low=stats.minimum_price,
        prior_watch_average=stats.average_price,
    )
