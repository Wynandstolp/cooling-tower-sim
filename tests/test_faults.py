"""Unit tests for simulator/faults.py"""

import pytest

from simulator.cooling_tower import CoolingTowerSimulator, SimulatorInputs
from simulator.faults import (
    FaultInjector,
    FanFault,
    FoulingFault,
    HighConductivityFault,
    HotWeatherSpike,
    LowFlowFault,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _inputs(**overrides) -> SimulatorInputs:
    defaults = dict(
        t_hot_in=40.0,
        water_flow_m3hr=150.0,
        t_amb=28.0,
        t_wb=22.0,
        fan_speed_pct=100.0,
        cond_makeup=200.0,
    )
    defaults.update(overrides)
    return SimulatorInputs(**defaults)


def _fresh_sim(**kwargs) -> CoolingTowerSimulator:
    return CoolingTowerSimulator(**kwargs)


# ---------------------------------------------------------------------------
# FoulingFault
# ---------------------------------------------------------------------------

class TestFoulingFault:
    def test_degrades_fouling_factor_over_time(self):
        sim = _fresh_sim(initial_fouling_factor=1.0)
        fault = FoulingFault(start_time=0, rate_per_day=0.1)
        fault.apply(t=86400, inputs=_inputs(), sim=sim)  # 1 day
        assert abs(sim.fouling_factor - 0.9) < 1e-9

    def test_no_effect_before_start(self):
        sim = _fresh_sim(initial_fouling_factor=1.0)
        fault = FoulingFault(start_time=3600, rate_per_day=0.1)
        fault.apply(t=0, inputs=_inputs(), sim=sim)
        assert sim.fouling_factor == 1.0

    def test_stops_at_floor(self):
        sim = _fresh_sim(initial_fouling_factor=1.0)
        fault = FoulingFault(start_time=0, rate_per_day=0.5, floor=0.2)
        fault.apply(t=86400 * 10, inputs=_inputs(), sim=sim)  # 10 days
        assert sim.fouling_factor == 0.2

    def test_stops_progressing_after_end_time(self):
        sim = _fresh_sim(initial_fouling_factor=1.0)
        fault = FoulingFault(start_time=0, rate_per_day=0.1, end_time=86400)
        # Apply at exactly end_time — should be a no-op (t >= end_time)
        fault.apply(t=0, inputs=_inputs(), sim=sim)       # sets to 1.0 (no elapsed)
        fault.apply(t=86400, inputs=_inputs(), sim=sim)   # at end_time, no-op
        assert sim.fouling_factor == 1.0

    def test_active_flag(self):
        sim = _fresh_sim()
        fault = FoulingFault(start_time=0)
        assert not fault.active
        fault.apply(t=3600, inputs=_inputs(), sim=sim)
        assert fault.active


# ---------------------------------------------------------------------------
# FanFault
# ---------------------------------------------------------------------------

class TestFanFault:
    def test_instantaneous_drop(self):
        sim = _fresh_sim()
        fault = FanFault(start_time=0, target_speed_pct=30.0, ramp_seconds=0)
        out = fault.apply(t=0, inputs=_inputs(fan_speed_pct=100.0), sim=sim)
        assert out.fan_speed_pct == 30.0

    def test_no_effect_before_start(self):
        sim = _fresh_sim()
        fault = FanFault(start_time=3600, target_speed_pct=0.0)
        out = fault.apply(t=0, inputs=_inputs(fan_speed_pct=100.0), sim=sim)
        assert out.fan_speed_pct == 100.0

    def test_ramp_midpoint(self):
        sim = _fresh_sim()
        fault = FanFault(start_time=0, target_speed_pct=20.0, ramp_seconds=600)
        # Capture onset speed
        fault.apply(t=0, inputs=_inputs(fan_speed_pct=100.0), sim=sim)
        out = fault.apply(t=300, inputs=_inputs(fan_speed_pct=100.0), sim=sim)
        assert abs(out.fan_speed_pct - 60.0) < 1e-6  # halfway between 100 and 20

    def test_ramp_complete(self):
        sim = _fresh_sim()
        fault = FanFault(start_time=0, target_speed_pct=20.0, ramp_seconds=600)
        fault.apply(t=0, inputs=_inputs(fan_speed_pct=100.0), sim=sim)
        out = fault.apply(t=600, inputs=_inputs(fan_speed_pct=100.0), sim=sim)
        assert abs(out.fan_speed_pct - 20.0) < 1e-6

    def test_recovery_restores_input(self):
        sim = _fresh_sim()
        fault = FanFault(start_time=0, target_speed_pct=0.0, end_time=3600)
        fault.apply(t=0, inputs=_inputs(fan_speed_pct=100.0), sim=sim)
        out = fault.apply(t=3600, inputs=_inputs(fan_speed_pct=100.0), sim=sim)
        assert out.fan_speed_pct == 100.0  # fault window closed, pass-through

    def test_other_inputs_unchanged(self):
        sim = _fresh_sim()
        fault = FanFault(start_time=0, target_speed_pct=50.0)
        inp = _inputs(t_hot_in=42.0, water_flow_m3hr=120.0)
        out = fault.apply(t=0, inputs=inp, sim=sim)
        assert out.t_hot_in == 42.0
        assert out.water_flow_m3hr == 120.0


# ---------------------------------------------------------------------------
# HighConductivityFault
# ---------------------------------------------------------------------------

class TestHighConductivityFault:
    def test_blowdown_suppressed_when_active(self):
        """CoC well above target but fault suppresses blowdown."""
        sim = _fresh_sim(initial_cond_basin=1000.0)
        fault = HighConductivityFault(start_time=0)
        fault.apply(t=0, inputs=_inputs(), sim=sim)
        out = sim.step(60, _inputs(cond_makeup=200.0))
        assert out.blowdown_m3hr == 0.0

    def test_blowdown_restored_after_recovery(self):
        sim = _fresh_sim(initial_cond_basin=1000.0)
        fault = HighConductivityFault(start_time=0, end_time=3600)
        fault.apply(t=0, inputs=_inputs(), sim=sim)
        # Trigger recovery
        fault.apply(t=3600, inputs=_inputs(), sim=sim)
        out = sim.step(60, _inputs(cond_makeup=200.0))
        assert out.blowdown_m3hr > 0.0

    def test_conductivity_rises_under_fault(self):
        sim = _fresh_sim(initial_cond_basin=300.0)
        fault = HighConductivityFault(start_time=0)
        inp = _inputs(cond_makeup=200.0)
        for step in range(48):
            fault.apply(t=step * 3600, inputs=inp, sim=sim)
            sim.step(3600, inp)
        assert sim.cond_basin > 500.0  # should have concentrated significantly

    def test_no_effect_before_start(self):
        sim = _fresh_sim(initial_cond_basin=1000.0)
        original_target = sim._coc_target
        fault = HighConductivityFault(start_time=3600)
        fault.apply(t=0, inputs=_inputs(), sim=sim)
        assert sim._coc_target == original_target


# ---------------------------------------------------------------------------
# LowFlowFault
# ---------------------------------------------------------------------------

class TestLowFlowFault:
    def test_flow_reduced(self):
        sim = _fresh_sim()
        fault = LowFlowFault(start_time=0, target_flow_m3hr=50.0)
        out = fault.apply(t=0, inputs=_inputs(water_flow_m3hr=150.0), sim=sim)
        assert out.water_flow_m3hr == 50.0

    def test_no_effect_before_start(self):
        sim = _fresh_sim()
        fault = LowFlowFault(start_time=3600, target_flow_m3hr=50.0)
        out = fault.apply(t=0, inputs=_inputs(water_flow_m3hr=150.0), sim=sim)
        assert out.water_flow_m3hr == 150.0

    def test_recovery(self):
        sim = _fresh_sim()
        fault = LowFlowFault(start_time=0, target_flow_m3hr=50.0, end_time=3600)
        fault.apply(t=0, inputs=_inputs(water_flow_m3hr=150.0), sim=sim)
        out = fault.apply(t=3600, inputs=_inputs(water_flow_m3hr=150.0), sim=sim)
        assert out.water_flow_m3hr == 150.0

    def test_other_inputs_unchanged(self):
        sim = _fresh_sim()
        fault = LowFlowFault(start_time=0, target_flow_m3hr=50.0)
        inp = _inputs(t_hot_in=42.0, fan_speed_pct=80.0)
        out = fault.apply(t=0, inputs=inp, sim=sim)
        assert out.t_hot_in == 42.0
        assert out.fan_speed_pct == 80.0


# ---------------------------------------------------------------------------
# HotWeatherSpike
# ---------------------------------------------------------------------------

class TestHotWeatherSpike:
    def test_offsets_applied(self):
        sim = _fresh_sim()
        fault = HotWeatherSpike(start_time=0, end_time=3600, t_wb_offset=3.0, t_amb_offset=4.0)
        inp = _inputs(t_wb=22.0, t_amb=28.0)
        out = fault.apply(t=0, inputs=inp, sim=sim)
        assert abs(out.t_wb - 25.0) < 1e-9
        assert abs(out.t_amb - 32.0) < 1e-9

    def test_no_effect_outside_window(self):
        sim = _fresh_sim()
        fault = HotWeatherSpike(start_time=3600, end_time=7200, t_wb_offset=3.0)
        out_before = fault.apply(t=0, inputs=_inputs(t_wb=22.0), sim=sim)
        out_after = fault.apply(t=7200, inputs=_inputs(t_wb=22.0), sim=sim)
        assert out_before.t_wb == 22.0
        assert out_after.t_wb == 22.0

    def test_other_inputs_unchanged(self):
        sim = _fresh_sim()
        fault = HotWeatherSpike(start_time=0, end_time=3600, t_wb_offset=2.0)
        inp = _inputs(t_hot_in=41.0, fan_speed_pct=90.0)
        out = fault.apply(t=0, inputs=inp, sim=sim)
        assert out.t_hot_in == 41.0
        assert out.fan_speed_pct == 90.0


# ---------------------------------------------------------------------------
# FaultInjector
# ---------------------------------------------------------------------------

class TestFaultInjector:
    def test_applies_multiple_faults(self):
        sim = _fresh_sim()
        injector = FaultInjector()
        injector.add(FanFault(start_time=0, target_speed_pct=50.0))
        injector.add(LowFlowFault(start_time=0, target_flow_m3hr=80.0))
        out = injector.apply(t=0, inputs=_inputs(), sim=sim)
        assert out.fan_speed_pct == 50.0
        assert out.water_flow_m3hr == 80.0

    def test_remove_by_label(self):
        sim = _fresh_sim()
        injector = FaultInjector()
        injector.add(FanFault(start_time=0, target_speed_pct=50.0))
        injector.add(LowFlowFault(start_time=0, target_flow_m3hr=80.0))
        injector.remove("fan_fault")
        out = injector.apply(t=0, inputs=_inputs(fan_speed_pct=100.0), sim=sim)
        assert out.fan_speed_pct == 100.0  # fan fault removed
        assert out.water_flow_m3hr == 80.0  # low flow still active

    def test_clear(self):
        sim = _fresh_sim()
        injector = FaultInjector()
        injector.add(FanFault(start_time=0, target_speed_pct=0.0))
        injector.clear()
        out = injector.apply(t=0, inputs=_inputs(fan_speed_pct=100.0), sim=sim)
        assert out.fan_speed_pct == 100.0

    def test_active_labels(self):
        sim = _fresh_sim()
        injector = FaultInjector()
        injector.add(FanFault(start_time=0, target_speed_pct=50.0, label="fan_fault"))
        injector.add(LowFlowFault(start_time=9999, target_flow_m3hr=80.0, label="low_flow"))
        injector.apply(t=0, inputs=_inputs(), sim=sim)
        assert "fan_fault" in injector.active_labels
        assert "low_flow" not in injector.active_labels  # hasn't triggered yet

    def test_no_faults_is_passthrough(self):
        sim = _fresh_sim()
        injector = FaultInjector()
        inp = _inputs()
        out = injector.apply(t=0, inputs=inp, sim=sim)
        assert out == inp
