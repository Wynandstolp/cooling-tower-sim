"""
Simulation runner — ties together the physics engine, weather data, and fault injector.

Drives the cooling tower simulator through a date range using real hourly weather
data (interpolated to the simulation timestep) and a stochastic process model for
the hot-water inlet conditions.

Typical usage::

    from simulator.runner import RunConfig, run
    from simulator.faults import FaultInjector, FoulingFault, FanFault

    config = RunConfig(start_date="2023-01-01", end_date="2023-01-07")

    injector = FaultInjector()
    injector.add(FoulingFault(start_time=0, rate_per_day=0.03))
    injector.add(FanFault(start_time=3 * 86400, target_speed_pct=30.0, ramp_seconds=300))

    df = run(config, injector)
    print(df.head())
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from simulator.cooling_tower import CoolingTowerSimulator, SimulatorInputs
from simulator.faults import FaultInjector
from simulator.weather import QUEENSLAND_LOCATIONS, fetch_weather


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass
class RunConfig:
    """All parameters needed to define a simulation run."""

    start_date: str                  # ISO date, e.g. "2023-01-01"
    end_date: str                    # ISO date, inclusive, e.g. "2023-01-07"
    dt: float = 60.0                 # Timestep [s]
    location: str = "rockhampton"    # Key into QUEENSLAND_LOCATIONS

    # Nominal process setpoints
    t_hot_in_nominal: float = 40.0       # °C
    water_flow_nominal: float = 150.0    # m³/hr
    fan_speed_nominal: float = 100.0     # %
    cond_makeup_nominal: float = 200.0   # µS/cm

    # Ornstein-Uhlenbeck process noise for hot-water inlet and flow
    # Controls how "restless" the process inputs are
    t_hot_in_sigma: float = 0.005    # °C/√s  (noise intensity)
    t_hot_in_theta: float = 1/7200   # 1/s    (mean-reversion speed, ~2-hour timescale)
    flow_sigma: float = 0.05         # m³/hr/√s
    flow_theta: float = 1/3600       # 1/s    (mean-reversion, ~1-hour timescale)

    # Hard bounds for process inputs
    t_hot_in_min: float = 35.0
    t_hot_in_max: float = 45.0
    flow_min: float = 50.0
    flow_max: float = 200.0

    # Initial simulator state
    initial_cond_basin: float = 300.0
    initial_fouling_factor: float = 1.0

    seed: int = 42


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def run(
    config: RunConfig,
    injector: FaultInjector | None = None,
) -> pd.DataFrame:
    """
    Run the cooling tower simulation over the configured date range.

    Args:
        config:   Run parameters (dates, location, process noise, initial state)
        injector: Optional FaultInjector; pass None for a clean baseline run

    Returns:
        DataFrame with one row per timestep containing all inputs and outputs.
        Timestamps are timezone-aware (Australia/Brisbane, UTC+10).
    """
    if injector is None:
        injector = FaultInjector()

    loc = QUEENSLAND_LOCATIONS[config.location]
    weather = fetch_weather(
        config.start_date,
        config.end_date,
        latitude=loc["latitude"],
        longitude=loc["longitude"],
        label=loc["label"],
    )

    sim = CoolingTowerSimulator(
        initial_cond_basin=config.initial_cond_basin,
        initial_fouling_factor=config.initial_fouling_factor,
    )

    sim_start = weather["timestamp"].iloc[0]
    sim_end = weather["timestamp"].iloc[-1]

    timestamps = pd.date_range(
        start=sim_start,
        end=sim_end,
        freq=pd.Timedelta(seconds=config.dt),
        inclusive="left",
    )

    t_amb_interp, t_wb_interp = _interpolate_weather(weather, timestamps)
    t_hot_series, flow_series = _generate_process_inputs(config, len(timestamps))

    records = []
    for i, ts in enumerate(timestamps):
        sim_time = i * config.dt  # seconds from epoch

        inputs = SimulatorInputs(
            t_hot_in=t_hot_series[i],
            water_flow_m3hr=flow_series[i],
            t_amb=t_amb_interp[i],
            t_wb=t_wb_interp[i],
            fan_speed_pct=config.fan_speed_nominal,
            cond_makeup=config.cond_makeup_nominal,
        )

        inputs = injector.apply(sim_time, inputs, sim)

        # Guard: wet bulb must not exceed hot inlet after fault injection
        if inputs.t_wb >= inputs.t_hot_in:
            inputs = SimulatorInputs(
                **{**dataclasses.asdict(inputs), "t_wb": inputs.t_hot_in - 0.5}
            )

        outputs = sim.step(config.dt, inputs)

        row = {
            "timestamp": ts,
            # inputs
            "t_hot_in": inputs.t_hot_in,
            "water_flow_m3hr": inputs.water_flow_m3hr,
            "t_amb": inputs.t_amb,
            "t_wb": inputs.t_wb,
            "fan_speed_pct": inputs.fan_speed_pct,
            "cond_makeup": inputs.cond_makeup,
            # outputs
            "t_cold_out": outputs.t_cold_out,
            "t_approach": outputs.t_approach,
            "t_range": outputs.t_range,
            "evaporation_m3hr": outputs.evaporation_m3hr,
            "blowdown_m3hr": outputs.blowdown_m3hr,
            "makeup_m3hr": outputs.makeup_m3hr,
            "coc": outputs.coc,
            "cond_basin": outputs.cond_basin,
            "ntu": outputs.ntu,
            "effectiveness": outputs.effectiveness,
            "fouling_factor": outputs.fouling_factor,
            # fault state
            "active_faults": ",".join(injector.active_labels) or "none",
        }
        records.append(row)

    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _interpolate_weather(
    weather: pd.DataFrame,
    timestamps: pd.DatetimeIndex,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Linearly interpolate hourly weather onto the simulation timestep grid.

    Returns arrays of t_amb and t_wb aligned to `timestamps`.
    """
    # Convert to float seconds for numpy interp
    t_weather = weather["timestamp"].astype("int64").to_numpy().astype("float64")
    t_sim = timestamps.astype("int64").to_numpy().astype("float64")

    t_amb = np.interp(t_sim, t_weather, weather["t_amb"].to_numpy())
    t_wb = np.interp(t_sim, t_weather, weather["t_wb"].to_numpy())
    return t_amb, t_wb


def _generate_process_inputs(
    config: RunConfig,
    n_steps: int,
) -> tuple[np.ndarray, np.ndarray]:
    """
    Generate stochastic hot-water inlet temperature and flow rate time series
    using an Ornstein-Uhlenbeck (mean-reverting) process.

    The OU update equation (Euler-Maruyama):
        x[t+1] = x[t] + θ(μ - x[t])dt + σ√dt · ε,  ε ~ N(0,1)
    """
    rng = np.random.default_rng(config.seed)
    dt = config.dt

    t_hot = np.empty(n_steps)
    flow = np.empty(n_steps)

    t_hot[0] = config.t_hot_in_nominal
    flow[0] = config.water_flow_nominal

    noise_t = rng.standard_normal(n_steps)
    noise_f = rng.standard_normal(n_steps)

    for i in range(1, n_steps):
        t_hot[i] = (
            t_hot[i - 1]
            + config.t_hot_in_theta * (config.t_hot_in_nominal - t_hot[i - 1]) * dt
            + config.t_hot_in_sigma * dt ** 0.5 * noise_t[i]
        )
        flow[i] = (
            flow[i - 1]
            + config.flow_theta * (config.water_flow_nominal - flow[i - 1]) * dt
            + config.flow_sigma * dt ** 0.5 * noise_f[i]
        )

    # Clip to physical bounds
    t_hot = np.clip(t_hot, config.t_hot_in_min, config.t_hot_in_max)
    flow = np.clip(flow, config.flow_min, config.flow_max)

    return t_hot, flow
