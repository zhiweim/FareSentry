import json
import sqlite3
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
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        } == {"watches", "fare_observations"}
        assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"


@pytest.mark.parametrize("version", [2, 99])
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
