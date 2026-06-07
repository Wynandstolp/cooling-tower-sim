"""Integration tests for pipeline/timescale_writer.py

Requires the TimescaleDB Docker container to be running.
Uses a real database connection — no mocks.
Skip with: pytest -m "not integration"
"""

import pytest

from simulator.runner import RunConfig, run
from pipeline.timescale_writer import TimescaleWriter

_DB_URL = "postgresql://postgres:password@localhost:5432/cooling_tower"

_SHORT_CONFIG = RunConfig(start_date="2024-06-01", end_date="2024-06-02")


def _writer() -> TimescaleWriter:
    return TimescaleWriter.from_url(_DB_URL)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture()
def df():
    return run(_SHORT_CONFIG)


@pytest.fixture()
def written_run(df):
    """Insert a run and yield its run_id; delete it on teardown."""
    with _writer() as w:
        run_id = w.write_run(df, _SHORT_CONFIG, label="_test_run", notes="pytest")
    yield run_id
    with _writer() as w:
        w.delete_run(run_id)


# ---------------------------------------------------------------------------
# write_run
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestWriteRun:
    def test_returns_integer_run_id(self, df):
        with _writer() as w:
            run_id = w.write_run(df, _SHORT_CONFIG, label="_test_write")
        assert isinstance(run_id, int)
        assert run_id > 0
        with _writer() as w:
            w.delete_run(run_id)

    def test_row_count_matches_dataframe(self, df, written_run):
        with _writer() as w:
            import psycopg
            conn = psycopg.connect(_DB_URL)
            cur = conn.cursor()
            cur.execute(
                "SELECT COUNT(*) FROM tower_readings WHERE run_id = %s",
                (written_run,),
            )
            count = cur.fetchone()[0]
            conn.close()
        assert count == len(df)

    def test_run_registered_in_runs_table(self, written_run):
        import psycopg
        conn = psycopg.connect(_DB_URL)
        cur = conn.cursor()
        cur.execute("SELECT label, notes FROM runs WHERE run_id = %s", (written_run,))
        row = cur.fetchone()
        conn.close()
        assert row is not None
        assert row[0] == "_test_run"
        assert row[1] == "pytest"

    def test_config_metadata_stored(self, df):
        config = RunConfig(
            start_date="2024-07-01", end_date="2024-07-02",
            location="townsville",
            dt=120,
        )
        df2 = run(config)
        with _writer() as w:
            run_id = w.write_run(df2, config, label="_test_meta")
        try:
            import psycopg
            conn = psycopg.connect(_DB_URL)
            cur = conn.cursor()
            cur.execute(
                "SELECT location, dt_seconds FROM runs WHERE run_id = %s", (run_id,)
            )
            row = cur.fetchone()
            conn.close()
            assert row[0] == "townsville"
            assert row[1] == 120
        finally:
            with _writer() as w:
                w.delete_run(run_id)


# ---------------------------------------------------------------------------
# delete_run
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestDeleteRun:
    def test_delete_removes_run(self, df):
        with _writer() as w:
            run_id = w.write_run(df, _SHORT_CONFIG, label="_test_delete")
        with _writer() as w:
            w.delete_run(run_id)
        import psycopg
        conn = psycopg.connect(_DB_URL)
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM runs WHERE run_id = %s", (run_id,))
        assert cur.fetchone()[0] == 0
        conn.close()

    def test_delete_cascades_to_readings(self, df):
        with _writer() as w:
            run_id = w.write_run(df, _SHORT_CONFIG, label="_test_cascade")
        with _writer() as w:
            w.delete_run(run_id)
        import psycopg
        conn = psycopg.connect(_DB_URL)
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM tower_readings WHERE run_id = %s", (run_id,)
        )
        assert cur.fetchone()[0] == 0
        conn.close()


# ---------------------------------------------------------------------------
# list_runs
# ---------------------------------------------------------------------------

@pytest.mark.integration
class TestListRuns:
    def test_returns_dataframe(self, written_run):
        with _writer() as w:
            df = w.list_runs()
        import pandas as pd
        assert isinstance(df, pd.DataFrame)

    def test_written_run_appears(self, written_run):
        with _writer() as w:
            df = w.list_runs()
        assert written_run in df["run_id"].values

    def test_row_count_column_populated(self, written_run, df):
        with _writer() as w:
            runs_df = w.list_runs()
        row = runs_df[runs_df["run_id"] == written_run].iloc[0]
        assert row["row_count"] == len(df)
