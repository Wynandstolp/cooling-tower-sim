"""
Fault injection for the cooling tower simulator.

Each fault is a self-contained object that knows how to modify SimulatorInputs
and/or the simulator's internal state for a given simulation time.  The
FaultInjector holds a collection of faults and applies them each step.

Usage::

    from simulator.faults import FaultInjector, FanFault, FoulingFault

    injector = FaultInjector()
    injector.add(FoulingFault(start_time=0, rate_per_day=0.02))
    injector.add(FanFault(start_time=72 * 3600, target_speed_pct=30.0, ramp_seconds=300))

    # In the simulation loop:
    inputs = injector.apply(sim_time_seconds, inputs, sim)
    outputs = sim.step(dt, inputs)

All faults accept `start_time` and optional `end_time` in seconds from
simulation epoch.  Outside that window the fault is a no-op.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Protocol

from simulator.cooling_tower import CoolingTowerSimulator, SimulatorInputs


# ---------------------------------------------------------------------------
# Fault protocol
# ---------------------------------------------------------------------------

class Fault(Protocol):
    """
    Interface every fault must satisfy.

    apply() receives the current simulation time [s], a copy of the inputs
    for this step, and the simulator instance (for state mutations).
    It returns the (possibly modified) inputs.
    """

    def apply(
        self,
        t: float,
        inputs: SimulatorInputs,
        sim: CoolingTowerSimulator,
    ) -> SimulatorInputs:
        ...

    @property
    def active(self) -> bool:
        """True if the fault has ever been triggered."""
        ...

    @property
    def label(self) -> str:
        """Short human-readable name for logging / Grafana annotations."""
        ...


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------

def _in_window(t: float, start: float, end: float | None) -> bool:
    return t >= start and (end is None or t < end)


def _lerp(a: float, b: float, frac: float) -> float:
    """Linear interpolation, frac clamped to [0, 1]."""
    frac = max(0.0, min(1.0, frac))
    return a + (b - a) * frac


# ---------------------------------------------------------------------------
# Concrete faults
# ---------------------------------------------------------------------------

@dataclass
class FoulingFault:
    """
    Packing fouling — degrades fouling_factor linearly over time.

    Simulates scale and biofilm buildup reducing the effective transfer area.
    The fouling_factor on the simulator instance is mutated directly each step
    so that its effect persists into subsequent steps even without the fault
    being re-applied.

    Args:
        start_time:   Simulation time [s] at which fouling begins
        rate_per_day: Fractional degradation of fouling_factor per day
                      (e.g. 0.02 = loses 2 percentage points per day)
        end_time:     Optional time [s] at which fouling stops progressing
        floor:        Minimum fouling_factor (fully fouled = 0.0)
    """

    start_time: float
    rate_per_day: float = 0.02
    end_time: float | None = None
    floor: float = 0.1
    label: str = "fouling"
    _triggered: bool = field(default=False, init=False, repr=False)

    def apply(
        self,
        t: float,
        inputs: SimulatorInputs,
        sim: CoolingTowerSimulator,
    ) -> SimulatorInputs:
        if not _in_window(t, self.start_time, self.end_time):
            return inputs
        self._triggered = True
        # dt since fault started [days]
        elapsed_days = (t - self.start_time) / 86400.0
        degradation = self.rate_per_day * elapsed_days
        sim.fouling_factor = max(self.floor, 1.0 - degradation)
        return inputs

    @property
    def active(self) -> bool:
        return self._triggered


@dataclass
class FanFault:
    """
    Fan speed fault — drops fan speed to a target value, with optional ramp.

    Models motor failure, belt slip, or VFD fault.  Speed ramps linearly from
    the value at fault onset to `target_speed_pct` over `ramp_seconds`.

    Args:
        start_time:       Simulation time [s] at which fault triggers
        target_speed_pct: Fan speed after fault settles [0–100 %]
        ramp_seconds:     Time [s] to reach target (0 = instantaneous)
        end_time:         Optional recovery time [s]; fan returns to original
                          speed immediately on recovery
    """

    start_time: float
    target_speed_pct: float = 0.0
    ramp_seconds: float = 0.0
    end_time: float | None = None
    label: str = "fan_fault"
    _triggered: bool = field(default=False, init=False, repr=False)
    _speed_at_onset: float | None = field(default=None, init=False, repr=False)

    def apply(
        self,
        t: float,
        inputs: SimulatorInputs,
        sim: CoolingTowerSimulator,
    ) -> SimulatorInputs:
        if not _in_window(t, self.start_time, self.end_time):
            return inputs
        self._triggered = True

        if self._speed_at_onset is None:
            self._speed_at_onset = inputs.fan_speed_pct

        if self.ramp_seconds > 0:
            frac = (t - self.start_time) / self.ramp_seconds
            new_speed = _lerp(self._speed_at_onset, self.target_speed_pct, frac)
        else:
            new_speed = self.target_speed_pct

        return SimulatorInputs(
            t_hot_in=inputs.t_hot_in,
            water_flow_m3hr=inputs.water_flow_m3hr,
            t_amb=inputs.t_amb,
            t_wb=inputs.t_wb,
            fan_speed_pct=new_speed,
            cond_makeup=inputs.cond_makeup,
        )

    @property
    def active(self) -> bool:
        return self._triggered


@dataclass
class HighConductivityFault:
    """
    Stuck blowdown valve — forces blowdown to zero, letting CoC climb.

    Simulates a failed-closed blowdown valve or control system fault.
    Conductivity rises naturally through the simulator's mass balance;
    this fault simply zeros out the blowdown term by overriding the
    simulator's CoC target to an unreachably high value.

    Args:
        start_time: Simulation time [s] at which valve sticks shut
        end_time:   Optional time [s] at which valve is restored
    """

    start_time: float
    end_time: float | None = None
    label: str = "high_conductivity"
    _triggered: bool = field(default=False, init=False, repr=False)
    _original_coc_target: float | None = field(default=None, init=False, repr=False)

    def apply(
        self,
        t: float,
        inputs: SimulatorInputs,
        sim: CoolingTowerSimulator,
    ) -> SimulatorInputs:
        if _in_window(t, self.start_time, self.end_time):
            self._triggered = True
            if self._original_coc_target is None:
                self._original_coc_target = sim._coc_target
            sim._coc_target = 9999.0  # blowdown never triggers
        else:
            # Restore original target on recovery
            if self._original_coc_target is not None:
                sim._coc_target = self._original_coc_target
                self._original_coc_target = None
        return inputs

    @property
    def active(self) -> bool:
        return self._triggered


@dataclass
class LowFlowFault:
    """
    Water flow reduction — drops flow to a fraction of nominal.

    Simulates upstream pump trip, partially closed isolation valve, or
    strainer blockage.  Flow drops instantaneously to `target_flow_m3hr`.

    Args:
        start_time:        Simulation time [s] at fault onset
        target_flow_m3hr:  Water flow after fault [m³/hr]
        end_time:          Optional recovery time [s]
    """

    start_time: float
    target_flow_m3hr: float
    end_time: float | None = None
    label: str = "low_flow"
    _triggered: bool = field(default=False, init=False, repr=False)

    def apply(
        self,
        t: float,
        inputs: SimulatorInputs,
        sim: CoolingTowerSimulator,
    ) -> SimulatorInputs:
        if not _in_window(t, self.start_time, self.end_time):
            return inputs
        self._triggered = True
        return SimulatorInputs(
            t_hot_in=inputs.t_hot_in,
            water_flow_m3hr=self.target_flow_m3hr,
            t_amb=inputs.t_amb,
            t_wb=inputs.t_wb,
            fan_speed_pct=inputs.fan_speed_pct,
            cond_makeup=inputs.cond_makeup,
        )

    @property
    def active(self) -> bool:
        return self._triggered


@dataclass
class HotWeatherSpike:
    """
    Ambient wet bulb spike — adds a temperature offset over a time window.

    The actual T_amb and T_wb are driven by real weather data in the runner;
    this fault adds an additional offset on top to simulate an unusually hot
    or humid period that narrows the approach temperature.

    Args:
        start_time:  Simulation time [s] at spike start
        end_time:    Simulation time [s] at spike end
        t_wb_offset: Additional wet bulb temperature [°C] (positive = hotter)
        t_amb_offset: Additional dry bulb temperature [°C]
    """

    start_time: float
    end_time: float
    t_wb_offset: float = 3.0
    t_amb_offset: float = 3.0
    label: str = "hot_weather_spike"
    _triggered: bool = field(default=False, init=False, repr=False)

    def apply(
        self,
        t: float,
        inputs: SimulatorInputs,
        sim: CoolingTowerSimulator,
    ) -> SimulatorInputs:
        if not _in_window(t, self.start_time, self.end_time):
            return inputs
        self._triggered = True
        return SimulatorInputs(
            t_hot_in=inputs.t_hot_in,
            water_flow_m3hr=inputs.water_flow_m3hr,
            t_amb=inputs.t_amb + self.t_amb_offset,
            t_wb=inputs.t_wb + self.t_wb_offset,
            fan_speed_pct=inputs.fan_speed_pct,
            cond_makeup=inputs.cond_makeup,
        )

    @property
    def active(self) -> bool:
        return self._triggered


# ---------------------------------------------------------------------------
# Injector
# ---------------------------------------------------------------------------

@dataclass
class FaultInjector:
    """
    Applies a collection of faults to each simulation step.

    Faults are applied in the order they were added.  If multiple faults
    modify the same input field the last one wins.
    """

    _faults: list = field(default_factory=list, init=False, repr=False)

    def add(self, fault) -> None:
        """Register a fault."""
        self._faults.append(fault)

    def remove(self, label: str) -> None:
        """Remove all faults with the given label."""
        self._faults = [f for f in self._faults if f.label != label]

    def clear(self) -> None:
        """Remove all faults."""
        self._faults.clear()

    def apply(
        self,
        t: float,
        inputs: SimulatorInputs,
        sim: CoolingTowerSimulator,
    ) -> SimulatorInputs:
        """Apply all registered faults and return the modified inputs."""
        for fault in self._faults:
            inputs = fault.apply(t, inputs, sim)
        return inputs

    @property
    def active_labels(self) -> list[str]:
        """Labels of all faults that have triggered at least once."""
        return [f.label for f in self._faults if f.active]
