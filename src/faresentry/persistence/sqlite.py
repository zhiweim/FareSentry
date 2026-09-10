"""Versioned, file-backed SQLite fare history using only the standard library."""

import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from faresentry.history import (
    FareObservation,
    FareWatch,
    PriceStatistics,
    calculate_price_statistics,
    itinerary_identity,
)
from faresentry.models import FlightItinerary, RoundTripItinerary

SCHEMA_VERSION = 1


class SQLiteFareHistory:
    """Append-only observation repository. Construction initializes the schema.

    The path is required, its parent must exist, and connections are closed after
    every operation. Use a dedicated file (not :memory:). Version 0 is initialized
    only when empty; unsupported versions are rejected without modifying data.
    Future schema migrations must run transactionally before bumping user_version.
    """

    def __init__(self, database_path: str | Path) -> None:
        if not str(database_path).strip() or str(database_path) == ":memory:":
            raise ValueError("Provide a nonempty file-backed database path")
        self.database_path = Path(database_path).resolve()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            with connection:
                yield connection
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == SCHEMA_VERSION:
                return
            if version != 0:
                raise ValueError(f"Unsupported fare-history schema version: {version}")
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
            ).fetchone():
                raise ValueError("Cannot initialize a nonempty unversioned database")
            connection.execute(
                """CREATE TABLE watches (
                    watch_id TEXT PRIMARY KEY,
                    context_json TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    UNIQUE (watch_id, currency)
                )"""
            )
            connection.execute(
                """CREATE TABLE fare_observations (
                    observation_id INTEGER PRIMARY KEY,
                    watch_id TEXT NOT NULL,
                    observed_at TEXT NOT NULL,
                    total_price TEXT NOT NULL,
                    currency TEXT NOT NULL,
                    itinerary_id TEXT NOT NULL,
                    outbound_json TEXT NOT NULL,
                    inbound_json TEXT NOT NULL,
                    FOREIGN KEY (watch_id, currency)
                        REFERENCES watches (watch_id, currency)
                )"""
            )
            connection.execute(
                """CREATE INDEX observations_by_watch_time
                   ON fare_observations (watch_id, observed_at, observation_id)"""
            )
            connection.execute(
                """CREATE INDEX observations_by_itinerary_time ON fare_observations
                   (watch_id, itinerary_id, observed_at, observation_id)"""
            )
            connection.execute("PRAGMA user_version = 1")

    def record_observation(
        self,
        watch: FareWatch,
        itinerary: RoundTripItinerary,
        *,
        observed_at: datetime | None = None,
    ) -> FareObservation:
        """Record one complete fare; repeated calls intentionally append records.

        Timestamp defaults to now in UTC; explicit naive timestamps are rejected.
        Validates route/currency against the watch. The caller supplies dates via
        the watch because current normalized itineraries do not carry dates.
        """
        watch = FareWatch.model_validate(watch.model_dump())
        itinerary = RoundTripItinerary.model_validate(itinerary.model_dump())
        if (
            itinerary.outbound.origin != watch.origin
            or itinerary.outbound.destination != watch.destination
            or itinerary.currency != watch.currency
        ):
            raise ValueError("Itinerary route and currency must match the watch")
        # Validate everything before any write. The temporary ID is replaced by
        # SQLite's insertion ID; it is not persisted or used for ordering.
        observation = FareObservation(
            **itinerary.model_dump(),
            observation_id=1,
            watch_id=watch.watch_id,
            itinerary_id=itinerary_identity(watch, itinerary),
            observed_at=observed_at if observed_at is not None else datetime.now(UTC),
        )
        with self._connect() as connection:
            connection.execute(
                """INSERT INTO watches (watch_id, context_json, currency)
                   VALUES (?, ?, ?) ON CONFLICT(watch_id) DO NOTHING""",
                (watch.watch_id, watch.model_dump_json(), watch.currency),
            )
            cursor = connection.execute(
                """INSERT INTO fare_observations (
                    watch_id, observed_at, total_price, currency, itinerary_id,
                    outbound_json, inbound_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    observation.watch_id,
                    observation.observed_at.isoformat(timespec="microseconds"),
                    str(observation.total_price),
                    observation.currency,
                    observation.itinerary_id,
                    observation.outbound.model_dump_json(),
                    observation.inbound.model_dump_json(),
                ),
            )
            return observation.model_copy(update={"observation_id": cursor.lastrowid})

    def get_recent_observations(
        self,
        watch: FareWatch,
        *,
        limit: int | None = None,
        itinerary_id: str | None = None,
    ) -> list[FareObservation]:
        """Newest N records returned oldest first; None returns all history.

        Ties use insertion ID. Optional itinerary_id scopes to one normalized
        itinerary; without it, all candidates count, including at the same time.
        """
        if limit is not None and (type(limit) is not int or limit <= 0):
            raise ValueError("limit must be a positive integer or None")
        watch = FareWatch.model_validate(watch.model_dump())
        query = "SELECT * FROM fare_observations WHERE watch_id = ?"
        parameters: list[str | int] = [watch.watch_id]
        if itinerary_id is not None:
            query += " AND itinerary_id = ?"
            parameters.append(itinerary_id)
        query += " ORDER BY observed_at DESC, observation_id DESC"
        if limit is not None:
            query += " LIMIT ?"
            parameters.append(limit)
        with self._connect() as connection:
            rows = connection.execute(query, parameters).fetchall()
        return [self._observation(row) for row in reversed(rows)]

    @staticmethod
    def _observation(row: sqlite3.Row) -> FareObservation:
        return FareObservation(
            observation_id=row["observation_id"],
            watch_id=row["watch_id"],
            observed_at=row["observed_at"],
            total_price=Decimal(row["total_price"]),
            currency=row["currency"],
            itinerary_id=row["itinerary_id"],
            outbound=FlightItinerary.model_validate_json(row["outbound_json"]),
            inbound=FlightItinerary.model_validate_json(row["inbound_json"]),
        )

    def get_lowest_price(
        self, watch: FareWatch, *, itinerary_id: str | None = None
    ) -> Decimal | None:
        """Lowest numeric price in scope, or None; never lexical SQL MIN(TEXT)."""
        observations = self.get_recent_observations(watch, itinerary_id=itinerary_id)
        return min((item.total_price for item in observations), default=None)

    def get_price_statistics(
        self, watch: FareWatch, *, itinerary_id: str | None = None
    ) -> PriceStatistics:
        """Statistics over all records in scope, read in a single DB snapshot."""
        return calculate_price_statistics(
            self.get_recent_observations(watch, itinerary_id=itinerary_id),
            currency=watch.currency,
        )
