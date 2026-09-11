from pathlib import Path
from unittest.mock import Mock

import pytest

from faresentry import demo, demo_live
from faresentry.notification_models import NotificationOutcome

pytest.importorskip("streamlit", reason="Install the ui extra to run rendering tests")
from streamlit.testing.v1 import AppTest

APP = Path(__file__).resolve().parents[1] / "streamlit_app.py"


@pytest.fixture(autouse=True)
def enable_local_live_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FARESENTRY_ENABLE_LIVE", "1")


def test_initial_render_and_rerenders_do_not_run_checks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    simulated = Mock(wraps=demo.run_demo)
    live = Mock(side_effect=AssertionError("Unexpected live call"))
    monkeypatch.setattr(demo, "run_demo", simulated)
    monkeypatch.setattr(demo_live, "run_live_check", live)
    monkeypatch.setattr(demo_live, "deliver_live_alert", live)
    app = AppTest.from_file(str(APP), default_timeout=15).run()
    assert not app.exception
    simulated.assert_not_called()
    live.assert_not_called()
    app.button[0].click().run()
    assert not app.exception
    assert simulated.call_count == 1
    assert any("Completed + silent" in item.value for item in app.info)
    app.run()
    app.radio[1].set_value("Worthwhile opportunity · alert").run()
    assert simulated.call_count == 1
    app.button[0].click().run()
    assert not app.exception
    assert simulated.call_count == 2
    assert any("Simulated notification delivered" in item.value for item in app.success)
    report = app.session_state["demo_report"]
    output = str(
        [
            item.value
            for kind in ("text", "markdown", "caption", "info", "success")
            for item in app.get(kind)
        ]
    )
    for secret in (
        report.result.selected_candidate_id,
        report.result.watch_id,
        "synthetic-token",
        "departure_token",
        "test-key-not-a-real-credential",
    ):
        assert secret not in output
    live.assert_not_called()


@pytest.mark.parametrize(
    ("field", "value"),
    [("Origin airport", "BAD CODE"), ("Minimum fare improvement (USD)", "nope")],
)
def test_invalid_form_is_rejected_before_live_calls(
    field: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    live = Mock()
    monkeypatch.setattr(demo_live, "run_live_check", live)
    app = AppTest.from_file(str(APP), default_timeout=15).run()
    app.radio[0].set_value("Live Mode").run()
    next(item for item in app.text_input if item.label == field).set_value(value)
    app.button[0].click().run()
    assert not app.exception
    assert any("Check the trip" in item.value for item in app.error)
    live.assert_not_called()


def test_live_form_creates_domain_configuration_only_on_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = []

    def run(configuration):
        captured.append(configuration)
        return demo.CheckReport(
            configuration=configuration,
            checked_at=demo.DEMO_NOW,
            error="Fixture: no network used",
        )

    monkeypatch.setattr(demo_live, "run_live_check", run)
    app = AppTest.from_file(str(APP), default_timeout=15).run()
    app.radio[0].set_value("Live Mode").run()
    assert not captured
    next(item for item in app.text_input if item.label == "Origin airport").set_value(
        "pdx"
    )
    app.button[0].click().run()
    assert not app.exception
    assert len(captured) == 1
    configuration = captured[0]
    assert configuration.watch.origin == "PDX"
    assert configuration.constraints.max_stops_per_direction == 1
    assert configuration.preferences.willingness_to_pay_more == "moderate"
    assert configuration.policy.minimum_absolute_improvement == 100
    app.run()
    assert len(captured) == 1


def test_live_configuration_message_and_mode_isolation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "SERPAPI_API_KEY",
        "AWS_PROFILE",
        "AWS_REGION",
        "FARESENTRY_BEDROCK_MODEL_ID",
    ):
        monkeypatch.delenv(name, raising=False)
    factory = Mock()
    monkeypatch.setattr(demo_live, "_live_dependencies", factory)
    app = AppTest.from_file(str(APP), default_timeout=15).run()
    app.button[0].click().run()
    app.radio[0].set_value("Live Mode").run()
    assert not app.exception
    assert any("Not ready" in item.value for item in app.info)
    assert not any("SYNTHETIC DEMO RESULT" in item.value for item in app.caption)
    app.button[0].click().run()
    assert not app.exception
    assert any("not configured" in item.value for item in app.error)
    factory.assert_not_called()


def test_real_email_requires_separate_click_and_preserves_failed_delivery_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in (
        "AWS_PROFILE",
        "AWS_REGION",
        "FARESENTRY_EMAIL_FROM",
        "FARESENTRY_EMAIL_TO",
    ):
        monkeypatch.setenv(name, "test-configuration")
    report = demo.run_demo("opportunity").model_copy(
        update={"simulated": False, "notification": None}
    )
    sender = Mock(
        return_value=NotificationOutcome(
            watch_id=report.result.watch_id,
            run_id=report.result.run_id,
            status="failed",
            failure_stage="provider",
            error_type="NotificationProviderError",
        )
    )
    monkeypatch.setattr(demo_live, "deliver_live_alert", sender)
    app = AppTest.from_file(str(APP), default_timeout=15).run()
    app.session_state["live_report"] = report
    app.radio[0].set_value("Live Mode").run()
    sender.assert_not_called()
    next(
        item for item in app.button if item.label.startswith("Send this approved")
    ).click().run()
    assert not app.exception
    sender.assert_called_once_with(report)
    after = app.session_state["live_report"]
    assert after.result == report.result
    assert after.notification.status == "failed"
    assert any("Email delivery failed" in item.value for item in app.error)
    app.run()
    sender.assert_called_once()
