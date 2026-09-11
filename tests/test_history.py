import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import UTC, datetime, timedelta, timezone
from decimal import ROUND_UP, Decimal, localcontext
from pathlib import Path

import pytest
from pydantic import ValidationError

from faresentry.history import (
    FareWatch,
    calculate_price_statistics,
    itinerary_identity,
)
from faresentry.models import (
    AirportTransfer,
    FlightItinerary,
    FlightSegment,
    RoundTripItinerary,
    TripQuery,
)
from faresentry.persistence import SQLiteFareHistory

START = datetime(2026, 9, 1, 12, tzinfo=UTC)


@pytest.fixture
def watch() -> FareWatch:
    return FareWatch(
        origin="LAX",
        destination="SIN",
        outbound_date="2026-12-01",
        return_date="2026-12-15",
    )


@pytest.fixture
def repository(tmp_path: Path) -> SQLiteFareHistory:
    return SQLiteFareHistory(tmp_path / "history.sqlite3")


def fare(price: str = "1000.00", currency: str = "USD") -> RoundTripItinerary:
    def segment(origin: str, destination: str, number: str) -> FlightSegment:
        return FlightSegment(
            origin=origin,
            destination=destination,
            airline="Example Air",
            flight_number=number,
            duration_minutes=300,
        )

    return RoundTripItinerary(
        outbound=FlightItinerary(
            segments=(segment("LAX", "HND", "EA 1"), segment("NRT", "SIN", "EA 2")),
            layovers=(
                AirportTransfer(
                    arrival_airport="HND",
                    departure_airport="NRT",
                    duration_minutes=150,
                ),
            ),
        ),
        inbound=FlightItinerary(segments=(segment("SIN", "LAX", "EA 3"),)),
        total_price=Decimal(price),
        currency=currency,
    )


def test_initialization_is_idempotent(tmp_path: Path) -> None:
    path = tmp_path / "history.sqlite3"
    SQLiteFareHistory(path)
    SQLiteFareHistory(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        } == {
            "watches",
            "fare_observations",
            "monitoring_runs",
            "notification_deliveries",
        }
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("version", [5, 99])
def test_unknown_schema_is_not_overwritten(tmp_path: Path, version: int) -> None:
    path = tmp_path / "future.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute(f"PRAGMA user_version = {version}")
    with pytest.raises(ValueError, match="Unsupported"):
        SQLiteFareHistory(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == version
        assert connection.execute("SELECT * FROM sqlite_master").fetchall() == []


def test_unversioned_existing_database_is_not_modified(tmp_path: Path) -> None:
    path = tmp_path / "unrelated.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
    with pytest.raises(ValueError, match="nonempty unversioned"):
        SQLiteFareHistory(path)
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute("SELECT name FROM sqlite_master").fetchall() == [
            ("unrelated",)
        ]


def test_one_observation_and_reopen(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    itinerary = fare()
    result = repository.record_observation(watch, itinerary, observed_at=START)
    assert result.observation_id == 1
    assert result.watch_id == watch.watch_id
    assert result.itinerary_id == itinerary_identity(watch, itinerary)
    assert result.total_price == itinerary.total_price
    assert result.currency == "USD"
    assert result.observed_at == START
    assert result.outbound == itinerary.outbound
    assert result.inbound == itinerary.inbound
    assert result.outbound.stops == 1
    assert result.outbound.duration_minutes == 750
    assert result.outbound.connections[0].requires_airport_transfer
    reopened = SQLiteFareHistory(repository.database_path)
    assert reopened.get_recent_observations(watch) == [result]


def test_chronological_order_limits_and_timestamp_ties(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    latest = repository.record_observation(
        watch, fare("950"), observed_at=START + timedelta(days=2)
    )
    first = repository.record_observation(watch, fare("900"), observed_at=START)
    tied = repository.record_observation(watch, fare("800"), observed_at=START)
    assert repository.get_recent_observations(watch) == [first, tied, latest]
    assert repository.get_recent_observations(watch, limit=2) == [tied, latest]
    assert repository.get_recent_observations(watch, limit=1) == [latest]
    assert repository.get_recent_observations(watch, limit=99) == [first, tied, latest]
    stats = repository.get_price_statistics(watch)
    assert stats.current_price == Decimal("950")
    assert stats.previous_price == Decimal("800")


@pytest.mark.parametrize(
    "updates",
    [
        {"origin": "SFO"},
        {"destination": "HND"},
        {"outbound_date": "2026-12-02"},
        {"return_date": "2026-12-16"},
        {"currency": "EUR"},
    ],
)
def test_every_search_parameter_isolates_history(
    repository: SQLiteFareHistory, watch: FareWatch, updates: dict[str, str]
) -> None:
    other = FareWatch.model_validate(watch.model_dump() | updates)
    assert other.watch_id != watch.watch_id
    repository.record_observation(watch, fare())
    assert repository.get_recent_observations(other) == []
    assert repository.get_lowest_price(other) is None
    assert repository.get_price_statistics(other).observation_count == 0


def test_watch_identity_is_canonical_and_immutable(watch: FareWatch) -> None:
    query = TripQuery.model_validate(watch.model_dump())
    reconstructed = FareWatch.from_query(query)
    reordered = FareWatch.model_validate(
        dict(reversed(list(watch.model_dump().items())))
    )
    assert reconstructed.watch_id == reordered.watch_id == watch.watch_id
    query.origin = "SFO"
    assert reconstructed.origin == "LAX"
    with pytest.raises(ValidationError, match="frozen"):
        reconstructed.origin = "SFO"


@pytest.mark.parametrize("currency", ["USD", "EUR", "JPY", "KWD"])
def test_currency_preservation_and_separation(
    repository: SQLiteFareHistory, watch: FareWatch, currency: str
) -> None:
    other = FareWatch.model_validate(watch.model_dump() | {"currency": currency})
    repository.record_observation(other, fare("12.345", currency))
    assert repository.get_recent_observations(other)[0].currency == currency
    assert repository.get_price_statistics(other).currency == currency


@pytest.mark.parametrize(
    "price",
    ["0", "0.10", "999.9900", "12.345", "1E-20", "12345678901234567890.123456789"],
)
def test_decimal_round_trip_uses_text(
    repository: SQLiteFareHistory, watch: FareWatch, price: str
) -> None:
    repository.record_observation(watch, fare(price))
    restored = repository.get_recent_observations(watch)[0].total_price
    assert restored.as_tuple() == Decimal(price).as_tuple()
    with closing(sqlite3.connect(repository.database_path)) as connection:
        stored, storage_type = connection.execute(
            "SELECT total_price, typeof(total_price) FROM fare_observations"
        ).fetchone()
    assert stored == str(Decimal(price))
    assert storage_type == "text"


def test_statistics_are_numeric_and_comparisons_use_prior_history(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    for index, price in enumerate(["100.10", "9.90", "20.00"]):
        repository.record_observation(
            watch, fare(price), observed_at=START + timedelta(days=index)
        )
    stats = repository.get_price_statistics(watch)
    assert stats.observation_count == 3
    assert stats.minimum_price == repository.get_lowest_price(watch) == Decimal("9.90")
    assert stats.maximum_price == Decimal("100.10")
    assert stats.average_price == Decimal("43.33333333333333333333333333")
    assert stats.current_price == Decimal("20.00")
    assert stats.previous_price == stats.historical_low_price == Decimal("9.90")
    assert (
        stats.current_vs_previous == stats.current_vs_historical_low == Decimal("10.10")
    )
    repository.record_observation(
        watch, fare("0"), observed_at=START + timedelta(days=3)
    )
    stats = repository.get_price_statistics(watch)
    assert stats.minimum_price == Decimal(0)
    assert stats.average_price == Decimal("32.50")
    assert stats.current_vs_previous == Decimal("-20.00")
    assert stats.current_vs_historical_low == Decimal("-9.90")


def test_zero_previous_and_repeated_records(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    repository.record_observation(watch, fare("0"), observed_at=START)
    repository.record_observation(watch, fare("0"), observed_at=START)
    repository.record_observation(watch, fare("0.30"), observed_at=START)
    stats = repository.get_price_statistics(watch)
    assert stats.observation_count == 3
    assert stats.average_price == Decimal("0.10")
    assert (
        stats.current_vs_previous == stats.current_vs_historical_low == Decimal("0.30")
    )


def test_statistics_ignore_ambient_decimal_context(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    for price in ["12345678901234567890.123456789", "12345678901234567890.123456791"]:
        repository.record_observation(watch, fare(price), observed_at=START)
    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_UP
        stats = repository.get_price_statistics(watch)
    assert stats.average_price == Decimal("12345678901234567890.123456790")
    assert stats.current_vs_previous == Decimal("0.000000002")


def test_empty_and_single_observation_statistics(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    assert repository.get_recent_observations(watch) == []
    assert repository.get_lowest_price(watch) is None
    empty = repository.get_price_statistics(watch).model_dump()
    assert empty.pop("observation_count") == 0
    assert empty.pop("currency") == "USD"
    assert all(value is None for value in empty.values())
    repository.record_observation(watch, fare("100"))
    stats = repository.get_price_statistics(watch)
    assert stats.observation_count == 1
    assert (
        stats.minimum_price
        == stats.maximum_price
        == stats.average_price
        == Decimal(100)
    )
    assert stats.current_price == Decimal(100)
    assert stats.previous_price is None
    assert stats.historical_low_price is None
    assert stats.current_vs_previous is None
    assert stats.current_vs_historical_low is None


def test_itinerary_identity_and_filter(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    original = fare()
    data = original.model_dump()
    data["inbound"]["segments"][0]["flight_number"] = "EA 4"
    alternative = RoundTripItinerary.model_validate(data)
    first = repository.record_observation(watch, original, observed_at=START)
    other = repository.record_observation(watch, alternative, observed_at=START)
    changed_price = repository.record_observation(watch, fare("900"), observed_at=START)
    assert first.itinerary_id == changed_price.itinerary_id != other.itinerary_id
    assert repository.get_recent_observations(
        watch, itinerary_id=first.itinerary_id
    ) == [
        first,
        changed_price,
    ]
    assert repository.get_recent_observations(
        watch, itinerary_id=first.itinerary_id, limit=1
    ) == [changed_price]
    assert repository.get_lowest_price(
        watch, itinerary_id=other.itinerary_id
    ) == Decimal("1000")
    stats = repository.get_price_statistics(watch, itinerary_id=first.itinerary_id)
    assert stats.observation_count == 2
    assert stats.current_vs_previous == Decimal("-100")
    assert repository.get_recent_observations(watch, itinerary_id="' OR 1=1 --") == []


def test_timestamps_normalized_before_sorting(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    local = START.astimezone(timezone(timedelta(hours=-7)))
    first = repository.record_observation(watch, fare(), observed_at=START)
    second = repository.record_observation(watch, fare(), observed_at=local)
    earlier = repository.record_observation(
        watch, fare(), observed_at=START - timedelta(microseconds=1)
    )
    assert second.observed_at.tzinfo is UTC
    assert repository.get_recent_observations(watch) == [earlier, first, second]
    with closing(sqlite3.connect(repository.database_path)) as connection:
        times = connection.execute(
            "SELECT observed_at FROM fare_observations ORDER BY observation_id"
        ).fetchall()
    assert times[0] == times[1] == ("2026-09-01T12:00:00.000000+00:00",)


def test_default_timestamp_is_aware_utc(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    before = datetime.now(UTC)
    observation = repository.record_observation(watch, fare())
    assert before <= observation.observed_at <= datetime.now(UTC)
    assert observation.observed_at.tzinfo is UTC


@pytest.mark.parametrize("limit", [0, -1, True, 1.5, "2"])
def test_invalid_limits(
    repository: SQLiteFareHistory, watch: FareWatch, limit: object
) -> None:
    with pytest.raises(ValueError, match="limit"):
        repository.get_recent_observations(watch, limit=limit)


@pytest.mark.parametrize("price", ["-1", "NaN", "Infinity"])
def test_invalid_price_revalidated_before_write(
    repository: SQLiteFareHistory, watch: FareWatch, price: str
) -> None:
    invalid = fare().model_copy(update={"total_price": Decimal(price)})
    with pytest.raises(ValidationError):
        repository.record_observation(watch, invalid)
    assert repository.get_recent_observations(watch) == []


def test_invalid_inputs_do_not_create_watch(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    with pytest.raises(ValidationError, match="timezone"):
        repository.record_observation(
            watch, fare(), observed_at=START.replace(tzinfo=None)
        )
    with pytest.raises(ValueError, match="match the watch"):
        repository.record_observation(watch, fare(currency="EUR"))
    other_route = FareWatch.model_validate(watch.model_dump() | {"origin": "SFO"})
    with pytest.raises(ValueError, match="match the watch"):
        repository.record_observation(other_route, fare())
    with closing(sqlite3.connect(repository.database_path)) as connection:
        assert connection.execute("SELECT * FROM watches").fetchall() == []


@pytest.mark.parametrize("path", ["", " ", ":memory:"])
def test_invalid_database_path(path: str) -> None:
    with pytest.raises(ValueError, match="file-backed"):
        SQLiteFareHistory(path)


def test_only_normalized_data_is_stored(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    repository.record_observation(watch, fare())
    with closing(sqlite3.connect(repository.database_path)) as connection:
        context = connection.execute("SELECT context_json FROM watches").fetchone()[0]
        outbound, inbound = connection.execute(
            "SELECT outbound_json, inbound_json FROM fare_observations"
        ).fetchone()
        dump = "\n".join(connection.iterdump())
    assert json.loads(context) == watch.model_dump(mode="json")
    assert json.loads(outbound) == fare().outbound.model_dump(mode="json")
    assert json.loads(inbound) == fare().inbound.model_dump(mode="json")
    for forbidden in [
        "departure_token",
        "api_key",
        "aws",
        "test-key-not-a-real-credential",
    ]:
        assert forbidden not in dump.lower()


def test_mixed_currency_calculations_rejected(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    observation = repository.record_observation(watch, fare())
    with pytest.raises(ValueError, match="different currencies"):
        calculate_price_statistics([observation], currency="EUR")


def test_two_populated_watches_remain_isolated(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    other = FareWatch.model_validate(
        watch.model_dump() | {"outbound_date": "2026-12-02"}
    )
    first = repository.record_observation(watch, fare("900"), observed_at=START)
    second = repository.record_observation(other, fare("500"), observed_at=START)
    assert repository.get_recent_observations(watch) == [first]
    assert repository.get_recent_observations(other) == [second]
    assert repository.get_lowest_price(watch) == Decimal("900")
    assert repository.get_lowest_price(other) == Decimal("500")
    assert repository.get_price_statistics(watch).observation_count == 1
    assert repository.get_price_statistics(other).average_price == Decimal("500")
    with pytest.raises(ValueError, match="different watches"):
        calculate_price_statistics([first, second], currency="USD")


def test_record_transaction_rolls_back_watch_on_insert_failure(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    with closing(sqlite3.connect(repository.database_path)) as connection:
        connection.execute(
            """CREATE TRIGGER reject_insert BEFORE INSERT ON fare_observations
               BEGIN SELECT RAISE(ABORT, 'test failure'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="test failure"):
        repository.record_observation(watch, fare())
    with closing(sqlite3.connect(repository.database_path)) as connection:
        assert connection.execute("SELECT * FROM watches").fetchall() == []
    assert repository.get_recent_observations(watch) == []


def alternative_fare(price: str = "800") -> RoundTripItinerary:
    data = fare(price).model_dump()
    data["inbound"]["segments"][0]["flight_number"] = "EA 4"
    return RoundTripItinerary.model_validate(data)


def test_create_runs_normalizes_time_and_persists(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    local = START.astimezone(timezone(timedelta(hours=-7)))
    first = repository.create_run(watch, observed_at=local)
    second = repository.create_run(watch, observed_at=START)
    assert first.run_id < second.run_id
    assert first.watch_id == second.watch_id == watch.watch_id
    assert first.observed_at == second.observed_at == START
    assert first.observed_at.tzinfo is UTC
    with pytest.raises(ValidationError, match="frozen"):
        first.run_id = 99
    reopened = SQLiteFareHistory(repository.database_path)
    observation = reopened.record_observation(watch, fare(), run=first)
    assert observation.run_id == first.run_id
    assert observation.observed_at == START
    reopened.mark_run_completed(watch, run=first)
    assert reopened.get_prior_observations(watch, before_run=second) == [observation]


def test_runs_order_by_timestamp_then_id_independently_of_observation_writes(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    future = repository.create_run(watch, observed_at=START + timedelta(days=1))
    first = repository.create_run(watch, observed_at=START)
    tied = repository.create_run(watch, observed_at=START)
    earliest = repository.create_run(watch, observed_at=START - timedelta(days=1))
    future_fare = repository.record_observation(watch, fare("500"), run=future)
    tied_fare = repository.record_observation(watch, fare("700"), run=tied)
    first_fare = repository.record_observation(watch, fare("800"), run=first)
    earliest_fare = repository.record_observation(watch, fare("900"), run=earliest)
    for run in (future, first, tied, earliest):
        repository.mark_run_completed(watch, run=run)
    assert repository.get_prior_observations(watch, before_run=earliest) == []
    assert repository.get_prior_observations(watch, before_run=tied) == [
        earliest_fare,
        first_fare,
    ]
    assert repository.get_prior_observations(watch, before_run=future) == [
        earliest_fare,
        first_fare,
        tied_fare,
    ]
    assert future_fare not in repository.get_prior_observations(watch, before_run=first)


def test_prior_history_supports_both_scopes_and_excludes_entire_current_run(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    first = repository.create_run(watch, observed_at=START)
    second = repository.create_run(watch, observed_at=START + timedelta(days=1))
    current = repository.create_run(watch, observed_at=START + timedelta(days=2))
    a = repository.record_observation(watch, fare("1000"), run=first)
    b = repository.record_observation(watch, alternative_fare("800"), run=first)
    c = repository.record_observation(watch, fare("900"), run=second)
    repository.record_observation(watch, fare("1"), run=current)
    repository.record_observation(watch, alternative_fare("2"), run=current)
    repository.record_observation(watch, fare("0"), observed_at=START)
    for run in (first, second, current):
        repository.mark_run_completed(watch, run=run)
    assert a.run_id == b.run_id != c.run_id
    assert a.itinerary_id == c.itinerary_id != b.itinerary_id
    assert repository.get_prior_observations(watch, before_run=current) == [a, b, c]
    assert repository.get_prior_observations(
        watch, before_run=current, itinerary_id=a.itinerary_id
    ) == [a, c]
    stats = repository.get_prior_run_statistics(watch, before_run=current)
    assert stats.before_run == current
    assert stats.currency == "USD"
    assert stats.itinerary_id is None
    assert stats.observation_count == 3
    assert stats.run_count == 2
    assert stats.minimum_price == Decimal("800")
    assert stats.maximum_price == Decimal("1000")
    assert stats.average_price == Decimal("900")
    scoped = repository.get_prior_run_statistics(
        watch, before_run=current, itinerary_id=a.itinerary_id
    )
    assert scoped.observation_count == scoped.run_count == 2
    assert scoped.average_price == Decimal("950")
    assert "previous_price" not in stats.model_dump()


def test_watch_history_does_not_require_recurring_itineraries(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    prior = repository.create_run(watch, observed_at=START)
    current = repository.create_run(watch, observed_at=START)
    previous_fare = repository.record_observation(watch, fare(), run=prior)
    new_fare = repository.record_observation(watch, alternative_fare(), run=current)
    repository.mark_run_completed(watch, run=prior)
    assert repository.get_prior_observations(watch, before_run=current) == [
        previous_fare
    ]
    assert (
        repository.get_prior_observations(
            watch, before_run=current, itinerary_id=new_fare.itinerary_id
        )
        == []
    )
    assert (
        repository.get_prior_observations(
            watch, before_run=current, itinerary_id="' OR 1=1 --"
        )
        == []
    )


def test_empty_prior_history_and_empty_runs(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    repository.create_run(watch, observed_at=START)
    current = repository.create_run(watch, observed_at=START)
    stats = repository.get_prior_run_statistics(watch, before_run=current)
    assert stats.observation_count == stats.run_count == 0
    assert stats.minimum_price is stats.maximum_price is stats.average_price is None


@pytest.mark.parametrize("price", ["1000.00", "1000", "1E3"])
def test_same_run_duplicate_reuses_original_record(
    repository: SQLiteFareHistory, watch: FareWatch, price: str
) -> None:
    run = repository.create_run(watch, observed_at=START)
    first = repository.record_observation(watch, fare(), run=run)
    repeated = repository.record_observation(watch, fare(price), run=run)
    assert repeated == first
    assert repeated.total_price.as_tuple() == Decimal("1000.00").as_tuple()
    assert repository.get_recent_observations(watch) == [first]


def test_conflicting_duplicate_does_not_change_existing_fare(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    run = repository.create_run(watch, observed_at=START)
    first = repository.record_observation(watch, fare(), run=run)
    with pytest.raises(ValueError, match="Conflicting"):
        repository.record_observation(watch, fare("999"), run=run)
    with pytest.raises(ValueError, match="currency"):
        repository.record_observation(watch, fare(currency="EUR"), run=run)
    assert repository.get_recent_observations(watch) == [first]


def test_detail_conflict_under_same_identity_is_rejected(
    repository: SQLiteFareHistory, watch: FareWatch, monkeypatch: pytest.MonkeyPatch
) -> None:
    run = repository.create_run(watch, observed_at=START)
    first = repository.record_observation(watch, fare(), run=run)
    # Simulate an identity collision; normally changed details produce a new ID.
    monkeypatch.setattr(
        "faresentry.persistence.sqlite.itinerary_identity",
        lambda watch, itinerary: first.itinerary_id,
    )
    with pytest.raises(ValueError, match="Conflicting"):
        repository.record_observation(watch, alternative_fare("1000"), run=run)
    assert repository.get_recent_observations(watch) == [first]


def test_watch_run_mismatch_rejected_for_writes_and_queries(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    other = FareWatch.model_validate(
        watch.model_dump() | {"outbound_date": "2026-12-02"}
    )
    run = repository.create_run(watch, observed_at=START)
    with pytest.raises(ValueError, match="match the watch"):
        repository.record_observation(other, fare(), run=run)
    with pytest.raises(ValueError, match="match the watch"):
        repository.get_prior_observations(other, before_run=run)
    with pytest.raises(ValueError, match="match the watch"):
        repository.get_prior_run_statistics(other, before_run=run)
    with pytest.raises(ValueError, match="match the watch"):
        repository.mark_run_completed(other, run=run)
    with closing(sqlite3.connect(repository.database_path)) as connection:
        assert connection.execute("SELECT COUNT(*) FROM watches").fetchone()[0] == 1


@pytest.mark.parametrize(
    "change", [{"run_id": 999}, {"observed_at": START + timedelta(days=1)}]
)
def test_unpersisted_or_altered_run_rejected(
    repository: SQLiteFareHistory, watch: FareWatch, change: dict[str, object]
) -> None:
    run = repository.create_run(watch, observed_at=START).model_copy(update=change)
    with pytest.raises(ValueError, match="persisted run"):
        repository.record_observation(watch, fare(), run=run)
    with pytest.raises(ValueError, match="persisted run"):
        repository.get_prior_observations(watch, before_run=run)
    with pytest.raises(ValueError, match="persisted run"):
        repository.mark_run_completed(watch, run=run)


def test_naive_run_and_inconsistent_observation_timestamp_rejected(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    with pytest.raises(ValidationError, match="timezone"):
        repository.create_run(watch, observed_at=START.replace(tzinfo=None))
    with closing(sqlite3.connect(repository.database_path)) as connection:
        assert connection.execute("SELECT * FROM watches").fetchall() == []
    run = repository.create_run(watch, observed_at=START)
    with pytest.raises(ValueError, match="timestamp"):
        repository.record_observation(
            watch, fare(), run=run, observed_at=START + timedelta(seconds=1)
        )
    with pytest.raises(ValidationError, match="timezone"):
        repository.record_observation(
            watch, fare(), run=run, observed_at=START.replace(tzinfo=None)
        )
    local = START.astimezone(timezone(timedelta(hours=3)))
    assert (
        repository.record_observation(
            watch, fare(), run=run, observed_at=local
        ).observed_at
        == START
    )


def test_run_creation_rolls_back_watch_on_failure(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    with closing(sqlite3.connect(repository.database_path)) as connection:
        connection.execute(
            """CREATE TRIGGER reject_run BEFORE INSERT ON monitoring_runs
               BEGIN SELECT RAISE(ABORT, 'run failure'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="run failure"):
        repository.create_run(watch, observed_at=START)
    with closing(sqlite3.connect(repository.database_path)) as connection:
        assert connection.execute("SELECT * FROM watches").fetchall() == []
        assert connection.execute("SELECT * FROM monitoring_runs").fetchall() == []


def test_run_observation_insert_failure_preserves_run_and_previous_records(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    run = repository.create_run(watch, observed_at=START)
    first = repository.record_observation(watch, fare(), run=run)
    with closing(sqlite3.connect(repository.database_path)) as connection:
        connection.execute(
            """CREATE TRIGGER reject_fare BEFORE INSERT ON fare_observations
               BEGIN SELECT RAISE(ABORT, 'fare failure'); END"""
        )
    with pytest.raises(sqlite3.IntegrityError, match="fare failure"):
        repository.record_observation(watch, alternative_fare(), run=run)
    assert repository.get_recent_observations(watch) == [first]
    assert repository.get_prior_observations(watch, before_run=run) == []


@pytest.mark.parametrize("currency", ["USD", "EUR", "JPY", "KWD"])
def test_prior_run_decimal_statistics_preserve_precision_and_currency(
    repository: SQLiteFareHistory, watch: FareWatch, currency: str
) -> None:
    watch = FareWatch.model_validate(watch.model_dump() | {"currency": currency})
    first = repository.create_run(watch, observed_at=START)
    second = repository.create_run(watch, observed_at=START)
    current = repository.create_run(watch, observed_at=START)
    prices = ["12345678901234567890.123456789", "12345678901234567890.123456791"]
    for run, price in zip((first, second), prices):
        repository.record_observation(watch, fare(price, currency), run=run)
        repository.mark_run_completed(watch, run=run)
    with localcontext() as context:
        context.prec = 3
        context.rounding = ROUND_UP
        stats = repository.get_prior_run_statistics(watch, before_run=current)
    assert stats.currency == currency
    assert stats.average_price == Decimal("12345678901234567890.123456790")
    restored = repository.get_prior_observations(watch, before_run=current)
    assert [item.total_price.as_tuple() for item in restored] == [
        Decimal(p).as_tuple() for p in prices
    ]
    assert stats == repository.get_prior_run_statistics(watch, before_run=current)


def test_run_inputs_unchanged(repository: SQLiteFareHistory, watch: FareWatch) -> None:
    itinerary = fare()
    run = repository.create_run(watch, observed_at=START)
    before = [item.model_dump_json() for item in (watch, itinerary, run)]
    repository.record_observation(watch, itinerary, run=run)
    repository.get_prior_run_statistics(watch, before_run=run)
    assert before == [item.model_dump_json() for item in (watch, itinerary, run)]


@pytest.fixture
def version_one_database(tmp_path: Path, watch: FareWatch) -> Path:
    """Build the original schema directly, independently of migration code."""
    path = tmp_path / "version-one.sqlite3"
    with closing(sqlite3.connect(path)) as connection, connection:
        connection.execute(
            """CREATE TABLE watches (
                watch_id TEXT PRIMARY KEY, context_json TEXT NOT NULL,
                currency TEXT NOT NULL, UNIQUE (watch_id, currency))"""
        )
        connection.execute(
            """CREATE TABLE fare_observations (
                observation_id INTEGER PRIMARY KEY, watch_id TEXT NOT NULL,
                observed_at TEXT NOT NULL, total_price TEXT NOT NULL,
                currency TEXT NOT NULL, itinerary_id TEXT NOT NULL,
                outbound_json TEXT NOT NULL, inbound_json TEXT NOT NULL,
                FOREIGN KEY (watch_id, currency)
                    REFERENCES watches (watch_id, currency))"""
        )
        connection.execute(
            """CREATE INDEX observations_by_watch_time
               ON fare_observations (watch_id, observed_at, observation_id)"""
        )
        connection.execute(
            """CREATE INDEX observations_by_itinerary_time ON fare_observations
               (watch_id, itinerary_id, observed_at, observation_id)"""
        )
        connection.execute(
            "INSERT INTO watches VALUES (?, ?, ?)",
            (watch.watch_id, watch.model_dump_json(), watch.currency),
        )
        for index, price in enumerate(("0.1000", "0.20"), start=1):
            itinerary = fare(price)
            connection.execute(
                "INSERT INTO fare_observations VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    index,
                    watch.watch_id,
                    START.isoformat(timespec="microseconds"),
                    price,
                    watch.currency,
                    itinerary_identity(watch, itinerary),
                    itinerary.outbound.model_dump_json(),
                    itinerary.inbound.model_dump_json(),
                ),
            )
        connection.execute("PRAGMA user_version = 1")
    return path


def test_version_one_migrates_without_inventing_runs(
    version_one_database: Path, watch: FareWatch
) -> None:
    with closing(sqlite3.connect(version_one_database)) as connection:
        original = connection.execute("SELECT * FROM fare_observations").fetchall()
    repository = SQLiteFareHistory(version_one_database)
    SQLiteFareHistory(version_one_database)
    with closing(sqlite3.connect(version_one_database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT * FROM monitoring_runs").fetchall() == []
        rows = connection.execute("SELECT * FROM fare_observations").fetchall()
        assert rows == [(*row, None) for row in original]
    old = repository.get_recent_observations(watch)
    assert all(item.run_id is None for item in old)
    assert old[0].total_price.as_tuple() == Decimal("0.1000").as_tuple()
    assert repository.get_price_statistics(watch).observation_count == 2
    prior = repository.create_run(watch, observed_at=START + timedelta(days=1))
    current = repository.create_run(watch, observed_at=START + timedelta(days=2))
    assert (
        repository.get_prior_run_statistics(watch, before_run=current).observation_count
        == 0
    )
    known = repository.record_observation(watch, fare("100"), run=prior)
    repository.mark_run_completed(watch, run=prior)
    assert repository.get_prior_observations(watch, before_run=current) == [known]
    assert repository.get_prior_observations(
        watch, before_run=current, itinerary_id=known.itinerary_id
    ) == [known]
    stats = repository.get_prior_run_statistics(watch, before_run=current)
    assert (
        stats.minimum_price
        == stats.maximum_price
        == stats.average_price
        == Decimal("100")
    )
    assert stats.run_count == stats.observation_count == 1


def test_migration_failure_rolls_back_schema_and_version(
    version_one_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(sqlite3.connect(version_one_database)) as connection:
        before = list(connection.iterdump())
    migrate = SQLiteFareHistory._migrate_v1_to_v2

    def failing_migration(connection: sqlite3.Connection) -> None:
        migrate(connection)
        raise RuntimeError("migration failure")

    with monkeypatch.context() as patch:
        patch.setattr(
            SQLiteFareHistory, "_migrate_v1_to_v2", staticmethod(failing_migration)
        )
        with pytest.raises(RuntimeError, match="migration failure"):
            SQLiteFareHistory(version_one_database)
    with closing(sqlite3.connect(version_one_database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert list(connection.iterdump()) == before
    SQLiteFareHistory(version_one_database)


def test_database_enforces_run_membership_and_uniqueness(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    run = repository.create_run(watch, observed_at=START)
    first = repository.record_observation(watch, fare(), run=run)
    other = FareWatch.model_validate(
        watch.model_dump() | {"outbound_date": "2026-12-02"}
    )
    other_run = repository.create_run(other, observed_at=START)
    with closing(sqlite3.connect(repository.database_path)) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"), connection:
            connection.execute(
                """INSERT INTO fare_observations (
                    watch_id, observed_at, total_price, currency, itinerary_id,
                    outbound_json, inbound_json, run_id)
                    SELECT watch_id, observed_at, total_price, currency, itinerary_id,
                    outbound_json, inbound_json, run_id FROM fare_observations"""
            )
        with pytest.raises(sqlite3.IntegrityError, match="match run"), connection:
            connection.execute(
                "UPDATE fare_observations SET run_id = ? WHERE observation_id = ?",
                (other_run.run_id, first.observation_id),
            )
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"), connection:
            connection.execute(
                "DELETE FROM monitoring_runs WHERE run_id = ?", (run.run_id,)
            )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_concurrent_duplicate_writers_reuse_one_observation(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    run = repository.create_run(watch, observed_at=START)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(repository.record_observation, watch, fare(), run=run)
            for _ in range(2)
        ]
        results = [future.result() for future in futures]
    assert results[0] == results[1]
    assert repository.get_recent_observations(watch) == [results[0]]


def test_completion_controls_both_prior_scopes_and_statistics(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    prior = repository.create_run(watch, observed_at=START)
    current = repository.create_run(watch, observed_at=START)
    first = repository.record_observation(watch, fare("1000"), run=prior)
    second = repository.record_observation(watch, alternative_fare("800"), run=prior)
    repository.record_observation(watch, fare("1"), run=current)
    with closing(sqlite3.connect(repository.database_path)) as connection:
        assert connection.execute(
            "SELECT completed FROM monitoring_runs"
        ).fetchall() == [(0,), (0,)]
    for itinerary_id in (None, first.itinerary_id):
        assert (
            repository.get_prior_observations(
                watch, before_run=current, itinerary_id=itinerary_id
            )
            == []
        )
        assert (
            repository.get_prior_run_statistics(
                watch, before_run=current, itinerary_id=itinerary_id
            ).observation_count
            == 0
        )

    repository.mark_run_completed(watch, run=prior)
    repository.mark_run_completed(watch, run=prior)  # Idempotent.
    reopened = SQLiteFareHistory(repository.database_path)
    assert reopened.get_prior_observations(watch, before_run=current) == [first, second]
    assert reopened.get_prior_observations(
        watch, before_run=current, itinerary_id=first.itinerary_id
    ) == [first]
    stats = reopened.get_prior_run_statistics(watch, before_run=current)
    assert stats.observation_count == 2
    assert stats.run_count == 1
    assert stats.minimum_price == Decimal("800")
    assert stats.maximum_price == Decimal("1000")
    assert stats.average_price == Decimal("900")
    scoped = reopened.get_prior_run_statistics(
        watch, before_run=current, itinerary_id=first.itinerary_id
    )
    assert scoped.observation_count == scoped.run_count == 1
    assert (
        scoped.minimum_price
        == scoped.maximum_price
        == scoped.average_price
        == Decimal("1000")
    )
    assert len(reopened.get_recent_observations(watch)) == 3
    assert reopened.get_lowest_price(watch) == Decimal("1")
    with closing(sqlite3.connect(repository.database_path)) as connection:
        assert connection.execute(
            "SELECT completed FROM monitoring_runs ORDER BY run_id"
        ).fetchall() == [(1,), (0,)]


@pytest.fixture
def version_two_database(version_one_database: Path) -> Path:
    # Use the unchanged v1->v2 builder; the v3 migration under test is not involved.
    with closing(sqlite3.connect(version_one_database)) as connection, connection:
        SQLiteFareHistory._migrate_v1_to_v2(connection)
        watch_id = connection.execute("SELECT watch_id FROM watches").fetchone()[0]
        for run_id in (1, 2):
            connection.execute(
                "INSERT INTO monitoring_runs VALUES (?, ?, ?)",
                (run_id, watch_id, START.isoformat(timespec="microseconds")),
            )
            connection.execute(
                "UPDATE fare_observations SET run_id = ? WHERE observation_id = ?",
                (run_id, run_id),
            )
    return version_one_database


def test_v2_migration_preserves_history_and_defaults_new_runs_to_incomplete(
    version_two_database: Path, watch: FareWatch
) -> None:
    with closing(sqlite3.connect(version_two_database)) as connection:
        original = connection.execute("SELECT * FROM fare_observations").fetchall()
    repository = SQLiteFareHistory(version_two_database)
    SQLiteFareHistory(version_two_database)
    with closing(sqlite3.connect(version_two_database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert (
            connection.execute("SELECT * FROM fare_observations").fetchall() == original
        )
        assert connection.execute(
            "SELECT completed FROM monitoring_runs"
        ).fetchall() == [(1,), (1,)]
    incomplete = repository.create_run(watch, observed_at=START)
    repository.record_observation(watch, fare("0.01"), run=incomplete)
    current = repository.create_run(watch, observed_at=START)
    prior = repository.get_prior_observations(watch, before_run=current)
    assert [item.run_id for item in prior] == [1, 2]
    assert [item.total_price for item in prior] == [Decimal("0.1000"), Decimal("0.20")]
    repository.mark_run_completed(watch, run=incomplete)
    assert [
        item.run_id
        for item in repository.get_prior_observations(watch, before_run=current)
    ] == [1, 2, incomplete.run_id]


def test_v3_migration_failure_rolls_back_marker_and_version(
    version_two_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(sqlite3.connect(version_two_database)) as connection:
        before = list(connection.iterdump())
    migrate = SQLiteFareHistory._migrate_v2_to_v3

    def failing_migration(connection: sqlite3.Connection) -> None:
        migrate(connection)
        raise RuntimeError("migration failure")

    with monkeypatch.context() as patch:
        patch.setattr(
            SQLiteFareHistory, "_migrate_v2_to_v3", staticmethod(failing_migration)
        )
        with pytest.raises(RuntimeError, match="migration failure"):
            SQLiteFareHistory(version_two_database)
    with closing(sqlite3.connect(version_two_database)) as connection:
        assert list(connection.iterdump()) == before
    SQLiteFareHistory(version_two_database)


@pytest.mark.parametrize("completed", [None, -1, 2])
def test_database_rejects_invalid_completion_marker(
    repository: SQLiteFareHistory, watch: FareWatch, completed: int | None
) -> None:
    run = repository.create_run(watch, observed_at=START)
    with closing(sqlite3.connect(repository.database_path)) as connection:
        with pytest.raises(sqlite3.IntegrityError), connection:
            connection.execute(
                "UPDATE monitoring_runs SET completed = ? WHERE run_id = ?",
                (completed, run.run_id),
            )
        assert connection.execute(
            "SELECT completed FROM monitoring_runs"
        ).fetchone() == (0,)


def test_latest_run_uses_attempt_order_and_watch_scope(
    repository: SQLiteFareHistory, watch: FareWatch
) -> None:
    assert repository.get_latest_run(watch) is None
    repository.record_observation(watch, fare(), observed_at=START)
    assert repository.get_latest_run(watch) is None  # Legacy rows are not attempts.
    first = repository.create_run(watch, observed_at=START)
    repository.mark_run_completed(watch, run=first)
    assert repository.get_latest_run(watch) == first
    tied = repository.create_run(watch, observed_at=START)
    assert repository.get_latest_run(watch) == tied  # Includes incomplete empty runs.
    future = repository.create_run(watch, observed_at=START + timedelta(days=1))
    repository.create_run(watch, observed_at=START - timedelta(days=1))
    other = FareWatch.model_validate(
        watch.model_dump() | {"outbound_date": "2026-12-02"}
    )
    repository.create_run(other, observed_at=START + timedelta(days=2))
    reopened = SQLiteFareHistory(repository.database_path)
    assert reopened.get_latest_run(watch) == future
    assert reopened.get_prior_observations(watch, before_run=future) == []


@pytest.fixture
def version_three_database(version_two_database: Path) -> Path:
    with closing(sqlite3.connect(version_two_database)) as connection, connection:
        SQLiteFareHistory._migrate_v2_to_v3(connection)
        connection.execute("UPDATE monitoring_runs SET completed = 0 WHERE run_id = 2")
    return version_two_database


def test_v4_migration_preserves_all_history_and_completion(
    version_three_database: Path,
) -> None:
    with closing(sqlite3.connect(version_three_database)) as connection:
        original = {
            table: connection.execute(f"SELECT * FROM {table}").fetchall()
            for table in ("watches", "monitoring_runs", "fare_observations")
        }
    SQLiteFareHistory(version_three_database)
    SQLiteFareHistory(version_three_database)
    with closing(sqlite3.connect(version_three_database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 4
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        for table, rows in original.items():
            assert connection.execute(f"SELECT * FROM {table}").fetchall() == rows
        assert (
            connection.execute("SELECT * FROM notification_deliveries").fetchall() == []
        )
        assert connection.execute(
            "SELECT completed FROM monitoring_runs ORDER BY run_id"
        ).fetchall() == [(1,), (0,)]


def test_v4_migration_rolls_back_table_and_version(
    version_three_database: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with closing(sqlite3.connect(version_three_database)) as connection:
        original = list(connection.iterdump())
    migrate = SQLiteFareHistory._migrate_v3_to_v4

    def fail(connection: sqlite3.Connection) -> None:
        migrate(connection)
        raise RuntimeError("migration failure")

    with monkeypatch.context() as patch:
        patch.setattr(SQLiteFareHistory, "_migrate_v3_to_v4", staticmethod(fail))
        with pytest.raises(RuntimeError, match="migration failure"):
            SQLiteFareHistory(version_three_database)
    with closing(sqlite3.connect(version_three_database)) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3
        assert list(connection.iterdump()) == original
    SQLiteFareHistory(version_three_database)
