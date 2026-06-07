"""Unit tests for simulator/runner.py"""

import math

import numpy as np
import pandas as pd
import pytest

from simulator.faults import FaultInjector, FanFault, FoulingFault
from simulator.runner import RunConfig, _generate_process_inputs, run


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SHORT_CONFIG = RunConfig(start_date="2025-01-01", end_date="2025-01-03")

_EXPECTED_COLUMNS = {
    "timestamp",
    "t_hot_in", "water_flow_m3hr", "t_amb", "t_wb", "fan_speed_pct", "cond_makeup",
    "t_cold_out", "t_approach", "t_range",
    "evaporation_m3hr", "blowdown_m3hr", "makeup_m3hr",
    "coc", "cond_basin", "ntu", "effectiveness", "fouling_factor",
    "active_faults",
}


# ---------------------------------------------------------------------------
# Output shape and schema
# ---------------------------------------------------------------------------

class TestRunOutputShape:
    def test_returns_dataframe(self):
        df = run(_SHORT_CONFIG)
        assert isinstance(df, pd.DataFrame)

    def test_all_columns_present(self):
        df = run(_SHORT_CONFIG)
        assert _EXPECTED_COLUMNS.issubset(set(df.columns))

    def test_row_count_matches_timesteps(self):
        config = RunConfig(start_date="2025-01-01", end_date="2025-01-02", dt=60.0)
        df = run(config)
        # 2 days at 60s steps = 2880 steps; actual count depends on weather window
        assert len(df) > 0
        assert len(df) <= 2 * 24 * 60 + 1

    def test_no_nulls(self):
        df = run(_SHORT_CONFIG)
        assert not df.isnull().any().any()

    def test_timestamps_are_timezone_aware(self):
        df = run(_SHORT_CONFIG)
        assert df["timestamp"].dt.tz is not None

    def test_timestamps_monotonically_increasing(self):
        df = run(_SHORT_CONFIG)
        assert (df["timestamp"].diff().dropna() > pd.Timedelta(0)).all()


# ---------------------------------------------------------------------------
# Physics sanity on full run output
# ---------------------------------------------------------------------------

class TestRunPhysics:
    def test_cold_outlet_below_hot_inlet_always(self):
        df = run(_SHORT_CONFIG)
        assert (df["t_cold_out"] < df["t_hot_in"]).all()

    def test_approach_non_negative_always(self):
        df = run(_SHORT_CONFIG)
        assert (df["t_approach"] >= 0.0).all()

    def test_range_equals_hot_minus_cold(self):
        df = run(_SHORT_CONFIG)
        diff = (df["t_hot_in"] - df["t_cold_out"] - df["t_range"]).abs()
        assert diff.max() < 1e-6

    def test_evaporation_positive(self):
        df = run(_SHORT_CONFIG)
        assert (df["evaporation_m3hr"] > 0).all()

    def test_makeup_ge_evaporation(self):
        df = run(_SHORT_CONFIG)
        assert (df["makeup_m3hr"] >= df["evaporation_m3hr"]).all()

    def test_fouling_factor_stays_at_one_without_fault(self):
        df = run(_SHORT_CONFIG)
        assert (df["fouling_factor"] == 1.0).all()

    def test_active_faults_none_without_injector(self):
        df = run(_SHORT_CONFIG)
        assert (df["active_faults"] == "none").all()


# ---------------------------------------------------------------------------
# Fault injection integration
# ---------------------------------------------------------------------------

class TestRunWithFaults:
    def test_fouling_degrades_fouling_factor(self):
        injector = FaultInjector()
        injector.add(FoulingFault(start_time=0, rate_per_day=0.1))
        df = run(_SHORT_CONFIG, injector)
        assert df["fouling_factor"].iloc[-1] < 1.0

    def test_fouling_increases_outlet_temp(self):
        clean_df = run(_SHORT_CONFIG)
        injector = FaultInjector()
        injector.add(FoulingFault(start_time=0, rate_per_day=0.2))
        fouled_df = run(_SHORT_CONFIG, injector)
        assert fouled_df["t_cold_out"].mean() > clean_df["t_cold_out"].mean()

    def test_fan_fault_reduces_fan_speed(self):
        injector = FaultInjector()
        injector.add(FanFault(start_time=0, target_speed_pct=30.0, ramp_seconds=0))
        df = run(_SHORT_CONFIG, injector)
        assert (df["fan_speed_pct"] == 30.0).all()

    def test_fault_label_appears_in_output(self):
        injector = FaultInjector()
        injector.add(FanFault(start_time=0, target_speed_pct=50.0, label="fan_fault"))
        df = run(_SHORT_CONFIG, injector)
        assert (df["active_faults"] == "fan_fault").all()

    def test_no_injector_defaults_to_clean_run(self):
        df_explicit = run(_SHORT_CONFIG, FaultInjector())
        df_implicit = run(_SHORT_CONFIG, None)
        pd.testing.assert_frame_equal(df_explicit, df_implicit)


# ---------------------------------------------------------------------------
# Reproducibility
# ---------------------------------------------------------------------------

class TestReproducibility:
    def test_same_seed_gives_identical_output(self):
        df1 = run(RunConfig(start_date="2025-01-01", end_date="2025-01-02", seed=99))
        df2 = run(RunConfig(start_date="2025-01-01", end_date="2025-01-02", seed=99))
        pd.testing.assert_frame_equal(df1, df2)

    def test_different_seed_gives_different_process_inputs(self):
        df1 = run(RunConfig(start_date="2025-01-01", end_date="2025-01-02", seed=1))
        df2 = run(RunConfig(start_date="2025-01-01", end_date="2025-01-02", seed=2))
        assert not df1["t_hot_in"].equals(df2["t_hot_in"])


# ---------------------------------------------------------------------------
# Process input generation
# ---------------------------------------------------------------------------

class TestGenerateProcessInputs:
    def test_hot_in_within_bounds(self):
        config = RunConfig(
            start_date="2025-01-01", end_date="2025-01-07",
            t_hot_in_min=35.0, t_hot_in_max=45.0,
        )
        t_hot, _ = _generate_process_inputs(config, n_steps=10_000)
        assert t_hot.min() >= 35.0
        assert t_hot.max() <= 45.0

    def test_flow_within_bounds(self):
        config = RunConfig(
            start_date="2025-01-01", end_date="2025-01-07",
            flow_min=50.0, flow_max=200.0,
        )
        _, flow = _generate_process_inputs(config, n_steps=10_000)
        assert flow.min() >= 50.0
        assert flow.max() <= 200.0

    def test_output_length_matches_n_steps(self):
        config = _SHORT_CONFIG
        t_hot, flow = _generate_process_inputs(config, n_steps=500)
        assert len(t_hot) == 500
        assert len(flow) == 500

    def test_mean_reverts_to_nominal(self):
        config = RunConfig(
            start_date="2025-01-01", end_date="2025-01-07",
            t_hot_in_nominal=40.0, seed=42,
        )
        t_hot, _ = _generate_process_inputs(config, n_steps=100_000)
        assert abs(t_hot.mean() - 40.0) < 0.5
