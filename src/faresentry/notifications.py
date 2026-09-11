"""Deliver existing alert decisions, without making fare or alert judgments."""

from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Literal, Protocol

from faresentry.history import FareWatch, itinerary_identity
from faresentry.monitoring import MonitoringRunResult
from faresentry.notification_models import (
    DeliveryReceipt,
    NotificationDelivery,
    NotificationMessage,
    NotificationOutcome,
)
from faresentry.scheduling import SchedulerPassResult


class NotificationProviderError(RuntimeError):
    """Notification transport failed; the message is safe to display."""


class NotificationProvider(Protocol):
    def send(self, message: NotificationMessage) -> DeliveryReceipt: ...


class NotificationHistory(Protocol):
    def get_notification_delivery(
        self, watch: FareWatch, *, run_id: int
    ) -> NotificationDelivery | None:
        """Validate a completed run belonging to watch, then return its receipt."""
        ...

    def record_notification_delivery(
        self, watch: FareWatch, delivery: NotificationDelivery
    ) -> NotificationDelivery: ...


def build_notification_message(
    watch: FareWatch, result: MonitoringRunResult
) -> NotificationMessage:
    """Format accepted domain facts and display-only recommendation text.

    Identity checks prevent accidentally mixing one result with another watch.
    No alert thresholds are evaluated here; the service gates on should_alert.
    """
    decision = result.alert_decision
    itinerary = result.selected_itinerary
    recommendation = result.recommendation
    if (
        result.status != "completed"
        or decision is None
        or itinerary is None
        or recommendation is None
        or result.selected_candidate_id is None
    ):
        raise ValueError("A complete monitoring recommendation and alert are required")
    if (
        watch.watch_id != result.watch_id
        or decision.watch_id != result.watch_id
        or decision.run_id != result.run_id
        or decision.selected_candidate_id != result.selected_candidate_id
        or recommendation.selected_candidate_id != result.selected_candidate_id
        or decision.itinerary_id != itinerary_identity(watch, itinerary)
        or decision.current_price != itinerary.total_price
        or decision.currency != itinerary.currency
        or decision.currency != watch.currency
        or itinerary.outbound.origin != watch.origin
        or itinerary.outbound.destination != watch.destination
    ):
        raise ValueError("Notification watch, selection, fare, and decision must agree")
    currency = decision.currency
    lines = [
        f"Trip: {watch.origin} -> {watch.destination}",
        f"Outbound: {watch.outbound_date.isoformat()}; "
        f"return: {watch.return_date.isoformat()}",
        f"Selected round-trip fare: {currency} {decision.current_price:f}",
    ]
    for label, price in (
        ("Previous fare for this itinerary", decision.previous_same_itinerary_price),
        ("Prior eligible watch low", decision.prior_watch_low),
        ("Prior eligible watch average", decision.prior_watch_average),
    ):
        if price is not None:
            lines.append(f"{label}: {currency} {price:f}")
    if decision.absolute_improvement is not None:
        reference = (
            "previous fare for this itinerary"
            if decision.improvement_reference_type == "same_itinerary_previous"
            else "prior eligible watch low"
        )
        lines.append(
            f"Price improvement versus {reference}: "
            f"{currency} {decision.absolute_improvement:f}"
        )
        if decision.percentage_improvement is not None:
            lines.append(
                f"Percentage improvement: {decision.percentage_improvement:f}%"
            )
    for label, direction in (
        ("Outbound", itinerary.outbound),
        ("Return", itinerary.inbound),
    ):
        lines.append(
            f"{label}: {direction.stops} stops; {direction.duration_minutes} minutes; "
            f"flights {', '.join(direction.flight_numbers)}"
        )
    lines.extend(["", "Why this alert was approved:"])
    lines.extend(
        f"- {signal.explanation}"
        for signal in decision.signals
        if signal.satisfied
        and signal.role in ("trigger", "prerequisite_and_trigger", "component")
    )
    # Tradeoff references are validated against the current candidates upstream.
    # Only optional display content is suppressed; the recommendation stays intact.
    candidate_ids = {result.selected_candidate_id} | {
        candidate_id
        for tradeoff in recommendation.key_tradeoffs
        for candidate_id in tradeoff.candidate_ids
    }
    if not any(
        candidate_id in recommendation.recommendation for candidate_id in candidate_ids
    ):
        lines.extend(["", "Recommendation:", recommendation.recommendation])
    return NotificationMessage(
        subject=(
            "FareSentry: worthwhile fare found for "
            f"{watch.origin} -> {watch.destination}"
        ),
        body="\n".join(lines),
    )


def _utc_now() -> datetime:
    return datetime.now(UTC)


class NotificationService:
    """Sequential delivery with durable successful-send deduplication per run.

    No automatic retries. A provider timeout or local failure after sending can
    leave delivery uncertain; an explicit retry may duplicate the email. Use one
    sequential caller, as with the scheduler; no transaction spans email sending.
    """

    def __init__(
        self,
        *,
        history: NotificationHistory,
        provider: NotificationProvider,
        clock: Callable[[], datetime] = _utc_now,
    ) -> None:
        self._history = history
        self._provider = provider
        self._clock = clock

    def handle_monitoring_result(
        self, watch: FareWatch, result: MonitoringRunResult
    ) -> NotificationOutcome:
        stage: Literal["validation", "history", "provider", "recording"] = "validation"
        try:
            if result.alert_decision is None:
                return NotificationOutcome(
                    watch_id=result.watch_id,
                    run_id=result.run_id,
                    status="not_applicable",
                )
            if not result.alert_decision.should_alert:
                return NotificationOutcome(
                    watch_id=result.watch_id, run_id=result.run_id, status="not_needed"
                )
            message = build_notification_message(watch, result)
            stage = "history"
            existing = self._history.get_notification_delivery(
                watch, run_id=result.run_id
            )
            if existing is not None:
                return NotificationOutcome(
                    watch_id=result.watch_id,
                    run_id=result.run_id,
                    status="already_delivered",
                    delivery=existing,
                )
            stage = "provider"
            receipt = self._provider.send(message)
            stage = "recording"
            delivery = NotificationDelivery(
                **receipt.model_dump(), run_id=result.run_id, delivered_at=self._clock()
            )
            stored = self._history.record_notification_delivery(watch, delivery)
            return NotificationOutcome(
                watch_id=result.watch_id,
                run_id=result.run_id,
                status="delivered",
                delivery=stored,
            )
        except Exception as error:
            return NotificationOutcome(
                watch_id=result.watch_id,
                run_id=result.run_id,
                status="failed",
                failure_stage=stage,
                error_type=type(error).__name__,
            )

    def handle_scheduler_result(
        self, batch: SchedulerPassResult, *, watches: Mapping[str, FareWatch]
    ) -> tuple[NotificationOutcome, ...]:
        """Consume a pass after all monitoring checks; retain the original batch.

        Each delivery has its own outcome. Monitoring failures/skips never send;
        notification failures do not hide successful MonitoringRunResult values.
        """
        outcomes: list[NotificationOutcome] = []
        for item in batch.outcomes:
            if item.status != "succeeded" or item.result is None:
                outcomes.append(
                    NotificationOutcome(watch_id=item.watch_id, status="not_applicable")
                )
            elif item.watch_id not in watches:
                outcomes.append(
                    NotificationOutcome(
                        watch_id=item.watch_id,
                        run_id=item.result.run_id,
                        status="failed",
                        failure_stage="validation",
                        error_type="MissingWatchConfiguration",
                    )
                )
            else:
                outcomes.append(
                    self.handle_monitoring_result(watches[item.watch_id], item.result)
                )
        return tuple(outcomes)
