"""Fixed-interval scheduling for one sequential local scheduler instance."""

import math
import time
from collections.abc import Callable, Iterator, Sequence
from datetime import UTC, datetime, timedelta
from typing import Literal, Protocol, Self

from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    model_validator,
)

from faresentry.alerts import AlertPolicy
from faresentry.history import FareWatch, MonitoringRun
from faresentry.models import HardTravelConstraints, TravelerSoftPreferences, TripQuery
from faresentry.monitoring import (
    FareHistoryRepository,
    MonitoringRunResult,
    Recommender,
    run_monitoring_cycle,
)
from faresentry.providers.base import FlightProvider


class ScheduledWatch(BaseModel):
    """Injected watch configuration; no account/configuration database required.

    The immutable FareWatch contains the TripQuery fields. Configurations with
    the same watch identity share attempt history, even if preferences differ.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    watch: FareWatch
    constraints: HardTravelConstraints
    preferences: TravelerSoftPreferences
    policy: AlertPolicy
    enabled: bool = Field(default=True, strict=True)
    check_interval: timedelta = Field(gt=timedelta(0))
    max_return_lookups: int = Field(default=3, gt=0, strict=True)

    @model_validator(mode="after")
    def validate_currency(self) -> Self:
        if self.policy.currency != self.watch.currency:
            raise ValueError("Policy and watch currencies must match")
        return self


class SchedulingHistory(FareHistoryRepository, Protocol):
    def get_latest_run(self, watch: FareWatch) -> MonitoringRun | None: ...


class MonitoringCycle(Protocol):
    """The existing monitoring entry point, replaceable with a scripted test call."""

    def __call__(
        self,
        query: TripQuery,
        *,
        constraints: HardTravelConstraints,
        preferences: TravelerSoftPreferences,
        policy: AlertPolicy,
        provider: FlightProvider,
        history: FareHistoryRepository,
        recommender: Recommender,
        max_return_lookups: int,
        clock: Callable[[], datetime],
    ) -> MonitoringRunResult: ...


class SchedulingFailure(BaseModel):
    """Observable failure category without raw exception strings or credentials."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    stage: Literal["history", "monitoring"]
    error_type: str


class ScheduledWatchOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    watch_id: str
    checked_at: AwareDatetime
    status: Literal["disabled", "not_due", "succeeded", "failed"]
    result: MonitoringRunResult | None = None
    failure: SchedulingFailure | None = None


class SchedulerPassResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    outcomes: tuple[ScheduledWatchOutcome, ...]

    @computed_field
    @property
    def watches_examined(self) -> int:
        return len(self.outcomes)

    @computed_field
    @property
    def watches_skipped(self) -> int:
        return sum(item.status in ("disabled", "not_due") for item in self.outcomes)

    @computed_field
    @property
    def watches_succeeded(self) -> int:
        return sum(item.status == "succeeded" for item in self.outcomes)

    @computed_field
    @property
    def watches_failed(self) -> int:
        return sum(item.status == "failed" for item in self.outcomes)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Scheduling timestamps must be timezone-aware")
    return value.astimezone(UTC)


def is_watch_due(
    configuration: ScheduledWatch,
    *,
    now: datetime,
    last_attempt_at: datetime | None,
) -> bool:
    """Enabled and never attempted, or elapsed time >= interval; no I/O.

    Compare elapsed UTC time, including across timezone/DST boundaries.
    A future last-attempt timestamp delays the next attempt, never makes it due.
    """
    now = _as_utc(now)
    previous = _as_utc(last_attempt_at) if last_attempt_at is not None else None
    return configuration.enabled and (
        previous is None or now - previous >= configuration.check_interval
    )


class MonitoringScheduler:
    """One serial scheduler with a fixed injected configuration snapshot.

    Persisted run creation times, including manual/incomplete runs, control
    cadence across restarts. A process-local attempt guard also covers failures
    before a run can be persisted (e.g. database unavailable); restarting clears
    that fallback. One interval permits one attempt, not a catch-up burst.

    Use one instance/loop per database. Multiple concurrent schedulers are outside
    this single-process MVP's execution contract.
    """

    def __init__(
        self,
        watches: Sequence[ScheduledWatch],
        *,
        history: SchedulingHistory,
        provider: FlightProvider,
        recommender: Recommender,
        clock: Callable[[], datetime] = _utc_now,
        monitoring_cycle: MonitoringCycle = run_monitoring_cycle,
    ) -> None:
        self._watches = tuple(watches)
        ids = [item.watch.watch_id for item in self._watches]
        if len(ids) != len(set(ids)):
            raise ValueError(
                "Each watch identity must occur only once in the scheduler"
            )
        self._history = history
        self._provider = provider
        self._recommender = recommender
        self._clock = clock
        self._monitoring_cycle = monitoring_cycle
        self._last_attempts: dict[str, datetime] = {}

    def run_due_watches(self) -> SchedulerPassResult:
        """Run each due watch once in configuration order, isolating exceptions.

        Disabled/not-due watches never invoke the monitoring cycle. History-read
        failures are reported distinctly and do not risk running without a known
        cadence reference. Exceptions are represented by stage and type; keyboard
        interrupts and other BaseException control signals propagate.
        """
        outcomes: list[ScheduledWatchOutcome] = []
        for configuration in self._watches:
            now = _as_utc(self._clock())
            watch = configuration.watch
            watch_id = watch.watch_id
            if not configuration.enabled:
                outcomes.append(
                    ScheduledWatchOutcome(
                        watch_id=watch_id, checked_at=now, status="disabled"
                    )
                )
                continue
            if not is_watch_due(
                configuration,
                now=now,
                last_attempt_at=self._last_attempts.get(watch_id),
            ):
                outcomes.append(
                    ScheduledWatchOutcome(
                        watch_id=watch_id, checked_at=now, status="not_due"
                    )
                )
                continue
            stage: Literal["history", "monitoring"] = "history"
            try:
                latest = self._history.get_latest_run(watch)
                if not is_watch_due(
                    configuration,
                    now=now,
                    last_attempt_at=latest.observed_at if latest is not None else None,
                ):
                    outcomes.append(
                        ScheduledWatchOutcome(
                            watch_id=watch_id, checked_at=now, status="not_due"
                        )
                    )
                    continue
                stage = "monitoring"
                self._last_attempts[watch_id] = now
                result = self._monitoring_cycle(
                    TripQuery.model_validate(watch.model_dump()),
                    constraints=configuration.constraints,
                    preferences=configuration.preferences,
                    policy=configuration.policy,
                    provider=self._provider,
                    history=self._history,
                    recommender=self._recommender,
                    max_return_lookups=configuration.max_return_lookups,
                    clock=lambda attempt_time=now: attempt_time,
                )
                outcomes.append(
                    ScheduledWatchOutcome(
                        watch_id=watch_id,
                        checked_at=now,
                        status="succeeded",
                        result=result,
                    )
                )
            except Exception as error:
                self._last_attempts[watch_id] = now
                outcomes.append(
                    ScheduledWatchOutcome(
                        watch_id=watch_id,
                        checked_at=now,
                        status="failed",
                        failure=SchedulingFailure(
                            stage=stage, error_type=type(error).__name__
                        ),
                    )
                )
        return SchedulerPassResult(outcomes=tuple(outcomes))

    def poll(
        self,
        *,
        poll_interval_seconds: float = 60,
        sleep: Callable[[float], None] = time.sleep,
        max_polls: int | None = None,
    ) -> Iterator[SchedulerPassResult]:
        """Yield each pass immediately, sleeping between passes; Ctrl+C stops.

        Poll delay is separate from watch cadence and starts after consuming the
        previous result. No pass overlaps another. max_polls bounds demo/tests;
        with None, continue until the caller stops iteration or interrupts.
        """
        if (
            isinstance(poll_interval_seconds, bool)
            or not math.isfinite(poll_interval_seconds)
            or poll_interval_seconds <= 0
        ):
            raise ValueError("poll_interval_seconds must be positive and finite")
        if max_polls is not None and (type(max_polls) is not int or max_polls < 0):
            raise ValueError("max_polls must be a nonnegative integer or None")
        polls = 0
        while max_polls is None or polls < max_polls:
            yield self.run_due_watches()
            polls += 1
            if max_polls is None or polls < max_polls:
                sleep(poll_interval_seconds)
