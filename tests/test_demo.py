from pathlib import Path
from unittest.mock import Mock

import pytest

from faresentry import demo, demo_live
from faresentry.models import Recommendation
from faresentry.notifications import NotificationService
from faresentry.providers.ses import SESEmailProvider


@pytest.mark.parametrize(
    ("scenario", "alert", "sends"),
    [
        ("routine", False, 0),
        ("opportunity", True, 1),
    ],
)
def test_scenarios_use_real_decisions_and_notification_gating(
    scenario: demo.DemoScenario,
    alert: bool,
    sends: int,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    forbidden = Mock(side_effect=AssertionError("Live dependencies used"))
    monkeypatch.setattr(demo_live, "_live_dependencies", forbidden)
    monkeypatch.setattr(SESEmailProvider, "send", forbidden)
    report = demo.run_demo(scenario)
    assert report.error is None
    assert report.result.status == "completed"
    assert report.result.alert_decision.should_alert is alert
    assert report.notification.status == ("delivered" if alert else "not_needed")
    assert report.simulated_sends == sends
    assert report.result.outbound_choices_found == 3
    assert report.result.outbound_choices_rejected == 1
    assert report.result.return_lookups_attempted == 2
    assert report.result.eligible_candidates == 2
    assert report.result.alert_decision.previous_same_itinerary_price == 1000
    assert len(report.history) == 4
    assert report.due_after_check is False
    forbidden.assert_not_called()


def test_demo_replay_uses_existing_delivery_deduplication(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = NotificationService.handle_scheduler_result

    def replay(self: NotificationService, *args: object, **kwargs: object):
        first = original(self, *args, **kwargs)
        repeated = original(self, *args, **kwargs)
        assert first[0].status == "delivered"
        assert repeated[0].status == "already_delivered"
        return first

    monkeypatch.setattr(NotificationService, "handle_scheduler_result", replay)
    assert demo.run_demo("opportunity").simulated_sends == 1


def test_repeated_demo_is_deterministic_and_temp_storage_is_removed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live_file = tmp_path / "live.sqlite3"
    live_file.write_bytes(b"existing live data")
    monkeypatch.setattr(demo_live, "LIVE_DATABASE", live_file)
    original = demo.SQLiteFareHistory
    paths: list[Path] = []

    def repository(path: Path):
        paths.append(path)
        assert path != live_file
        return original(path)

    monkeypatch.setattr(demo, "SQLiteFareHistory", repository)
    first = demo.run_demo("opportunity")
    second = demo.run_demo("opportunity")
    assert first.model_dump_json() == second.model_dump_json()
    assert len(set(paths)) == 2
    assert all(not path.parent.exists() for path in paths)
    assert live_file.read_bytes() == b"existing live data"


@pytest.mark.parametrize("field", ["selected", "alternative", "watch"])
def test_safe_display_omits_identifiers_without_mutating_recommendation(
    field: str,
) -> None:
    report = demo.run_demo("opportunity")
    result = report.result
    ids = result.recommendation.key_tradeoffs[0].candidate_ids
    identifier = {"selected": ids[0], "alternative": ids[1], "watch": result.watch_id}[
        field
    ]
    recommendation = Recommendation.model_validate(
        result.recommendation.model_dump()
        | {"recommendation": f"Consider {identifier} for this trip."}
    )
    result = result.model_copy(update={"recommendation": recommendation})
    before = recommendation.model_dump_json()
    assert (
        demo.safe_recommendation_text(
            result,
            recommendation.recommendation,
            current_candidate_ids=report.current_candidate_ids,
        )
        is None
    )
    assert (
        demo.safe_recommendation_text(
            result,
            "A good itinerary-v1-style trip.",
            current_candidate_ids=report.current_candidate_ids,
        )
        == "A good itinerary-v1-style trip."
    )
    assert recommendation.model_dump_json() == before


def test_history_display_is_an_allowlist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "private-aws-test-value")
    report = demo.run_demo("opportunity")
    rows = demo.history_rows(report.history)
    output = str(rows)
    for prohibited in (
        "itinerary-v1-",
        "watch-v1-",
        "synthetic-token",
        "departure_token",
        "private-aws-test-value",
        "test-key-not-a-real-credential",
    ):
        assert prohibited not in output
    assert rows[-2]["Fare"] == "USD 800.00"


def test_missing_live_configuration_is_read_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("FARESENTRY_ENABLE_LIVE", "1")
    for name in (
        "SERPAPI_API_KEY",
        "AWS_PROFILE",
        "AWS_REGION",
        "FARESENTRY_BEDROCK_MODEL_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    factory = Mock()
    monkeypatch.setattr(demo_live, "_live_dependencies", factory)
    path = tmp_path / "absent" / "live.sqlite3"
    assert demo_live.missing_live_configuration() == (
        "AWS_PROFILE",
        "AWS_REGION",
        "SERPAPI_API_KEY",
        "FARESENTRY_BEDROCK_MODEL_ID",
    )
    report = demo_live.run_live_check(demo.demo_watch(), database_path=path)
    assert report.error is not None
    assert report.result is None
    assert not path.exists()
    factory.assert_not_called()


def configure_live(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FARESENTRY_ENABLE_LIVE", "1")
    for name in (
        "AWS_PROFILE",
        "AWS_REGION",
        "FARESENTRY_BEDROCK_MODEL_ID",
        "FARESENTRY_EMAIL_FROM",
        "FARESENTRY_EMAIL_TO",
    ):
        monkeypatch.setenv(name, "test-configuration")


def test_live_check_and_email_are_separate_explicit_actions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_live(monkeypatch)
    path = tmp_path / "live.sqlite3"
    factory = Mock(
        side_effect=[
            (demo.DemoFlightProvider(), demo.DemoRecommender()),
            (demo.DemoFlightProvider(opportunity=True), demo.DemoRecommender()),
        ]
    )
    monkeypatch.setattr(demo_live, "_live_dependencies", factory)
    sender = Mock(wraps=demo.DemoNotificationProvider())
    monkeypatch.setattr(SESEmailProvider, "from_environment", lambda: sender)
    first = demo_live.run_live_check(demo.demo_watch(), database_path=path)
    assert not first.result.alert_decision.should_alert
    assert (
        demo_live.deliver_live_alert(first, database_path=path).status == "not_needed"
    )
    second = demo_live.run_live_check(demo.demo_watch(), database_path=path)
    assert second.result.alert_decision.should_alert
    assert second.notification is None
    sender.send.assert_not_called()
    assert (
        demo_live.deliver_live_alert(second, database_path=path).status == "delivered"
    )
    assert (
        demo_live.deliver_live_alert(second, database_path=path).status
        == "already_delivered"
    )
    sender.send.assert_called_once()
    assert factory.call_count == 2


def test_live_failures_have_safe_messages(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_live(monkeypatch)
    monkeypatch.setattr(
        demo_live,
        "_live_dependencies",
        Mock(side_effect=RuntimeError("private raw credential data")),
    )
    report = demo_live.run_live_check(
        demo.demo_watch(), database_path=tmp_path / "live.sqlite3"
    )
    assert report.error
    assert report.result is None
    assert "private raw credential data" not in report.model_dump_json()


def test_history_display_failure_preserves_completed_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    configure_live(monkeypatch)
    monkeypatch.setattr(
        demo_live,
        "_live_dependencies",
        lambda: (demo.DemoFlightProvider(), demo.DemoRecommender()),
    )
    monkeypatch.setattr(
        demo_live.SQLiteFareHistory,
        "get_recent_observations",
        Mock(side_effect=RuntimeError("private payload")),
    )
    report = demo_live.run_live_check(
        demo.demo_watch(), database_path=tmp_path / "live.sqlite3"
    )
    assert report.result.status == "completed"
    assert report.error is None
    assert report.history_error
    assert report.current_candidate_ids is None
    assert (
        demo.safe_recommendation_text(
            report.result,
            report.result.recommendation.recommendation,
            current_candidate_ids=report.current_candidate_ids,
        )
        is None
    )
