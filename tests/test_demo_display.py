from collections.abc import Mapping
from pathlib import Path

import pytest

from faresentry import demo, demo_live
from faresentry.models import (
    FlightItinerary,
    HardTravelConstraints,
    Recommendation,
    RoundTripItinerary,
    TravelerSoftPreferences,
)
from faresentry.recommendations import parse_recommendation


@pytest.fixture(params=["demo", "live"])
def current_report(
    request: pytest.FixtureRequest,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[demo.CheckReport, str]:
    original_provider = demo.DemoFlightProvider
    original_recommend = demo.DemoRecommender.recommend
    alternatives: list[str] = []

    def provider(*, opportunity: bool = False) -> demo.DemoFlightProvider:
        instance = original_provider(opportunity=opportunity)
        trip = instance.trips[0]
        # Fourteen current candidates ensure the early alternative is outside
        # Live Mode's twelve-row visible history. All share one outbound.
        instance.trips = tuple(
            RoundTripItinerary(
                outbound=trip.outbound,
                inbound=FlightItinerary(
                    segments=(
                        trip.inbound.segments[0].model_copy(
                            update={"flight_number": f"DP return {index}"}
                        ),
                    )
                ),
                total_price=trip.total_price,
            )
            for index in range(14)
        )
        return instance

    def recommend(
        self: demo.DemoRecommender,
        candidates: Mapping[str, RoundTripItinerary],
        preferences: TravelerSoftPreferences,
        *,
        constraints: HardTravelConstraints,
    ) -> Recommendation:
        selected, alternative, *_ = candidates
        alternatives.append(alternative)
        data = original_recommend(
            self, candidates, preferences, constraints=constraints
        ).model_dump()
        data["key_tradeoffs"][0]["candidate_ids"] = (selected,)
        data["key_tradeoffs"][0]["explanation"] = f"A better fit than {alternative}."
        data["recommendation"] = f"The selected option is better than {alternative}."
        return parse_recommendation(data, candidate_ids=tuple(candidates))

    monkeypatch.setattr(demo.DemoRecommender, "recommend", recommend)
    if request.param == "demo":
        monkeypatch.setattr(demo, "DemoFlightProvider", provider)
        report = demo.run_demo("opportunity")
    else:
        monkeypatch.setenv("FARESENTRY_ENABLE_LIVE", "1")
        for name in ("AWS_PROFILE", "AWS_REGION", "FARESENTRY_BEDROCK_MODEL_ID"):
            monkeypatch.setenv(name, "test-configuration")
        instance = provider(opportunity=True)
        monkeypatch.setattr(
            demo_live, "_live_dependencies", lambda: (instance, demo.DemoRecommender())
        )
        report = demo_live.run_live_check(
            demo.demo_watch(), database_path=tmp_path / "live.sqlite3"
        )
        assert instance.search_calls == 1
        assert instance.return_calls == 3
    assert report.result.status == "completed"
    assert report.result.eligible_candidates == 14
    assert len(report.current_candidate_ids) == 14
    assert alternatives[-1] in report.current_candidate_ids
    assert all(
        alternatives[-1] not in item.candidate_ids
        for item in report.result.recommendation.key_tradeoffs
    )
    if request.param == "live":
        assert len(report.history) == 12
        assert alternatives[-1] not in {item.itinerary_id for item in report.history}
    return report, alternatives[-1]


def test_complete_ids_suppress_unreferenced_current_alternative(
    current_report: tuple[demo.CheckReport, str],
) -> None:
    report, alternative = current_report
    recommendation = report.result.recommendation
    before = recommendation.model_dump_json()
    assert (
        parse_recommendation(recommendation, candidate_ids=report.current_candidate_ids)
        == recommendation
    )
    for prose in (
        recommendation.recommendation,
        recommendation.key_tradeoffs[0].explanation,
    ):
        assert alternative in prose
        assert (
            demo.safe_recommendation_text(
                report.result, prose, current_candidate_ids=report.current_candidate_ids
            )
            is None
        )
    assert report.result.recommendation is recommendation
    assert recommendation.model_dump_json() == before


def test_render_excludes_alternative_and_keeps_safe_content(
    current_report: tuple[demo.CheckReport, str],
) -> None:
    pytest.importorskip("streamlit")
    from streamlit.testing.v1 import AppTest

    report, alternative = current_report
    before = report.result.recommendation.model_dump_json()
    app = AppTest.from_string(
        "import streamlit as st\nfrom streamlit_app import show_report\n"
        "show_report(st.session_state['report'])\n",
        default_timeout=15,
    )
    app.session_state["report"] = report
    app.run()
    assert not app.exception
    output = str(
        [
            item.value
            for kind in ("text", "caption", "markdown", "subheader", "info", "success")
            for item in app.get(kind)
        ]
    )
    assert alternative not in output
    assert "The selected option is better than" not in output
    assert "A better fit than" not in output
    assert any(item.value == "LAX → SIN" for item in app.text)
    assert any(
        item.label == "Round-trip fare" and item.value == "USD 800.00"
        for item in app.metric
    )
    assert alternative not in app.dataframe[0].value.to_string()
    assert report.result.recommendation.model_dump_json() == before

    safe = "A good itinerary-v1-style comparison favors this trip."
    recommendation = Recommendation.model_validate(
        report.result.recommendation.model_dump() | {"recommendation": safe}
    )
    app.session_state["report"] = report.model_copy(
        update={
            "result": report.result.model_copy(
                update={"recommendation": recommendation}
            )
        }
    )
    app.run()
    assert not app.exception
    assert any(item.value == safe for item in app.text)


def test_current_candidate_collection_is_run_scoped_and_requires_completeness() -> None:
    report = demo.run_demo("opportunity")
    result = report.result
    current = tuple(item for item in report.history if item.run_id == result.run_id)
    other_run = current[0].model_copy(
        update={"run_id": result.run_id + 1, "itinerary_id": "itinerary-v1-" + "a" * 64}
    )
    other_watch = current[0].model_copy(
        update={
            "watch_id": "watch-v1-" + "b" * 64,
            "itinerary_id": "itinerary-v1-" + "c" * 64,
        }
    )
    assert (
        demo.current_run_candidate_ids(result, (*current, other_run, other_watch))
        == report.current_candidate_ids
    )
    assert demo.current_run_candidate_ids(result, current[:1]) is None
    assert (
        demo.safe_recommendation_text(
            result, result.recommendation.recommendation, current_candidate_ids=None
        )
        is None
    )
