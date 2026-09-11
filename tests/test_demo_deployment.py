"""Public deployment defaults, exercised without any external services."""

from pathlib import Path
from unittest.mock import Mock

import pytest

from faresentry import demo, demo_live
from faresentry.providers.ses import SESEmailProvider

LIVE_SETTINGS = (
    "SERPAPI_API_KEY",
    "AWS_PROFILE",
    "AWS_REGION",
    "FARESENTRY_BEDROCK_MODEL_ID",
    "FARESENTRY_EMAIL_FROM",
    "FARESENTRY_EMAIL_TO",
)
APP = Path(__file__).resolve().parents[1] / "streamlit_app.py"


@pytest.mark.parametrize("configured", [False, True])
def test_public_startup_and_both_scenarios_need_no_live_access(
    configured: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    st = pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    # Community Cloud's legacy false setting permits stack traces.
    st.set_option("client.showErrorDetails", "stacktrace")
    monkeypatch.delenv("FARESENTRY_ENABLE_LIVE", raising=False)
    for name in LIVE_SETTINGS:
        if configured:
            monkeypatch.setenv(name, f"private-test-{name}")
        else:
            monkeypatch.delenv(name, raising=False)
    forbidden = Mock(side_effect=AssertionError("Public demo accessed live services"))
    monkeypatch.setattr(demo_live, "_live_dependencies", forbidden)
    monkeypatch.setattr(demo_live, "run_live_check", forbidden)
    monkeypatch.setattr(demo_live, "deliver_live_alert", forbidden)
    monkeypatch.setattr(SESEmailProvider, "from_environment", forbidden)
    run_demo = Mock(wraps=demo.run_demo)
    monkeypatch.setattr(demo, "run_demo", run_demo)

    app = AppTest.from_file(str(APP), default_timeout=15).run()
    assert not app.exception
    assert st.get_option("client.showErrorDetails") == "none"
    assert app.radio[0].options == ["Demo Mode"]
    assert not app.text_input
    run_demo.assert_not_called()

    for story, alert in (
        ("Routine check · stay silent", False),
        ("Worthwhile opportunity · alert", True),
    ):
        app.radio[1].set_value(story).run()
        app.button[0].click().run()
        assert not app.exception
        report = app.session_state["demo_report"]
        assert report.result.status == "completed"
        assert report.result.alert_decision.should_alert is alert
        assert report.result.alert_decision.previous_same_itinerary_price == 1000
        assert report.result.selected_itinerary.total_price == (800 if alert else 1000)
        assert report.simulated_sends == int(alert)
        if alert:
            assert any(
                "Simulated notification delivered" in x.value for x in app.success
            )
        else:
            assert any("Completed + silent" in x.value for x in app.info)
        output = str(
            [
                item.value
                for kind in ("text", "markdown", "caption", "info", "success", "error")
                for item in app.get(kind)
            ]
            + [item.value for item in app.dataframe]
        )
        for prohibited in (
            *report.current_candidate_ids,
            report.result.watch_id,
            "departure_token",
            "synthetic-token",
            "private-test-",
        ):
            assert prohibited not in output

    assert run_demo.call_count == 2
    app.run()
    assert run_demo.call_count == 2
    # A new browser session needs no state from the earlier checks.
    fresh = AppTest.from_file(str(APP), default_timeout=15).run()
    fresh.button[0].click().run()
    assert not fresh.exception
    assert any("Completed + silent" in x.value for x in fresh.info)
    forbidden.assert_not_called()


@pytest.mark.parametrize("opt_in", [None, "0", "true"])
def test_disabled_live_facade_cannot_check_or_send_even_with_configuration(
    opt_in: str | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    if opt_in is None:
        monkeypatch.delenv("FARESENTRY_ENABLE_LIVE", raising=False)
    else:
        monkeypatch.setenv("FARESENTRY_ENABLE_LIVE", opt_in)
    for name in LIVE_SETTINGS:
        monkeypatch.setenv(name, "private-test-setting")
    # A retained approved live result must also respect the deployment gate.
    retained = demo.run_demo("opportunity").model_copy(update={"simulated": False})
    forbidden = Mock(
        side_effect=AssertionError("Disabled live facade accessed a service")
    )
    monkeypatch.setattr(demo_live, "_live_dependencies", forbidden)
    monkeypatch.setattr(demo_live, "SQLiteFareHistory", forbidden)
    monkeypatch.setattr(SESEmailProvider, "from_environment", forbidden)
    path = tmp_path / "absent" / "live.sqlite3"
    report = demo_live.run_live_check(demo.demo_watch(), database_path=path)
    assert report.result is None
    assert "disabled" in report.error
    outcome = demo_live.deliver_live_alert(retained, database_path=path)
    assert outcome.status == "failed"
    assert outcome.error_type == "LiveModeDisabled"
    assert not path.parent.exists()
    forbidden.assert_not_called()
