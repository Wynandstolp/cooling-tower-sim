"""Unit tests for simulator/cooling_tower.py"""

import math
import pytest

from simulator.cooling_tower import (
    CoolingTowerSimulator,
    SimulatorInputs,
    SimulatorOutputs,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _nominal_inputs(**overrides) -> SimulatorInputs:
    """Return a reasonable nominal operating point, with optional field overrides."""
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


def _step(sim: CoolingTowerSimulator | None = None, dt: float = 60.0, **overrides) -> SimulatorOutputs:
    if sim is None:
        sim = CoolingTowerSimulator()
    return sim.step(dt, _nominal_inputs(**overrides))


# ---------------------------------------------------------------------------
# Thermodynamic sanity
# ---------------------------------------------------------------------------

class TestThermodynamicBounds:
    def test_outlet_below_inlet(self):
        out = _step()
        assert out.t_cold_out < _nominal_inputs().t_hot_in

    def test_outlet_at_or_above_wet_bulb(self):
        inp = _nominal_inputs()
        out = _step()
        assert out.t_cold_out >= inp.t_wb

    def test_approach_positive(self):
        out = _step()
        assert out.t_approach >= 0.0

    def test_range_equals_hot_minus_cold(self):
        inp = _nominal_inputs()
        out = _step()
        assert math.isclose(out.t_range, inp.t_hot_in - out.t_cold_out, rel_tol=1e-9)

    def test_approach_equals_cold_minus_wb(self):
        inp = _nominal_inputs()
        out = _step()
        assert math.isclose(out.t_approach, out.t_cold_out - inp.t_wb, rel_tol=1e-9)

    def test_effectiveness_bounded(self):
        out = _step()
        assert 0.0 < out.effectiveness < 1.0

    def test_ntu_positive_at_full_fan(self):
        out = _step(fan_speed_pct=100.0)
        assert out.ntu > 0.0

    def test_zero_fan_gives_zero_ntu(self):
        out = _step(fan_speed_pct=0.0)
        assert out.ntu == 0.0

    def test_zero_fan_outlet_equals_inlet(self):
        """With no airflow, no heat transfer — outlet should equal inlet."""
        out = _step(fan_speed_pct=0.0)
        assert math.isclose(out.t_cold_out, _nominal_inputs().t_hot_in, rel_tol=1e-9)

    def test_approach_in_typical_range(self):
        """Approach temperature should be 3–8°C at nominal design conditions."""
        out = _step()
        assert 2.0 <= out.t_approach <= 10.0


# ---------------------------------------------------------------------------
# Fan speed sensitivity
# ---------------------------------------------------------------------------

class TestFanSpeed:
    def test_higher_fan_gives_lower_outlet(self):
        out_50 = _step(fan_speed_pct=50.0)
        out_100 = _step(fan_speed_pct=100.0)
        assert out_100.t_cold_out < out_50.t_cold_out

    def test_higher_fan_gives_higher_ntu(self):
        out_50 = _step(fan_speed_pct=50.0)
        out_100 = _step(fan_speed_pct=100.0)
        assert out_100.ntu > out_50.ntu


# ---------------------------------------------------------------------------
# Fouling
# ---------------------------------------------------------------------------

class TestFouling:
    def test_fouled_tower_warmer_outlet(self):
        clean = CoolingTowerSimulator(initial_fouling_factor=1.0)
        fouled = CoolingTowerSimulator(initial_fouling_factor=0.5)
        inp = _nominal_inputs()
        out_clean = clean.step(60, inp)
        out_fouled = fouled.step(60, inp)
        assert out_fouled.t_cold_out > out_clean.t_cold_out

    def test_fully_fouled_approaches_fan_off(self):
        """Near-zero fouling should behave similarly to zero fan."""
        near_zero = CoolingTowerSimulator(initial_fouling_factor=0.0)
        out = near_zero.step(60, _nominal_inputs())
        # Outlet should be close to hot inlet (little to no heat transfer)
        assert out.t_cold_out > _nominal_inputs().t_hot_in - 2.0


# ---------------------------------------------------------------------------
# Evaporation
# ---------------------------------------------------------------------------

class TestEvaporation:
    def test_evaporation_positive(self):
        out = _step()
        assert out.evaporation_m3hr > 0.0

    def test_evaporation_scales_with_range(self):
        """Higher range → more evaporation at same flow."""
        out_hot = _step(t_hot_in=45.0)
        out_cool = _step(t_hot_in=36.0)
        assert out_hot.evaporation_m3hr > out_cool.evaporation_m3hr

    def test_evaporation_matches_formula(self):
        """E = 0.00085 × L × range, per project physics spec."""
        inp = _nominal_inputs()
        out = _step()
        expected = 0.00085 * inp.water_flow_m3hr * out.t_range
        assert math.isclose(out.evaporation_m3hr, expected, rel_tol=1e-9)


# ---------------------------------------------------------------------------
# Blowdown and conductivity
# ---------------------------------------------------------------------------

class TestBlowdownAndConductivity:
    def test_no_blowdown_when_coc_below_target(self):
        """Fresh basin water below CoC target → blowdown = 0."""
        sim = CoolingTowerSimulator(initial_cond_basin=201.0)  # CoC ≈ 1.0 with makeup=200
        out = sim.step(60, _nominal_inputs(cond_makeup=200.0))
        assert out.blowdown_m3hr == 0.0

    def test_blowdown_when_coc_at_target(self):
        """Basin at 4× makeup conductivity should trigger blowdown."""
        sim = CoolingTowerSimulator(initial_cond_basin=800.0)  # CoC = 4.0 with makeup=200
        out = sim.step(60, _nominal_inputs(cond_makeup=200.0))
        assert out.blowdown_m3hr > 0.0

    def test_makeup_ge_evaporation(self):
        """Makeup must always cover at least evaporation losses."""
        out = _step()
        assert out.makeup_m3hr >= out.evaporation_m3hr

    def test_coc_equals_basin_over_makeup(self):
        sim = CoolingTowerSimulator(initial_cond_basin=600.0)
        out = sim.step(60, _nominal_inputs(cond_makeup=200.0))
        assert math.isclose(out.coc, out.cond_basin / 200.0, rel_tol=1e-6)

    def test_conductivity_rises_without_blowdown(self):
        """Basin conductivity should increase over time when CoC < target."""
        sim = CoolingTowerSimulator(initial_cond_basin=250.0)
        inp = _nominal_inputs(cond_makeup=200.0)
        prev = sim.cond_basin
        for _ in range(20):
            sim.step(3600, inp)
        assert sim.cond_basin > prev

    def test_conductivity_stabilises_with_blowdown(self):
        """With blowdown active, conductivity should not climb indefinitely."""
        sim = CoolingTowerSimulator(initial_cond_basin=800.0)
        inp = _nominal_inputs(cond_makeup=200.0)
        readings = []
        for _ in range(48):
            out = sim.step(3600, inp)
            readings.append(out.cond_basin)
        # Last quarter should not be significantly higher than mid-run
        assert max(readings[36:]) < max(readings[12:24]) * 1.1


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

class TestInputValidation:
    def test_wb_above_hot_in_raises(self):
        with pytest.raises(ValueError, match="Wet bulb"):
            _step(t_hot_in=20.0, t_wb=25.0)

    def test_fan_speed_above_100_raises(self):
        with pytest.raises(ValueError, match="fan_speed_pct"):
            _step(fan_speed_pct=110.0)

    def test_fan_speed_below_0_raises(self):
        with pytest.raises(ValueError, match="fan_speed_pct"):
            _step(fan_speed_pct=-5.0)

    def test_negative_flow_raises(self):
        with pytest.raises(ValueError, match="water_flow_m3hr"):
            _step(water_flow_m3hr=-10.0)


# ---------------------------------------------------------------------------
# Output fields completeness
# ---------------------------------------------------------------------------

class TestOutputFields:
    def test_all_output_fields_present(self):
        out = _step()
        fields = [
            "t_cold_out", "t_approach", "t_range",
            "evaporation_m3hr", "blowdown_m3hr", "makeup_m3hr",
            "coc", "cond_basin", "ntu", "effectiveness", "fouling_factor",
        ]
        for f in fields:
            assert hasattr(out, f), f"Missing output field: {f}"

    def test_fouling_factor_reflected_in_output(self):
        sim = CoolingTowerSimulator(initial_fouling_factor=0.7)
        out = sim.step(60, _nominal_inputs())
        assert math.isclose(out.fouling_factor, 0.7)
