"""
Cooling tower physics simulator — NTU-effectiveness method.

Models a mechanical draft, counterflow, induced-draft cooling tower.
State is advanced by calling step() with a fixed timestep and a SimulatorInputs
snapshot.  All units are SI unless stated otherwise in field docstrings.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field


# ---------------------------------------------------------------------------
# Design constants  (fixed geometry / nominal operating point)
# ---------------------------------------------------------------------------

# Nominal tower characteristic at design L/G ratio
_KAV_L_NOMINAL: float = 1.8

# Design air mass flow rate [kg/s] at 100% fan speed, scaled from ~150 m³/hr water flow
_G_DESIGN: float = 45.0  # kg/s

# Design water mass flow rate [kg/s] at 150 m³/hr, ρ ≈ 1000 kg/m³
_L_DESIGN: float = 41.7  # kg/s  (150 m³/hr)

# Drift loss as fraction of water flow rate
_DRIFT_FRACTION: float = 0.0002  # 0.02%

# Empirical evaporation coefficient from Nalco Water Handbook / Perry's:
#   ~1% of flow evaporates per 12°F (6.7°C) of cooling range, adjusted for the
#   ~75–80% evaporative fraction of total heat rejection.
#   Gives ≈0.00085 m³ evaporated per m³·°C.  Accurate to ~10–15% for typical
#   counter-flow towers; replace with a full Merkel balance for higher fidelity.
_EVAPORATION_COEFF: float = 0.00085  # m³ evaporated / (m³ circulated · °C range)

# Target cycles of concentration before blowdown is triggered
_COC_TARGET: float = 4.0

# L/G exponent from Merkel/Fills correlation
_LG_EXPONENT: float = -0.6

# Fan affinity law exponent for air flow vs speed
_FAN_EXPONENT: float = 0.8

# Basin volume [m³] — affects conductivity dynamics
_BASIN_VOLUME: float = 50.0


# ---------------------------------------------------------------------------
# Data transfer objects
# ---------------------------------------------------------------------------

@dataclass
class SimulatorInputs:
    """All time-varying inputs required for one simulation step."""

    t_hot_in: float        # Hot water inlet temperature [°C]
    water_flow_m3hr: float # Water flow rate [m³/hr]
    t_amb: float           # Ambient dry bulb temperature [°C]
    t_wb: float            # Ambient wet bulb temperature [°C]
    fan_speed_pct: float   # Fan speed [0–100 %]
    cond_makeup: float     # Makeup water conductivity [µS/cm]


@dataclass
class SimulatorOutputs:
    """All calculated outputs for one simulation step."""

    t_cold_out: float      # Cold water outlet temperature [°C]
    t_approach: float      # Approach temperature = T_cold_out − T_wb [°C]
    t_range: float         # Range = T_hot_in − T_cold_out [°C]
    evaporation_m3hr: float  # Evaporation loss [m³/hr]
    blowdown_m3hr: float   # Blowdown flow [m³/hr]
    makeup_m3hr: float     # Total makeup water required [m³/hr]
    coc: float             # Cycles of concentration [-]
    cond_basin: float      # Basin water conductivity [µS/cm]
    ntu: float             # Number of transfer units [-]
    effectiveness: float   # Heat transfer effectiveness [-]
    fouling_factor: float  # Current fouling factor [0–1, 1 = clean]


# ---------------------------------------------------------------------------
# Simulator
# ---------------------------------------------------------------------------

class CoolingTowerSimulator:
    """
    Stateful cooling tower simulator.

    State variables that persist between steps:
      - cond_basin:     basin conductivity [µS/cm]
      - fouling_factor: packing fouling state [0–1], 1 = fully clean

    Usage::

        sim = CoolingTowerSimulator()
        inputs = SimulatorInputs(t_hot_in=40, water_flow_m3hr=150, ...)
        outputs = sim.step(dt=60, inputs=inputs)
    """

    def __init__(
        self,
        initial_cond_basin: float = 300.0,
        initial_fouling_factor: float = 1.0,
        kav_l_nominal: float = _KAV_L_NOMINAL,
        g_design: float = _G_DESIGN,
        coc_target: float = _COC_TARGET,
        basin_volume: float = _BASIN_VOLUME,
    ) -> None:
        self.cond_basin: float = initial_cond_basin
        self.fouling_factor: float = initial_fouling_factor

        self._kav_l_nominal = kav_l_nominal
        self._g_design = g_design
        self._coc_target = coc_target
        self._basin_volume = basin_volume

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def step(self, dt: float, inputs: SimulatorInputs) -> SimulatorOutputs:
        """
        Advance simulator state by dt seconds and return outputs.

        Args:
            dt:     Timestep [seconds]
            inputs: Operating conditions for this step

        Returns:
            SimulatorOutputs snapshot for this step
        """
        self._validate_inputs(inputs)

        # --- Air mass flow from fan speed (affinity law) ---
        fan_fraction = max(inputs.fan_speed_pct / 100.0, 0.0)
        g_air = self._g_design * (fan_fraction ** _FAN_EXPONENT)  # kg/s

        # --- Water mass flow ---
        water_density = 1000.0  # kg/m³
        l_water = (inputs.water_flow_m3hr / 3600.0) * water_density  # kg/s

        # --- NTU (Merkel, degraded by fouling) ---
        if g_air > 0 and l_water > 0:
            lg_ratio = l_water / g_air
            ntu = self._kav_l_nominal * self.fouling_factor * (lg_ratio ** _LG_EXPONENT)
        else:
            ntu = 0.0

        # --- Effectiveness (single-stream approximation) ---
        effectiveness = 1.0 - math.exp(-ntu)

        # --- Outlet temperature ---
        t_cold_out = inputs.t_hot_in - effectiveness * (inputs.t_hot_in - inputs.t_wb)

        # Physically, outlet can't be below wet bulb
        t_cold_out = max(t_cold_out, inputs.t_wb)

        t_range = inputs.t_hot_in - t_cold_out
        t_approach = t_cold_out - inputs.t_wb

        # --- Evaporation [m³/hr] ---
        evaporation = _EVAPORATION_COEFF * inputs.water_flow_m3hr * t_range

        # --- Blowdown logic ---
        coc = self._calc_coc(inputs.cond_makeup)
        if coc >= self._coc_target and inputs.cond_makeup > 0:
            blowdown = evaporation / (self._coc_target - 1.0)
        else:
            blowdown = 0.0

        # --- Drift [m³/hr] ---
        drift = _DRIFT_FRACTION * inputs.water_flow_m3hr

        # --- Makeup water [m³/hr] ---
        makeup = evaporation + blowdown + drift

        # --- Update basin conductivity ---
        self._update_conductivity(dt, evaporation, blowdown, makeup, inputs.cond_makeup)

        return SimulatorOutputs(
            t_cold_out=t_cold_out,
            t_approach=t_approach,
            t_range=t_range,
            evaporation_m3hr=evaporation,
            blowdown_m3hr=blowdown,
            makeup_m3hr=makeup,
            coc=self._calc_coc(inputs.cond_makeup),
            cond_basin=self.cond_basin,
            ntu=ntu,
            effectiveness=effectiveness,
            fouling_factor=self.fouling_factor,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _calc_coc(self, cond_makeup: float) -> float:
        if cond_makeup <= 0:
            return 0.0
        return self.cond_basin / cond_makeup

    def _update_conductivity(
        self,
        dt: float,
        evaporation_m3hr: float,
        blowdown_m3hr: float,
        makeup_m3hr: float,
        cond_makeup: float,
    ) -> None:
        """
        Mass balance on dissolved solids in basin.

        d(V * C) / dt = Q_makeup * C_makeup - Q_blowdown * C_basin - Q_drift * C_basin

        Solved with forward Euler over dt seconds.
        """
        dt_hr = dt / 3600.0
        drift_m3hr = _DRIFT_FRACTION * (evaporation_m3hr / 0.00085 / max(evaporation_m3hr / 0.00085, 1))

        # Solids in [µS/cm · m³/hr] (proportional to mass flux)
        solids_in = makeup_m3hr * cond_makeup
        solids_out = (blowdown_m3hr + drift_m3hr) * self.cond_basin

        d_cond = (solids_in - solids_out) / self._basin_volume * dt_hr
        self.cond_basin = max(cond_makeup, self.cond_basin + d_cond)

    @staticmethod
    def _validate_inputs(inputs: SimulatorInputs) -> None:
        if inputs.t_wb > inputs.t_hot_in:
            raise ValueError(
                f"Wet bulb ({inputs.t_wb}°C) exceeds hot water inlet ({inputs.t_hot_in}°C) — "
                "thermodynamically impossible."
            )
        if not (0.0 <= inputs.fan_speed_pct <= 100.0):
            raise ValueError(f"fan_speed_pct must be 0–100, got {inputs.fan_speed_pct}")
        if inputs.water_flow_m3hr < 0:
            raise ValueError("water_flow_m3hr must be non-negative")
