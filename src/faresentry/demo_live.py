"""Optional live composition for the local UI; no external calls on import."""

import os
from datetime import UTC, datetime
from pathlib import Path

from faresentry.demo import CheckReport, current_run_candidate_ids
from faresentry.models import TripQuery
from faresentry.monitoring import Recommender, run_monitoring_cycle
from faresentry.notification_models import NotificationOutcome
from faresentry.notifications import NotificationService
from faresentry.persistence import SQLiteFareHistory
from faresentry.providers.base import FlightProvider
from faresentry.scheduling import ScheduledWatch, is_watch_due

LIVE_DATABASE = Path(__file__).resolve().parents[2] / ".faresentry-ui" / "live.sqlite3"


def live_mode_enabled() -> bool:
    """Local UI opt-in; public deployment leaves this unset."""
    return os.environ.get("FARESENTRY_ENABLE_LIVE") == "1"


def missing_live_configuration(*, email: bool = False) -> tuple[str, ...]:
    names = ("AWS_PROFILE", "AWS_REGION") + (
        ("FARESENTRY_EMAIL_FROM", "FARESENTRY_EMAIL_TO")
        if email
        else ("SERPAPI_API_KEY", "FARESENTRY_BEDROCK_MODEL_ID")
    )
    return tuple(name for name in names if not os.environ.get(name, "").strip())


def _live_dependencies() -> tuple[FlightProvider, Recommender]:
    # Even constructing a live SDK model is restricted to an explicit check.
    from strands.models import BedrockModel

    from faresentry.agents.recommendation import StrandsRecommender
    from faresentry.providers.serpapi import SerpApiFlightProvider

    return SerpApiFlightProvider(), StrandsRecommender(
        model=BedrockModel(
            model_id=os.environ["FARESENTRY_BEDROCK_MODEL_ID"],
            region_name=os.environ["AWS_REGION"],
            temperature=0.2,
            max_tokens=1500,
        )
    )


def run_live_check(
    configuration: ScheduledWatch, *, database_path: Path = LIVE_DATABASE
) -> CheckReport:
    """One explicit manual check, independent of cadence; never sends email."""
    now = datetime.now(UTC)
    if not live_mode_enabled():
        return CheckReport(
            configuration=configuration,
            checked_at=now,
            error="Live mode is disabled. This app supports deterministic demos only.",
        )
    if missing_live_configuration():
        return CheckReport(
            configuration=configuration,
            checked_at=now,
            error="Live mode is not configured. Complete the listed settings.",
        )
    try:
        database_path.parent.mkdir(parents=True, exist_ok=True)
        history = SQLiteFareHistory(database_path)
        provider, recommender = _live_dependencies()
        result = run_monitoring_cycle(
            TripQuery.model_validate(configuration.watch.model_dump()),
            constraints=configuration.constraints,
            preferences=configuration.preferences,
            policy=configuration.policy,
            provider=provider,
            history=history,
            recommender=recommender,
            max_return_lookups=configuration.max_return_lookups,
            clock=lambda: now,
        )
    except Exception:
        return CheckReport(
            configuration=configuration,
            checked_at=now,
            error=(
                "The live check could not complete. Check your AWS login, service "
                "permissions, SerpApi configuration, and local database access. "
                "No email was attempted."
            ),
        )
    try:
        observations = tuple(history.get_recent_observations(configuration.watch))
    except Exception:
        observations = ()
        history_error = True
    else:
        history_error = False
    return CheckReport(
        configuration=configuration,
        checked_at=now,
        result=result,
        history=observations[-12:],
        current_candidate_ids=(
            current_run_candidate_ids(result, observations)
            if not history_error
            else None
        ),
        history_error=history_error,
        due_after_check=is_watch_due(configuration, now=now, last_attempt_at=now),
    )


def deliver_live_alert(
    report: CheckReport, *, database_path: Path = LIVE_DATABASE
) -> NotificationOutcome:
    """Explicit email for the retained result, using existing deduplication."""
    if report.simulated or report.result is None:
        return NotificationOutcome(
            watch_id=report.configuration.watch.watch_id, status="not_applicable"
        )
    result = report.result
    if result.alert_decision is None or not result.alert_decision.should_alert:
        return NotificationOutcome(
            watch_id=result.watch_id,
            run_id=result.run_id,
            status="not_applicable" if result.alert_decision is None else "not_needed",
        )
    if not live_mode_enabled() or missing_live_configuration(email=True):
        return NotificationOutcome(
            watch_id=result.watch_id,
            run_id=result.run_id,
            status="failed",
            failure_stage="validation",
            error_type=(
                "LiveModeDisabled"
                if not live_mode_enabled()
                else "MissingEmailConfiguration"
            ),
        )
    try:
        from faresentry.providers.ses import SESEmailProvider

        service = NotificationService(
            history=SQLiteFareHistory(database_path),
            provider=SESEmailProvider.from_environment(),
        )
        return service.handle_monitoring_result(report.configuration.watch, result)
    except Exception:
        return NotificationOutcome(
            watch_id=result.watch_id,
            run_id=result.run_id,
            status="failed",
            failure_stage="validation",
            error_type="EmailSetupError",
        )
