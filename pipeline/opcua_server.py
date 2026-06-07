"""
OPC-UA server for the cooling tower simulator.

Exposes all simulator inputs and outputs as OPC-UA nodes, updated each
simulation step.  Designed to replicate the role of a PLC or SCADA historian
in a real plant — downstream clients (Grafana, MQTT bridge, MES) can browse
and subscribe to nodes by address.

Node hierarchy::

    Root/Objects/CoolingTower/
        Inputs/
            T_HotIn, WaterFlow, T_Amb, T_WetBulb, FanSpeed, CondMakeup
        Outputs/
            T_ColdOut, T_Approach, T_Range,
            EvaporationRate, BlowdownRate, MakeupRate,
            CoC, CondBasin, NTU, Effectiveness, FoulingFactor,
            ActiveFaults

Typical usage (run from project root)::

    python -m pipeline.opcua_server                         # baseline, no faults
    python -m pipeline.opcua_server --scenario fouling      # slow fouling fault
    python -m pipeline.opcua_server --scenario fan_fault    # fan trip on day 3

The server binds on opc.tcp://0.0.0.0:4840/cooling-tower/ by default.
Connect with UAExpert, Prosys OPC-UA Browser, or any asyncua client.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from asyncua import Server

from simulator.cooling_tower import CoolingTowerSimulator, SimulatorInputs, SimulatorOutputs
from simulator.faults import FaultInjector, FanFault, FoulingFault, HighConductivityFault
from simulator.runner import RunConfig, _generate_process_inputs, _interpolate_weather
from simulator.weather import QUEENSLAND_LOCATIONS, fetch_weather

log = logging.getLogger(__name__)

_ENDPOINT = "opc.tcp://0.0.0.0:4840/cooling-tower/"
_NAMESPACE = "urn:cooling-tower-sim"


# ---------------------------------------------------------------------------
# Node definitions
# ---------------------------------------------------------------------------

@dataclass
class _NodeDef:
    browse_name: str
    initial_value: Any


_INPUT_NODES: list[_NodeDef] = [
    _NodeDef("T_HotIn",     40.0),
    _NodeDef("WaterFlow",   150.0),
    _NodeDef("T_Amb",       28.0),
    _NodeDef("T_WetBulb",   22.0),
    _NodeDef("FanSpeed",    100.0),
    _NodeDef("CondMakeup",  200.0),
]

_OUTPUT_NODES: list[_NodeDef] = [
    _NodeDef("T_ColdOut",       25.0),
    _NodeDef("T_Approach",       3.0),
    _NodeDef("T_Range",         15.0),
    _NodeDef("EvaporationRate",  2.0),
    _NodeDef("BlowdownRate",     0.0),
    _NodeDef("MakeupRate",       2.0),
    _NodeDef("CoC",              1.5),
    _NodeDef("CondBasin",      300.0),
    _NodeDef("NTU",              1.9),
    _NodeDef("Effectiveness",   0.85),
    _NodeDef("FoulingFactor",    1.0),
    _NodeDef("ActiveFaults",  "none"),
]


# ---------------------------------------------------------------------------
# Server class
# ---------------------------------------------------------------------------

class CoolingTowerOPCUAServer:
    """
    Wraps asyncua.Server and manages the cooling tower node namespace.

    Lifecycle::
        async with CoolingTowerOPCUAServer() as server:
            await server.update(inputs, outputs)
    """

    def __init__(self, endpoint: str = _ENDPOINT) -> None:
        self._endpoint = endpoint
        self._server = Server()
        self._input_vars: dict[str, Any] = {}
        self._output_vars: dict[str, Any] = {}

    async def __aenter__(self) -> "CoolingTowerOPCUAServer":
        await self._server.init()
        self._server.set_endpoint(self._endpoint)
        self._server.set_server_name("Cooling Tower Simulator")

        idx = await self._server.register_namespace(_NAMESPACE)

        objects = self._server.nodes.objects
        tower = await objects.add_object(idx, "CoolingTower")
        inputs_folder  = await tower.add_object(idx, "Inputs")
        outputs_folder = await tower.add_object(idx, "Outputs")

        for defn in _INPUT_NODES:
            var = await inputs_folder.add_variable(idx, defn.browse_name, defn.initial_value)
            await var.set_writable()  # inputs can be overridden by clients
            self._input_vars[defn.browse_name] = var

        for defn in _OUTPUT_NODES:
            var = await outputs_folder.add_variable(idx, defn.browse_name, defn.initial_value)
            # Outputs are read-only from the client perspective
            self._output_vars[defn.browse_name] = var

        await self._server.start()
        log.info("OPC-UA server started at %s", self._endpoint)
        return self

    async def __aexit__(self, *_) -> None:
        await self._server.stop()
        log.info("OPC-UA server stopped")

    async def update(self, inputs: SimulatorInputs, outputs: SimulatorOutputs) -> None:
        """Push a new set of values to all nodes."""
        input_values = {
            "T_HotIn":    inputs.t_hot_in,
            "WaterFlow":  inputs.water_flow_m3hr,
            "T_Amb":      inputs.t_amb,
            "T_WetBulb":  inputs.t_wb,
            "FanSpeed":   inputs.fan_speed_pct,
            "CondMakeup": inputs.cond_makeup,
        }
        output_values = {
            "T_ColdOut":        outputs.t_cold_out,
            "T_Approach":       outputs.t_approach,
            "T_Range":          outputs.t_range,
            "EvaporationRate":  outputs.evaporation_m3hr,
            "BlowdownRate":     outputs.blowdown_m3hr,
            "MakeupRate":       outputs.makeup_m3hr,
            "CoC":              outputs.coc,
            "CondBasin":        outputs.cond_basin,
            "NTU":              outputs.ntu,
            "Effectiveness":    outputs.effectiveness,
            "FoulingFactor":    outputs.fouling_factor,
        }

        for name, value in input_values.items():
            await self._input_vars[name].write_value(float(value))

        for name, value in output_values.items():
            await self._output_vars[name].write_value(float(value))

    async def update_active_faults(self, label: str) -> None:
        await self._output_vars["ActiveFaults"].write_value(label)


# ---------------------------------------------------------------------------
# Streaming simulation loop
# ---------------------------------------------------------------------------

async def run_realtime(
    config: RunConfig,
    injector: FaultInjector | None = None,
    speed_factor: float = 60.0,
    opcua_server: CoolingTowerOPCUAServer | None = None,
) -> None:
    """
    Run the simulation step-by-step, updating OPC-UA nodes each step.

    Args:
        config:        Run configuration (dates, location, noise params)
        injector:      Optional fault injector
        speed_factor:  How many simulation seconds pass per real second.
                       60 = 1-minute steps run at ~1 Hz (default).
                       1  = real-time (1-minute steps take 1 minute each).
        opcua_server:  A started CoolingTowerOPCUAServer instance.
                       If None, runs the loop without publishing.
    """
    import dataclasses

    if injector is None:
        injector = FaultInjector()

    loc = QUEENSLAND_LOCATIONS[config.location]
    weather = fetch_weather(
        config.start_date, config.end_date,
        latitude=loc["latitude"],
        longitude=loc["longitude"],
        label=loc["label"],
    )

    import pandas as pd
    sim_start = weather["timestamp"].iloc[0]
    sim_end   = weather["timestamp"].iloc[-1]
    timestamps = pd.date_range(
        start=sim_start, end=sim_end,
        freq=pd.Timedelta(seconds=config.dt),
        inclusive="left",
    )

    import numpy as np
    t_amb_arr, t_wb_arr = _interpolate_weather(weather, timestamps)
    t_hot_arr, flow_arr = _generate_process_inputs(config, len(timestamps))

    sim = CoolingTowerSimulator(
        initial_cond_basin=config.initial_cond_basin,
        initial_fouling_factor=config.initial_fouling_factor,
    )

    sleep_per_step = config.dt / speed_factor

    for i, ts in enumerate(timestamps):
        sim_time = i * config.dt

        inputs = SimulatorInputs(
            t_hot_in=t_hot_arr[i],
            water_flow_m3hr=flow_arr[i],
            t_amb=t_amb_arr[i],
            t_wb=t_wb_arr[i],
            fan_speed_pct=config.fan_speed_nominal,
            cond_makeup=config.cond_makeup_nominal,
        )
        inputs = injector.apply(sim_time, inputs, sim)

        if inputs.t_wb >= inputs.t_hot_in:
            inputs = SimulatorInputs(
                **{**dataclasses.asdict(inputs), "t_wb": inputs.t_hot_in - 0.5}
            )

        outputs = sim.step(config.dt, inputs)
        fault_label = ",".join(injector.active_labels) or "none"

        if opcua_server is not None:
            await opcua_server.update(inputs, outputs)
            await opcua_server.update_active_faults(fault_label)

        log.debug(
            "[%s] T_cold=%.1f°C  approach=%.1f°C  NTU=%.2f  faults=%s",
            ts.strftime("%Y-%m-%d %H:%M"), outputs.t_cold_out,
            outputs.t_approach, outputs.ntu, fault_label,
        )

        await asyncio.sleep(sleep_per_step)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

_SCENARIOS: dict[str, FaultInjector] = {}

def _build_scenarios() -> dict[str, FaultInjector]:
    baseline = FaultInjector()

    fouling = FaultInjector()
    fouling.add(FoulingFault(start_time=0, rate_per_day=0.03))

    fan_fault = FaultInjector()
    fan_fault.add(FanFault(start_time=3 * 86400, target_speed_pct=30.0, ramp_seconds=300))

    high_cond = FaultInjector()
    high_cond.add(HighConductivityFault(start_time=2 * 86400))

    combined = FaultInjector()
    combined.add(FoulingFault(start_time=0, rate_per_day=0.02))
    combined.add(FanFault(start_time=4 * 86400, target_speed_pct=50.0, ramp_seconds=600))

    return {
        "baseline":     baseline,
        "fouling":      fouling,
        "fan_fault":    fan_fault,
        "high_cond":    high_cond,
        "combined":     combined,
    }


async def _main(args: argparse.Namespace) -> None:
    scenarios = _build_scenarios()
    injector = scenarios.get(args.scenario, scenarios["baseline"])

    config = RunConfig(
        start_date=args.start,
        end_date=args.end,
        location=args.location,
        dt=args.dt,
    )

    async with CoolingTowerOPCUAServer(endpoint=args.endpoint) as server:
        log.info("Running scenario '%s' at %.0fx speed", args.scenario, args.speed)
        await run_realtime(config, injector, speed_factor=args.speed, opcua_server=server)


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    parser = argparse.ArgumentParser(description="Cooling Tower OPC-UA Server")
    parser.add_argument("--scenario", default="baseline",
                        choices=["baseline", "fouling", "fan_fault", "high_cond", "combined"],
                        help="Fault scenario to inject (default: baseline)")
    parser.add_argument("--start",    default="2023-01-01", help="Simulation start date")
    parser.add_argument("--end",      default="2023-01-07", help="Simulation end date")
    parser.add_argument("--location", default="rockhampton",
                        choices=list(QUEENSLAND_LOCATIONS.keys()))
    parser.add_argument("--dt",       type=float, default=60.0, help="Timestep [s]")
    parser.add_argument("--speed",    type=float, default=60.0,
                        help="Speed factor (60=1Hz per step, 1=real-time)")
    parser.add_argument("--endpoint", default=_ENDPOINT, help="OPC-UA endpoint URL")
    args = parser.parse_args()

    asyncio.run(_main(args))
