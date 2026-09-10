"""One synchronous fare-watch check; no scheduling or delivery infrastructure."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from faresentry.alerts import AlertDecision, AlertPolicy, evaluate_alert
from faresentry.constraints import (
    evaluate_direction_constraints,
    evaluate_hard_constraints,
)
from faresentry.history import (
    FareObservation,
    FareWatch,
    MonitoringRun,
    itinerary_identity,
)
from faresentry.models import (
    HardTravelConstraints,
    Recommendation,
    RoundTripItinerary,
    TravelerSoftPreferences,
    TripQuery,
)
from faresentry.providers.base import FlightProvider, FlightProviderError
from faresentry.recommendations import parse_recommendation


class FareHistoryRepository(Protocol):
    """The run-aware subset of the existing history repository contract."""

    def create_run(
        self, watch: FareWatch, *, observed_at: datetime
    ) -> MonitoringRun: ...

    def mark_run_completed(self, watch: FareWatch, *, run: MonitoringRun) -> None: ...

    def record_observation(
        self, watch: FareWatch, itinerary: RoundTripItinerary, *, run: MonitoringRun
    ) -> FareObservation: ...

    def get_prior_observations(
        self, watch: FareWatch, *, before_run: MonitoringRun
    ) -> list[FareObservation]: ...


class Recommender(Protocol):
    """Implemented by StrandsRecommender without importing the SDK here."""

    def recommend(
        self,
        acceptable_candidates: Mapping[str, RoundTripItinerary],
        preferences: TravelerSoftPreferences,
        *,
        constraints: HardTravelConstraints,
    ) -> Recommendation: ...


class ConflictingItineraryError(ValueError):
    """One normalized identity has inconsistent fares/details in this check."""


MonitoringStatus = Literal[
    "completed",
    "no_outbound_options",
    "no_usable_departure_tokens",
    "no_eligible_outbound_options",
    "no_completed_itineraries",
    "no_eligible_candidates",
]


class MonitoringRunResult(BaseModel):
    """Application result, without provider tokens or raw model responses.

    completed_round_trips and rejected_by_constraints count unique identities;
    duplicate_round_trips counts additional equivalent results. Outbound
    prescreen rejections are separate from completed-itinerary rejections.
    completed means the decision was evaluated, regardless of should_alert.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    watch_id: str
    run_id: int = Field(gt=0, strict=True)
    status: MonitoringStatus
    outbound_choices_found: int = Field(ge=0, strict=True)
    outbound_choices_rejected: int = Field(ge=0, strict=True)
    return_lookups_attempted: int = Field(ge=0, strict=True)
    completed_round_trips: int = Field(ge=0, strict=True)
    duplicate_round_trips: int = Field(ge=0, strict=True)
    rejected_by_constraints: int = Field(ge=0, strict=True)
    eligible_candidates: int = Field(ge=0, strict=True)
    selected_candidate_id: str | None = None
    selected_itinerary: RoundTripItinerary | None = None
    recommendation: Recommendation | None = None
    alert_decision: AlertDecision | None = None


def _utc_now() -> datetime:
    return datetime.now(UTC)


def run_monitoring_cycle(
    query: TripQuery,
    *,
    constraints: HardTravelConstraints,
    preferences: TravelerSoftPreferences,
    policy: AlertPolicy,
    provider: FlightProvider,
    history: FareHistoryRepository,
    recommender: Recommender,
    max_return_lookups: int = 3,
    clock: Callable[[], datetime] = _utc_now,
) -> MonitoringRunResult:
    """Check a watch once using bounded outbound selection in provider order.

    Only token-bearing outbounds passing per-direction constraints consume the
    lookup budget. Every compatible return from those lookups is considered.
    Equal normalized identities/fares collapse in first-seen order; conflicting
    fares raise, since identity excludes fare-product detail and schedule times.

    Persist eligible fares before recommendation, then use strictly prior-run
    history rechecked against active constraints (watch IDs exclude constraints).
    Failures propagate without retries or fallback decisions. Repository writes
    already committed, including the run and any observations, remain on failure.
    Only normally finished runs enter future alert history. Completion is saved
    after result preparation; this is not a transaction across external services.
    """
    if type(max_return_lookups) is not int or max_return_lookups <= 0:
        raise ValueError("max_return_lookups must be a positive integer")
    watch = FareWatch.from_query(query)
    if policy.currency != watch.currency:
        raise ValueError("Policy and watch currencies must match")
    run = history.create_run(watch, observed_at=clock())
    # A fresh query preserves the immutable watch context for every lookup.
    search_query = TripQuery.model_validate(watch.model_dump())
    outbounds = provider.search(search_query)
    counts = {
        "outbound_choices_found": len(outbounds),
        "outbound_choices_rejected": 0,
        "return_lookups_attempted": 0,
        "completed_round_trips": 0,
        "duplicate_round_trips": 0,
        "rejected_by_constraints": 0,
        "eligible_candidates": 0,
    }

    def finish(result: MonitoringRunResult) -> MonitoringRunResult:
        history.mark_run_completed(watch, run=run)
        return result

    def empty_result(status: MonitoringStatus) -> MonitoringRunResult:
        result = MonitoringRunResult(
            watch_id=watch.watch_id, run_id=run.run_id, status=status, **counts
        )
        return finish(result)

    if not outbounds:
        return empty_result("no_outbound_options")
    usable = [
        option
        for option in outbounds
        if option.departure_token and option.departure_token.strip()
    ]
    if not usable:
        return empty_result("no_usable_departure_tokens")
    promising = []
    for option in usable:
        if (
            option.origin != watch.origin
            or option.destination != watch.destination
            or option.currency != watch.currency
        ):
            raise FlightProviderError(
                "Outbound route and currency must match the watch"
            )
        if evaluate_direction_constraints(
            option.outbound, constraints, direction="outbound"
        ).passes:
            promising.append(option)
        else:
            counts["outbound_choices_rejected"] += 1
    if not promising:
        return empty_result("no_eligible_outbound_options")

    completed: dict[str, RoundTripItinerary] = {}
    eligible: dict[str, RoundTripItinerary] = {}
    for option in promising[:max_return_lookups]:
        counts["return_lookups_attempted"] += 1
        for itinerary in provider.get_return_options(option, search_query):
            if (
                itinerary.outbound != option.outbound
                or itinerary.currency != watch.currency
            ):
                raise FlightProviderError(
                    "Completed itinerary must match the requested outbound and currency"
                )
            evaluation = evaluate_hard_constraints(itinerary, constraints)
            candidate_id = itinerary_identity(watch, itinerary)
            if candidate_id in completed:
                if completed[candidate_id] != itinerary:
                    raise ConflictingItineraryError(
                        "Conflicting fares/details for one itinerary identity; "
                        "identity does not distinguish fare products or schedule times"
                    )
                counts["duplicate_round_trips"] += 1
                continue
            completed[candidate_id] = itinerary
            if evaluation.passes:
                eligible[candidate_id] = itinerary
            else:
                counts["rejected_by_constraints"] += 1
    counts["completed_round_trips"] = len(completed)
    counts["eligible_candidates"] = len(eligible)
    if not completed:
        return empty_result("no_completed_itineraries")
    if not eligible:
        return empty_result("no_eligible_candidates")

    observations = {
        candidate_id: history.record_observation(watch, itinerary, run=run)
        for candidate_id, itinerary in eligible.items()
    }
    recommendation = parse_recommendation(
        recommender.recommend(
            MappingProxyType(eligible), preferences, constraints=constraints
        ),
        candidate_ids=tuple(eligible),
    )
    selected_id = recommendation.selected_candidate_id
    prior_eligible = [
        observation
        for observation in history.get_prior_observations(watch, before_run=run)
        if evaluate_hard_constraints(observation, constraints).passes
    ]
    decision = evaluate_alert(
        watch=watch,
        current_run=run,
        candidate_id=selected_id,
        itinerary=eligible[selected_id],
        current_observation=observations[selected_id],
        history=prior_eligible,
        recommendation=recommendation,
        constraints=constraints,
        policy=policy,
    )
    result = MonitoringRunResult(
        watch_id=watch.watch_id,
        run_id=run.run_id,
        status="completed",
        **counts,
        selected_candidate_id=selected_id,
        selected_itinerary=eligible[selected_id],
        recommendation=recommendation,
        alert_decision=decision,
    )
    return finish(result)
