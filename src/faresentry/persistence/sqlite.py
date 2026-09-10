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
    MonitoringRun,
    PriceStatistics,
    PriorRunPriceStatistics,
    calculate_price_statistics,
    itinerary_identity,
)
from faresentry.models import FlightItinerary, RoundTripItinerary

SCHEMA_VERSION = 2


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
            if version == 1:
                self._migrate_v1_to_v2(connection)
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
            self._migrate_v1_to_v2(connection)

    @staticmethod
    def _migrate_v1_to_v2(connection: sqlite3.Connection) -> None:
        """Called inside the initialization transaction; never infer old runs."""
        connection.execute(
            """CREATE TABLE monitoring_runs (
                run_id INTEGER PRIMARY KEY,
                watch_id TEXT NOT NULL REFERENCES watches (watch_id),
                observed_at TEXT NOT NULL
            )"""
        )
        connection.execute(
            """CREATE INDEX runs_by_watch_time
               ON monitoring_runs (watch_id, observed_at, run_id)"""
        )
        connection.execute(
            """ALTER TABLE fare_observations ADD COLUMN run_id INTEGER
               REFERENCES monitoring_runs (run_id)"""
        )
        connection.execute(
            """CREATE UNIQUE INDEX observation_per_run_itinerary
               ON fare_observations (run_id, itinerary_id)
               WHERE run_id IS NOT NULL"""
        )
        # ADD COLUMN cannot declare a composite foreign key. These triggers
        # enforce run/watch membership without rebuilding the legacy table.
        for event, suffix in (("INSERT", "insert"), ("UPDATE", "update")):
            connection.execute(
                f"""CREATE TRIGGER observation_run_watch_{suffix}
                    BEFORE {event} ON fare_observations
                    WHEN NEW.run_id IS NOT NULL AND NOT EXISTS (
                        SELECT 1 FROM monitoring_runs
                        WHERE run_id = NEW.run_id AND watch_id = NEW.watch_id
                    )
                    BEGIN
                        SELECT RAISE(ABORT, 'Observation watch must match run');
                    END"""
            )
        connection.execute("PRAGMA user_version = 2")

    @staticmethod
    def _record_watch(connection: sqlite3.Connection, watch: FareWatch) -> None:
        connection.execute(
            """INSERT INTO watches (watch_id, context_json, currency)
               VALUES (?, ?, ?) ON CONFLICT(watch_id) DO NOTHING""",
            (watch.watch_id, watch.model_dump_json(), watch.currency),
        )

    def create_run(self, watch: FareWatch, *, observed_at: datetime) -> MonitoringRun:
        """Create one distinct check; callers reuse its result for all its fares.

        observed_at is required and must be timezone-aware. Ordering uses its
        UTC value, then database-local run_id, independently of observation writes.
        """
        watch = FareWatch.model_validate(watch.model_dump())
        run = MonitoringRun(run_id=1, watch_id=watch.watch_id, observed_at=observed_at)
        with self._connect() as connection:
            self._record_watch(connection, watch)
            cursor = connection.execute(
                "INSERT INTO monitoring_runs (watch_id, observed_at) VALUES (?, ?)",
                (run.watch_id, run.observed_at.isoformat(timespec="microseconds")),
            )
            return run.model_copy(update={"run_id": cursor.lastrowid})

    @staticmethod
    def _validate_run(
        connection: sqlite3.Connection, watch: FareWatch, run: MonitoringRun
    ) -> MonitoringRun:
        run = MonitoringRun.model_validate(run.model_dump())
        if run.watch_id != watch.watch_id:
            raise ValueError("Run must match the watch")
        row = connection.execute(
            "SELECT * FROM monitoring_runs WHERE run_id = ?", (run.run_id,)
        ).fetchone()
        if row is None or MonitoringRun.model_validate(dict(row)) != run:
            raise ValueError("Run must match a persisted run in this repository")
        return run

    def record_observation(
        self,
        watch: FareWatch,
        itinerary: RoundTripItinerary,
        *,
        observed_at: datetime | None = None,
        run: MonitoringRun | None = None,
    ) -> FareObservation:
        """Record one complete fare, optionally belonging to an explicit run.

        With a run, its timestamp is used; an explicit timestamp must equal it.
        Equal fares/details for the same run/itinerary reuse the stored record
        (Decimal scale differences count as equal); conflicts raise ValueError.
        Without a run, append ungrouped records excluded from run-aware history.
        Ungrouped timestamps default to now; naive timestamps are rejected.
        Validates route/currency against the watch. The caller supplies dates via
        the watch because current normalized itineraries do not carry dates.
        """
        watch = FareWatch.model_validate(watch.model_dump())
        itinerary = RoundTripItinerary.model_validate(itinerary.model_dump())
        if run is not None:
            run = MonitoringRun.model_validate(run.model_dump())
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
            run_id=run.run_id if run is not None else None,
            watch_id=watch.watch_id,
            itinerary_id=itinerary_identity(watch, itinerary),
            observed_at=(
                observed_at
                if observed_at is not None
                else run.observed_at
                if run is not None
                else datetime.now(UTC)
            ),
        )
        with self._connect() as connection:
            # Serialize duplicate lookup and insertion, including across writers.
            connection.execute("BEGIN IMMEDIATE")
            if run is not None:
                self._validate_run(connection, watch, run)
                if observation.observed_at != run.observed_at:
                    raise ValueError("Observation timestamp must match the run")
                existing = connection.execute(
                    """SELECT * FROM fare_observations
                       WHERE run_id = ? AND itinerary_id = ?""",
                    (run.run_id, observation.itinerary_id),
                ).fetchone()
                if existing is not None:
                    stored = self._observation(existing)
                    if stored.model_dump(exclude={"observation_id"}) != (
                        observation.model_dump(exclude={"observation_id"})
                    ):
                        raise ValueError(
                            "Conflicting observation for run and itinerary"
                        )
                    return stored
            self._record_watch(connection, watch)
            cursor = connection.execute(
                """INSERT INTO fare_observations (
                    watch_id, observed_at, total_price, currency, itinerary_id,
                    outbound_json, inbound_json, run_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    observation.watch_id,
                    observation.observed_at.isoformat(timespec="microseconds"),
                    str(observation.total_price),
                    observation.currency,
                    observation.itinerary_id,
                    observation.outbound.model_dump_json(),
                    observation.inbound.model_dump_json(),
                    observation.run_id,
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
            run_id=row["run_id"],
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

    def get_prior_observations(
        self,
        watch: FareWatch,
        *,
        before_run: MonitoringRun,
        itinerary_id: str | None = None,
    ) -> list[FareObservation]:
        """Known-run history strictly before (run timestamp, run ID).

        Excludes the entire current run, later runs, and all null-run records.
        Returns oldest runs first, then observation ID within a run. Omitting
        itinerary_id includes every itinerary in the watch, even if none recur.
        Records within a run are alternatives, not temporal price changes.
        """
        watch = FareWatch.model_validate(watch.model_dump())
        with self._connect() as connection:
            connection.execute("BEGIN")
            run = self._validate_run(connection, watch, before_run)
            query = """SELECT o.* FROM fare_observations AS o
                       JOIN monitoring_runs AS r ON o.run_id = r.run_id
                           AND o.watch_id = r.watch_id
                       WHERE r.watch_id = ? AND (r.observed_at, r.run_id) < (?, ?)"""
            parameters: list[str | int] = [
                watch.watch_id,
                run.observed_at.isoformat(timespec="microseconds"),
                run.run_id,
            ]
            if itinerary_id is not None:
                query += " AND o.itinerary_id = ?"
                parameters.append(itinerary_id)
            query += " ORDER BY r.observed_at, r.run_id, o.observation_id"
            rows = connection.execute(query, parameters).fetchall()
        return [self._observation(row) for row in rows]

    def get_prior_run_statistics(
        self,
        watch: FareWatch,
        *,
        before_run: MonitoringRun,
        itinerary_id: str | None = None,
    ) -> PriorRunPriceStatistics:
        """Observation-weighted aggregates from a single prior-history snapshot.

        Reuses exact Decimal sums and half-even average rounding from existing
        statistics. No preceding candidate is presented as a previous-run fare.
        """
        observations = self.get_prior_observations(
            watch, before_run=before_run, itinerary_id=itinerary_id
        )
        stats = calculate_price_statistics(observations, currency=watch.currency)
        return PriorRunPriceStatistics(
            before_run=MonitoringRun.model_validate(before_run.model_dump()),
            currency=stats.currency,
            itinerary_id=itinerary_id,
            observation_count=stats.observation_count,
            run_count=len({item.run_id for item in observations}),
            minimum_price=stats.minimum_price,
            maximum_price=stats.maximum_price,
            average_price=stats.average_price,
        )
