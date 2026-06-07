"""
TimescaleDB writer for the cooling tower simulator.

Writes simulation output to the `runs` and `tower_readings` tables defined
in db/schema.sql.  Uses psycopg3's binary COPY protocol for bulk inserts —
significantly faster than executemany for the large DataFrames a multi-day
simulation produces.

Connection is configured via the DATABASE_URL environment variable
(or a .env file in the project root).

Typical usage::

    from pipeline.timescale_writer import TimescaleWriter
    from simulator.runner import RunConfig, run

    config = RunConfig(start_date="2023-01-01", end_date="2023-01-31")
    df = run(config)

    with TimescaleWriter.from_env() as writer:
        run_id = writer.write_run(
            df=df,
            config=config,
            label="baseline_jan_2023",
            notes="Clean tower, no faults injected",
        )
        print(f"Written {len(df)} rows as run_id={run_id}")
"""

from __future__ import annotations

import os
from datetime import date
from typing import Iterator

import pandas as pd
import psycopg
from dotenv import load_dotenv

from simulator.runner import RunConfig


# Column order must match INSERT in _insert_run and COPY in _copy_readings
_READINGS_COLUMNS = [
    "time",
    "run_id",
    "t_hot_in",
    "water_flow_m3hr",
    "t_amb",
    "t_wb",
    "fan_speed_pct",
    "cond_makeup",
    "t_cold_out",
    "t_approach",
    "t_range",
    "evaporation_m3hr",
    "blowdown_m3hr",
    "makeup_m3hr",
    "coc",
    "cond_basin",
    "ntu",
    "effectiveness",
    "fouling_factor",
    "active_faults",
]


class TimescaleWriter:
    """
    Manages a single psycopg connection and exposes write operations.

    Use as a context manager to ensure the connection is closed cleanly::

        with TimescaleWriter.from_env() as writer:
            run_id = writer.write_run(df, config)
    """

    def __init__(self, connection: psycopg.Connection) -> None:
        self._conn = connection

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls, env_file: str = ".env") -> "TimescaleWriter":
        """
        Create a writer using DATABASE_URL from the environment or a .env file.

        DATABASE_URL format:
            postgresql://user:password@host:port/dbname
        """
        load_dotenv(env_file, override=False)
        url = os.environ.get("DATABASE_URL")
        if not url:
            raise RuntimeError(
                "DATABASE_URL is not set. "
                "Add it to your .env file or export it before running."
            )
        conn = psycopg.connect(url, autocommit=False)
        return cls(conn)

    @classmethod
    def from_url(cls, url: str) -> "TimescaleWriter":
        """Create a writer from an explicit connection URL."""
        conn = psycopg.connect(url, autocommit=False)
        return cls(conn)

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "TimescaleWriter":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if exc_type is None:
            self._conn.commit()
        else:
            self._conn.rollback()
        self._conn.close()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def write_run(
        self,
        df: pd.DataFrame,
        config: RunConfig,
        label: str | None = None,
        notes: str | None = None,
        batch_size: int = 5_000,
    ) -> int:
        """
        Register a run and bulk-insert all readings in a single transaction.

        Args:
            df:         DataFrame returned by simulator.runner.run()
            config:     RunConfig used to produce the DataFrame
            label:      Optional human-readable name for this run
            notes:      Optional free-text description (fault scenarios, etc.)
            batch_size: Rows per COPY batch (default 5 000 — ~1 MB per batch)

        Returns:
            run_id assigned by the database
        """
        run_id = self._insert_run(config, label, notes)
        self._copy_readings(df, run_id, batch_size)
        return run_id

    def delete_run(self, run_id: int) -> None:
        """
        Delete a run and all its readings (cascades via FK).

        Useful when re-running a scenario and replacing the previous data.
        """
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM runs WHERE run_id = %s", (run_id,))

    def list_runs(self) -> pd.DataFrame:
        """Return a DataFrame summarising all runs in the database."""
        query = """
            SELECT
                r.run_id,
                r.label,
                r.location,
                r.start_date,
                r.end_date,
                r.dt_seconds,
                r.created_at,
                r.notes,
                COUNT(tr.time) AS row_count
            FROM runs r
            LEFT JOIN tower_readings tr USING (run_id)
            GROUP BY r.run_id
            ORDER BY r.run_id DESC
        """
        with self._conn.cursor() as cur:
            cur.execute(query)
            rows = cur.fetchall()
            cols = [d.name for d in cur.description]
        return pd.DataFrame(rows, columns=cols)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _insert_run(
        self,
        config: RunConfig,
        label: str | None,
        notes: str | None,
    ) -> int:
        sql = """
            INSERT INTO runs (label, location, start_date, end_date, dt_seconds, notes)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING run_id
        """
        with self._conn.cursor() as cur:
            cur.execute(sql, (
                label,
                config.location,
                date.fromisoformat(config.start_date),
                date.fromisoformat(config.end_date),
                int(config.dt),
                notes,
            ))
            return cur.fetchone()[0]

    def _copy_readings(
        self,
        df: pd.DataFrame,
        run_id: int,
        batch_size: int,
    ) -> None:
        """
        Bulk-insert readings using psycopg3 COPY (binary text protocol).

        COPY is orders of magnitude faster than executemany for large DataFrames
        because it bypasses per-row parsing overhead on the server side.
        """
        copy_sql = (
            "COPY tower_readings ("
            + ", ".join(_READINGS_COLUMNS)
            + ") FROM STDIN"
        )

        # Rename 'timestamp' → 'time' to match the DB column
        src = df.rename(columns={"timestamp": "time"}).copy()
        src["run_id"] = run_id

        # Ensure column order matches _READINGS_COLUMNS
        src = src[_READINGS_COLUMNS]

        with self._conn.cursor() as cur:
            for batch in _batched(src, batch_size):
                with cur.copy(copy_sql) as copy:
                    for row in batch.itertuples(index=False, name=None):
                        copy.write_row(row)


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------

def _batched(df: pd.DataFrame, size: int) -> Iterator[pd.DataFrame]:
    """Yield successive non-overlapping slices of a DataFrame."""
    for start in range(0, len(df), size):
        yield df.iloc[start : start + size]
