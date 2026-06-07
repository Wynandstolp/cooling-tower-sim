"""
Unified pipeline entrypoint for the cooling tower simulator.

Runs one simulation loop that simultaneously:
  - Updates an OPC-UA server (opc.tcp://0.0.0.0:4840/cooling-tower/)
  - Publishes to an MQTT broker
  - Accumulates readings and bulk-writes to TimescaleDB at the end

Any output can be disabled via --no-opcua / --no-mqtt / --no-db flags,
which is useful for development or when only one transport is needed.

Usage::

    python -m pipeline.run --scenario fouling --start 2025-01-01 --end 2025-01-31
    python -m pipeline.run --scenario baseline --no-opcua --speed 3600
    python -m pipeline.run --help
"""

from __future__ import annotations

import argparse
import asyncio
import dataclasses
import logging

import pandas as pd

from simulator.cooling_tower import CoolingTowerSimulator, SimulatorInputs
from simulator.faults import FaultInjector, FanFault, FoulingFault, HighConductivityFault
from simulator.runner import RunConfig, _generate_process_inputs, _interpolate_weather
from simulator.weather import QUEENSLAND_LOCATIONS, fetch_weather

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Scenario registry (shared with opcua_server / mqtt_publisher CLIs)
# ---------------------------------------------------------------------------

def _build_injector(scenario: str) -> FaultInjector:
    injector = FaultInjector()
    if scenario == "fouling":
        injector.add(FoulingFault(start_time=0, rate_per_day=0.03))
    elif scenario == "fan_fault":
        injector.add(FanFault(start_time=3 * 86400, target_speed_pct=30.0, ramp_seconds=300))
    elif scenario == "high_cond":
        injector.add(HighConductivityFault(start_time=2 * 86400))
    elif scenario == "combined":
        injector.add(FoulingFault(start_time=0, rate_per_day=0.02))
        injector.add(FanFault(start_time=4 * 86400, target_speed_pct=50.0, ramp_seconds=600))
    return injector


# ---------------------------------------------------------------------------
# Pipeline runner
# ---------------------------------------------------------------------------

async def run_pipeline(
    config: RunConfig,
    injector: FaultInjector | None = None,
    *,
    speed_factor: float = 60.0,
    enable_opcua: bool = True,
    enable_mqtt: bool = True,
    enable_db: bool = True,
    label: str | None = None,
    notes: str | None = None,
) -> int | None:
    """
    Run the full pipeline for the given config.

    Returns the TimescaleDB run_id if enable_db is True, else None.
    """
    from pipeline.mqtt_publisher import MQTTPublisher
    from pipeline.opcua_server import CoolingTowerOPCUAServer
    from pipeline.timescale_writer import TimescaleWriter

    if injector is None:
        injector = FaultInjector()

    loc = QUEENSLAND_LOCATIONS[config.location]
    weather = fetch_weather(
        config.start_date, config.end_date,
        latitude=loc["latitude"],
        longitude=loc["longitude"],
        label=loc["label"],
    )

    sim_start = weather["timestamp"].iloc[0]
    sim_end   = weather["timestamp"].iloc[-1]
    timestamps = pd.date_range(
        start=sim_start, end=sim_end,
        freq=pd.Timedelta(seconds=config.dt),
        inclusive="left",
    )

    t_amb_arr, t_wb_arr = _interpolate_weather(weather, timestamps)
    t_hot_arr, flow_arr = _generate_process_inputs(config, len(timestamps))

    sim = CoolingTowerSimulator(
        initial_cond_basin=config.initial_cond_basin,
        initial_fouling_factor=config.initial_fouling_factor,
    )

    sleep_per_step = config.dt / speed_factor
    records = [] if enable_db else None

    mqtt_pub = MQTTPublisher.from_env(location=config.location) if enable_mqtt else None
    if mqtt_pub:
        mqtt_pub.connect()

    try:
        opcua_ctx = CoolingTowerOPCUAServer() if enable_opcua else _NullContext()
        async with opcua_ctx as opcua:
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

                if opcua is not None:
                    await opcua.update(inputs, outputs)
                    await opcua.update_active_faults(fault_label)

                if mqtt_pub is not None:
                    mqtt_pub.publish(ts.to_pydatetime(), inputs, outputs, fault_label)

                if records is not None:
                    records.append({
                        "timestamp":        ts,
                        "t_hot_in":         inputs.t_hot_in,
                        "water_flow_m3hr":  inputs.water_flow_m3hr,
                        "t_amb":            inputs.t_amb,
                        "t_wb":             inputs.t_wb,
                        "fan_speed_pct":    inputs.fan_speed_pct,
                        "cond_makeup":      inputs.cond_makeup,
                        "t_cold_out":       outputs.t_cold_out,
                        "t_approach":       outputs.t_approach,
                        "t_range":          outputs.t_range,
                        "evaporation_m3hr": outputs.evaporation_m3hr,
                        "blowdown_m3hr":    outputs.blowdown_m3hr,
                        "makeup_m3hr":      outputs.makeup_m3hr,
                        "coc":              outputs.coc,
                        "cond_basin":       outputs.cond_basin,
                        "ntu":              outputs.ntu,
                        "effectiveness":    outputs.effectiveness,
                        "fouling_factor":   outputs.fouling_factor,
                        "active_faults":    fault_label,
                    })

                log.info(
                    "[%s] T_cold=%.1f°C  approach=%.1f°C  NTU=%.2f  faults=%s",
                    ts.strftime("%Y-%m-%d %H:%M"),
                    outputs.t_cold_out, outputs.t_approach, outputs.ntu, fault_label,
                )

                await asyncio.sleep(sleep_per_step)

    finally:
        if mqtt_pub is not None:
            mqtt_pub.disconnect()

    run_id = None
    if enable_db and records:
        df = pd.DataFrame(records)
        with TimescaleWriter.from_env() as writer:
            run_id = writer.write_run(df, config, label=label, notes=notes)
        log.info("Written %d rows to TimescaleDB as run_id=%d", len(df), run_id)

    return run_id


# ---------------------------------------------------------------------------
# Null async context — stands in when OPC-UA is disabled
# ---------------------------------------------------------------------------

class _NullContext:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_):
        pass


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

async def _main(args: argparse.Namespace) -> None:
    config = RunConfig(
        start_date=args.start,
        end_date=args.end,
        location=args.location,
        dt=args.dt,
    )
    injector = _build_injector(args.scenario)

    run_id = await run_pipeline(
        config,
        injector,
        speed_factor=args.speed,
        enable_opcua=not args.no_opcua,
        enable_mqtt=not args.no_mqtt,
        enable_db=not args.no_db,
        label=args.label or f"{args.scenario}_{args.start}",
        notes=args.notes,
    )

    if run_id is not None:
        print(f"run_id={run_id}")


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    parser = argparse.ArgumentParser(
        description="Cooling Tower Simulator — unified pipeline (OPC-UA + MQTT + TimescaleDB)"
    )
    parser.add_argument("--scenario", default="baseline",
                        choices=["baseline", "fouling", "fan_fault", "high_cond", "combined"],
                        help="Fault scenario to inject (default: baseline)")
    parser.add_argument("--start",    default="2025-01-01", help="Simulation start date")
    parser.add_argument("--end",      default="2025-01-07", help="Simulation end date")
    parser.add_argument("--location", default="rockhampton",
                        choices=list(QUEENSLAND_LOCATIONS.keys()))
    parser.add_argument("--dt",       type=float, default=60.0, help="Timestep in seconds")
    parser.add_argument("--speed",    type=float, default=60.0,
                        help="Speed factor: 60=~1 Hz per step, 1=real-time")
    parser.add_argument("--label",    default=None, help="Human-readable run label for TimescaleDB")
    parser.add_argument("--notes",    default=None, help="Free-text run notes for TimescaleDB")
    parser.add_argument("--no-opcua", action="store_true", help="Disable OPC-UA server")
    parser.add_argument("--no-mqtt",  action="store_true", help="Disable MQTT publishing")
    parser.add_argument("--no-db",    action="store_true", help="Disable TimescaleDB write")
    args = parser.parse_args()

    asyncio.run(_main(args))
