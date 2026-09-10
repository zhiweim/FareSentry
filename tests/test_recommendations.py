import json
from collections.abc import AsyncIterator
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest
from pydantic import ValidationError
from strands.models import Model
from strands.types.content import Messages
from strands.types.exceptions import StructuredOutputException
from strands.types.streaming import StreamEvent
from strands.types.tools import ToolSpec

from faresentry.agents.recommendation import SYSTEM_PROMPT, StrandsRecommender
from faresentry.models import (
    AirportTransfer,
    FlightItinerary,
    FlightSegment,
    HardTravelConstraints,
    Layover,
    Recommendation,
    RecommendationTradeoff,
    RoundTripItinerary,
    TravelerSoftPreferences,
)
from faresentry.recommendations import (
    InvalidRecommendationError,
    RecommendationError,
    build_recommendation_request,
    parse_recommendation,
)


def segment(
    origin: str,
    destination: str,
    minutes: int,
    number: str,
    airline: str = "Example Air",
) -> FlightSegment:
    return FlightSegment(
        origin=origin,
        destination=destination,
        duration_minutes=minutes,
        flight_number=number,
        airline=airline,
    )


@pytest.fixture
def candidates() -> dict[str, RoundTripItinerary]:
    return {
        "budget": RoundTripItinerary(
            outbound=FlightItinerary(
                segments=(
                    segment("LAX", "HND", 180, "EA 1"),
                    segment("NRT", "ICN", 120, "OA 2", "Other Air"),
                    segment("ICN", "SIN", 360, "EA 3"),
                ),
                layovers=(
                    AirportTransfer(
                        arrival_airport="HND",
                        departure_airport="NRT",
                        duration_minutes=150,
                    ),
                    Layover(airport="ICN", duration_minutes=90),
                ),
            ),
            inbound=FlightItinerary(
                segments=(
                    segment("SIN", "ICN", 360, "OA 4", "Other Air"),
                    segment("ICN", "LAX", 300, "EA 5"),
                ),
                layovers=(Layover(airport="ICN", duration_minutes=60),),
            ),
            total_price=Decimal("800.00"),
        ),
        "direct": RoundTripItinerary(
            outbound=FlightItinerary(segments=(segment("LAX", "SIN", 600, "EA 6"),)),
            inbound=FlightItinerary(segments=(segment("SIN", "LAX", 600, "EA 7"),)),
            total_price=Decimal("1000.00"),
        ),
    }


@pytest.fixture
def preferences() -> TravelerSoftPreferences:
    return TravelerSoftPreferences(
        preferred_max_stops_per_direction=0,
        preferred_airlines=("example air",),
        willingness_to_pay_more="high",
    )


def tradeoff(
    candidate_ids: tuple[str, ...] = ("budget", "direct"),
    favored_candidate_id: str | None = "direct",
    category: str = "total_travel_time",
) -> dict[str, object]:
    return {
        "category": category,
        "candidate_ids": list(candidate_ids),
        "favored_candidate_id": favored_candidate_id,
        "explanation": "Shorter travel matters to this traveler.",
    }


def output(candidate_id: str = "direct") -> dict[str, object]:
    return {
        "selected_candidate_id": candidate_id,
        "recommendation": f"Choose {candidate_id} for its fit to your preferences.",
        "key_tradeoffs": [tradeoff((candidate_id,), candidate_id, "overall_value")],
        "confidence": "high",
    }


def test_candidate_summaries_preserve_facts(
    candidates: dict[str, RoundTripItinerary], preferences: TravelerSoftPreferences
) -> None:
    before = {key: value.model_dump_json() for key, value in candidates.items()}
    request = build_recommendation_request(
        candidates, preferences, constraints=HardTravelConstraints()
    )
    budget, direct = request.candidates
    assert budget.outbound.model_dump() == {
        "origin": "LAX",
        "destination": "SIN",
        "duration_minutes": 900,
        "stops": 2,
        "stops_above_preference": 2,
        "connections": (
            {
                "arrival_airport": "HND",
                "departure_airport": "NRT",
                "duration_minutes": 150,
                "requires_airport_transfer": True,
            },
            {
                "arrival_airport": "ICN",
                "departure_airport": "ICN",
                "duration_minutes": 90,
                "requires_airport_transfer": False,
            },
        ),
        "total_connection_minutes": 240,
        "max_connection_minutes": 150,
        "airport_transfer_count": 1,
        "airlines": ("Example Air", "Other Air"),
        "preferred_airline_matches": ("Example Air",),
        "flight_numbers": ("EA 1", "OA 2", "EA 3"),
    }
    assert budget.inbound.duration_minutes == 720
    assert budget.inbound.stops == 1
    assert budget.inbound.connections[0].duration_minutes == 60
    assert budget.inbound.airport_transfer_count == 0
    assert budget.total_round_trip_price == Decimal("800.00")
    assert budget.currency == "USD"
    assert budget.total_travel_minutes == 1620
    assert budget.total_stops == 3
    assert budget.total_connection_minutes == 300
    assert budget.max_connection_minutes == 150
    assert budget.airport_transfer_count == 1
    assert budget.airlines == ("Example Air", "Other Air")
    assert budget.flight_numbers == ("EA 1", "OA 2", "EA 3", "OA 4", "EA 5")
    assert direct.total_travel_minutes == 1200
    assert direct.total_stops == direct.airport_transfer_count == 0
    assert direct.outbound.connections == ()
    assert direct.total_connection_minutes == direct.max_connection_minutes == 0
    assert direct.outbound.stops_above_preference == 0
    assert request.preferences == preferences
    assert (
        build_recommendation_request(
            candidates, preferences, constraints=HardTravelConstraints()
        ).model_dump_json()
        == request.model_dump_json()
    )
    assert before == {key: value.model_dump_json() for key, value in candidates.items()}


def test_python_computes_comparisons(
    candidates: dict[str, RoundTripItinerary], preferences: TravelerSoftPreferences
) -> None:
    request = build_recommendation_request(
        candidates, preferences, constraints=HardTravelConstraints()
    )
    cheaper, dearer = request.comparisons
    assert dearer.model_dump() == {
        "candidate_id": "direct",
        "reference_candidate_id": "budget",
        "price_difference": Decimal("200.00"),
        "price_difference_percent": Decimal("25.00"),
        "outbound_duration_difference_minutes": -300,
        "inbound_duration_difference_minutes": -120,
        "total_travel_difference_minutes": -420,
        "outbound_stops_difference": -2,
        "inbound_stops_difference": -1,
        "total_stops_difference": -3,
        "total_connection_difference_minutes": -300,
        "max_connection_difference_minutes": -150,
        "airport_transfer_count_difference": -1,
    }
    assert cheaper.price_difference_percent == Decimal("-20.00")
    assert cheaper.price_difference == Decimal("-200.00")
    assert cheaper.total_travel_difference_minutes == 420


@pytest.mark.parametrize(
    ("price", "expected"),
    [("0", None), ("3", Decimal("33233.33")), ("1000", Decimal("0.00"))],
)
def test_percentage_zero_rounding_and_ties(
    candidates: dict[str, RoundTripItinerary], price: str, expected: Decimal | None
) -> None:
    candidates["budget"] = candidates["budget"].model_copy(
        update={"total_price": Decimal(price)}
    )
    request = build_recommendation_request(
        candidates, TravelerSoftPreferences(), constraints=HardTravelConstraints()
    )
    assert request.comparisons[1].price_difference_percent == expected


def test_single_candidate_and_unspecified_preferences(
    candidates: dict[str, RoundTripItinerary],
) -> None:
    request = build_recommendation_request(
        {"direct": candidates["direct"]},
        TravelerSoftPreferences(),
        constraints=HardTravelConstraints(),
    )
    assert request.comparisons == ()
    assert request.candidates[0].outbound.stops_above_preference is None
    assert request.candidates[0].outbound.preferred_airline_matches == ()


@pytest.mark.parametrize(
    "fields",
    [
        {"preferred_max_stops_per_direction": -1},
        {"preferred_max_stops_per_direction": True},
        {"preferred_max_stops_per_direction": 1.5},
        {"prefer_shorter_total_travel_time": "yes"},
        {"prefer_shorter_connections": 1},
        {"dislike_airport_transfers": "false"},
        {"preferred_airlines": [" "]},
        {"willingness_to_pay_more": "unlimited"},
        {"max_price": 500},
    ],
)
def test_soft_preference_validation(fields: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        TravelerSoftPreferences.model_validate(fields)


@pytest.mark.parametrize("selected", ["budget", "direct"])
def test_agent_receives_facts_and_can_choose_any_acceptable_candidate(
    candidates: dict[str, RoundTripItinerary],
    preferences: TravelerSoftPreferences,
    selected: str,
) -> None:
    agent = Mock(return_value=SimpleNamespace(structured_output=output(selected)))
    factory = Mock(return_value=agent)
    recommender = StrandsRecommender(model="injected-model", agent_factory=factory)
    factory.assert_not_called()
    result = recommender.recommend(
        candidates, preferences, constraints=HardTravelConstraints()
    )
    assert result.selected_candidate_id == selected
    assert isinstance(result, Recommendation)
    factory.assert_called_once_with(
        model="injected-model",
        system_prompt=SYSTEM_PROMPT,
        tools=[],
        callback_handler=None,
    )
    prompt = agent.call_args.args[0]
    assert agent.call_args.kwargs == {"structured_output_model": Recommendation}
    payload = json.loads(prompt)
    assert set(payload) == {"preferences", "candidates", "comparisons"}
    assert payload["preferences"] == preferences.model_dump(mode="json")
    assert payload["candidates"][0]["total_round_trip_price"] == "800.00"
    assert payload["candidates"][0]["outbound"]["duration_minutes"] == 900
    assert payload["candidates"][0]["inbound"]["duration_minutes"] == 720
    assert payload["comparisons"][1]["price_difference_percent"] == "25.00"
    assert payload == build_recommendation_request(
        candidates, preferences, constraints=HardTravelConstraints()
    ).model_dump(mode="json")


def test_each_recommendation_uses_fresh_agent(
    candidates: dict[str, RoundTripItinerary], preferences: TravelerSoftPreferences
) -> None:
    first = Mock(return_value=SimpleNamespace(structured_output=output("budget")))
    second = Mock(return_value=SimpleNamespace(structured_output=output("direct")))
    factory = Mock(side_effect=[first, second])
    recommender = StrandsRecommender(model="fake", agent_factory=factory)
    for candidate_id in candidates:
        result = recommender.recommend(
            {candidate_id: candidates[candidate_id]},
            preferences,
            constraints=HardTravelConstraints(),
        )
        assert result.selected_candidate_id == candidate_id
    assert factory.call_count == 2
    first.assert_called_once()
    second.assert_called_once()
    assert '"budget"' not in second.call_args.args[0]


@pytest.mark.parametrize("direction", ["outbound", "inbound"])
def test_hard_constraint_failure_never_reaches_agent(
    candidates: dict[str, RoundTripItinerary],
    preferences: TravelerSoftPreferences,
    direction: str,
) -> None:
    # Isolate the violating direction to prove both directions are gated.
    other = "inbound" if direction == "outbound" else "outbound"
    candidates["budget"] = candidates["budget"].model_copy(
        update={other: getattr(candidates["direct"], other)}
    )
    factory = Mock()
    with pytest.raises(ValueError, match="hard constraints"):
        StrandsRecommender(model="fake", agent_factory=factory).recommend(
            candidates,
            preferences,
            constraints=HardTravelConstraints(max_stops_per_direction=0),
        )
    factory.assert_not_called()


@pytest.mark.parametrize(
    "problem", ["empty", "currency", "route", "blank_id", "padded_id"]
)
def test_invalid_candidate_sets_never_reach_agent(
    candidates: dict[str, RoundTripItinerary], problem: str
) -> None:
    if problem == "empty":
        candidates = {}
    elif problem == "currency":
        candidates["direct"] = candidates["direct"].model_copy(
            update={"currency": "EUR"}
        )
    elif problem == "route":
        candidates["direct"] = RoundTripItinerary(
            outbound=FlightItinerary(segments=(segment("SFO", "SIN", 600, "EA 6"),)),
            inbound=FlightItinerary(segments=(segment("SIN", "SFO", 600, "EA 7"),)),
            total_price=Decimal("1000"),
        )
    else:
        candidates[" " if problem == "blank_id" else " direct "] = candidates.pop(
            "direct"
        )
    factory = Mock()
    with pytest.raises(ValueError):
        StrandsRecommender(model="fake", agent_factory=factory).recommend(
            candidates, TravelerSoftPreferences(), constraints=HardTravelConstraints()
        )
    factory.assert_not_called()


@pytest.mark.parametrize("form", ["dict", "json", "model"])
def test_parse_structured_recommendation(form: str) -> None:
    value: object = output()
    if form == "json":
        value = json.dumps(value)
    elif form == "model":
        value = Recommendation.model_validate(value)
    assert parse_recommendation(value, candidate_ids=("budget", "direct")) == (
        Recommendation.model_validate(output())
    )


@pytest.mark.parametrize(
    "value",
    [
        None,
        "not JSON",
        "{}",
        [],
        {**output(), "selected_candidate_id": "invented"},
        {**output(), "selected_candidate_id": 7},
        {**output(), "recommendation": " "},
        {**output(), "recommendation": "x" * 1001},
        {**output(), "key_tradeoffs": []},
        {**output(), "key_tradeoffs": [{**tradeoff(), "explanation": " "}]},
        {**output(), "key_tradeoffs": [tradeoff()] * 7},
        {**output(), "confidence": 1.1},
        {**output(), "confidence": "certain"},
        {**output(), "should_alert": True},
        {**output(), "hard_constraint_violated": False},
        Recommendation.model_construct(**{**output(), "confidence": "invalid"}),
    ],
)
def test_malformed_output_fails_cleanly(
    candidates: dict[str, RoundTripItinerary], value: object
) -> None:
    agent = Mock(return_value=SimpleNamespace(structured_output=value))
    recommender = StrandsRecommender(
        model="fake", agent_factory=Mock(return_value=agent)
    )
    with pytest.raises(InvalidRecommendationError):
        recommender.recommend(
            candidates, TravelerSoftPreferences(), constraints=HardTravelConstraints()
        )


@pytest.mark.parametrize("form", ["dict", "json", "model"])
def test_typed_tradeoffs_serialize_and_support_structured_decisions(form: str) -> None:
    value = {
        **output(),
        "key_tradeoffs": [
            tradeoff(favored_candidate_id="budget", category="price"),
            tradeoff(),
            tradeoff(category="overall_value"),
        ],
    }
    structured: object = value
    if form == "json":
        structured = json.dumps(value)
    elif form == "model":
        structured = Recommendation.model_validate(value)
    result = parse_recommendation(structured, candidate_ids=("budget", "direct"))
    assert all(
        isinstance(item, RecommendationTradeoff) for item in result.key_tradeoffs
    )
    assert [item.category for item in result.key_tradeoffs] == [
        "price",
        "total_travel_time",
        "overall_value",
    ]
    # Consumers can distinguish price preference from the final choice without prose.
    assert result.key_tradeoffs[0].favored_candidate_id == "budget"
    assert result.key_tradeoffs[0].candidate_ids == ("budget", "direct")
    assert result.key_tradeoffs[2].favored_candidate_id == result.selected_candidate_id
    assert json.loads(result.model_dump_json()) == value


@pytest.mark.parametrize(
    "fields",
    [
        tradeoff(("budget", "direct", "ghost")),
        tradeoff(favored_candidate_id="ghost"),
        tradeoff(("budget", "ghost"), "ghost"),
        tradeoff(("budget",), "direct"),  # Known, but not among discussed candidates.
        tradeoff(category="future_fare"),
        tradeoff(category="convenience"),  # Use overall_value for combined judgment.
        tradeoff((), None),
        tradeoff(("direct", "direct")),
        tradeoff(("direct", " direct ")),  # Duplicate after normalization.
        tradeoff(category="overall_value", favored_candidate_id="budget"),
        tradeoff(category="overall_value", favored_candidate_id=None),
        {**tradeoff(), "final_recommendation_target": "ghost"},
        {**tradeoff(), "explanation": "x" * 501},
    ],
)
def test_invalid_typed_tradeoffs_rejected_by_adapter(
    candidates: dict[str, RoundTripItinerary], fields: dict[str, object]
) -> None:
    agent = Mock(
        return_value=SimpleNamespace(
            structured_output={**output(), "key_tradeoffs": [fields]}
        )
    )
    recommender = StrandsRecommender(
        model="fake", agent_factory=Mock(return_value=agent)
    )
    with pytest.raises(InvalidRecommendationError):
        recommender.recommend(
            candidates, TravelerSoftPreferences(), constraints=HardTravelConstraints()
        )


def test_unknown_selection_rejected_even_when_tradeoffs_agree() -> None:
    with pytest.raises(InvalidRecommendationError, match="unknown candidate ID"):
        parse_recommendation(output("ghost"), candidate_ids=("budget", "direct"))


def test_nested_model_instances_revalidated_for_request_membership() -> None:
    value = Recommendation.model_validate(output())
    invalid_tradeoff = value.key_tradeoffs[0].model_copy(
        update={"candidate_ids": ("direct", "ghost")}
    )
    altered = value.model_copy(update={"key_tradeoffs": (invalid_tradeoff,)})
    with pytest.raises(InvalidRecommendationError, match="unknown candidate ID"):
        parse_recommendation(altered, candidate_ids=("budget", "direct"))


@pytest.mark.parametrize("favored", ["budget", "direct", "third", None])
def test_multiple_discussed_candidates_and_no_favored_candidate(
    favored: str | None,
) -> None:
    ids = ("budget", "direct", "third")
    result = parse_recommendation(
        {**output(), "key_tradeoffs": [tradeoff(ids, favored)]}, candidate_ids=ids
    )
    assert result.key_tradeoffs[0].candidate_ids == ids
    assert result.key_tradeoffs[0].favored_candidate_id == favored


def test_prose_is_display_only_and_never_interpreted_as_structured_reasoning() -> None:
    value = {
        **output(),
        "recommendation": "Book candidate ghost.",
        "key_tradeoffs": [{**tradeoff(), "explanation": "Ghost is faster."}],
    }
    result = parse_recommendation(value, candidate_ids=("budget", "direct"))
    # Factual prose checking is outside this schema boundary. Machine consumers
    # use only the validated IDs/category below; no prose-based inference occurs.
    assert result.selected_candidate_id == "direct"
    assert result.key_tradeoffs[0].candidate_ids == ("budget", "direct")
    assert result.key_tradeoffs[0].favored_candidate_id == "direct"
    assert result.key_tradeoffs[0].category == "total_travel_time"
    assert result.recommendation == value["recommendation"]


@pytest.mark.parametrize("where", ["creation", "invocation"])
@pytest.mark.parametrize(
    ("failure", "error_type"),
    [
        (StructuredOutputException("private output"), InvalidRecommendationError),
        (RuntimeError("private credentials"), RecommendationError),
    ],
)
def test_sdk_errors_have_clean_messages(
    candidates: dict[str, RoundTripItinerary],
    where: str,
    failure: Exception,
    error_type: type[Exception],
) -> None:
    factory = (
        Mock(side_effect=failure)
        if where == "creation"
        else Mock(return_value=Mock(side_effect=failure))
    )
    with pytest.raises(error_type) as caught:
        StrandsRecommender(model="fake", agent_factory=factory).recommend(
            candidates, TravelerSoftPreferences(), constraints=HardTravelConstraints()
        )
    assert "private" not in str(caught.value)
    assert caught.value.__suppress_context__


@pytest.mark.parametrize("model", [None, "", " "])
def test_model_configuration_is_explicit(model: str | None) -> None:
    with pytest.raises(ValueError, match="explicit model"):
        StrandsRecommender(model=model)


class ScriptedModel(Model):
    """Exercise the real Strands event loop using only local tool-use events."""

    def __init__(self) -> None:
        self.received_messages: list[Messages] = []
        self.received_tools: list[list[ToolSpec]] = []

    def update_config(self, **model_config: Any) -> None:
        pass

    def get_config(self) -> dict[str, str]:
        return {"model_id": "offline-scripted-model"}

    def structured_output(self, *args: Any, **kwargs: Any) -> Any:
        raise AssertionError("The deprecated structured_output API must not be used")

    async def stream(
        self,
        messages: Messages,
        tool_specs: list[ToolSpec] | None = None,
        system_prompt: str | None = None,
        **kwargs: Any,
    ) -> AsyncIterator[StreamEvent]:
        self.received_messages.append(json.loads(json.dumps(messages)))
        self.received_tools.append(tool_specs or [])
        assert system_prompt == SYSTEM_PROMPT
        assert tool_specs is not None and len(tool_specs) == 1
        yield {"messageStart": {"role": "assistant"}}
        yield {
            "contentBlockStart": {
                "start": {
                    "toolUse": {"toolUseId": "choice-1", "name": tool_specs[0]["name"]}
                }
            }
        }
        yield {
            "contentBlockDelta": {"delta": {"toolUse": {"input": json.dumps(output())}}}
        }
        yield {"contentBlockStop": {}}
        yield {"messageStop": {"stopReason": "tool_use"}}


def test_real_strands_structured_output_with_offline_model(
    candidates: dict[str, RoundTripItinerary], preferences: TravelerSoftPreferences
) -> None:
    model = ScriptedModel()
    result = StrandsRecommender(model=model).recommend(
        candidates, preferences, constraints=HardTravelConstraints()
    )
    assert result == Recommendation.model_validate(output())
    assert len(model.received_messages) == 1
    prompt = model.received_messages[0][0]["content"][0]["text"]
    assert json.loads(prompt)["candidates"][0]["total_travel_minutes"] == 1620
    schema = model.received_tools[0][0]["inputSchema"]["json"]
    assert "selected_candidate_id" in schema["required"]
    assert schema["properties"]["confidence"]["enum"] == ["low", "medium", "high"]
    assert result.key_tradeoffs[0].category == "overall_value"
    assert result.key_tradeoffs[0].candidate_ids == ("direct",)
    assert result.key_tradeoffs[0].favored_candidate_id == "direct"
