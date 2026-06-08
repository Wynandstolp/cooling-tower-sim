"""
MQTT publisher for the cooling tower simulator.

Publishes simulator readings to two topic structures each step:

1. Flat telemetry topic — a single JSON payload with all values, designed
   for consumption by Grafana's MQTT datasource plugin:

       {prefix}/{location}/telemetry
       → {"timestamp": "2023-01-01T00:00:00+10:00", "t_cold_out": 24.5, ...}

2. Per-variable topics — individual float/string payloads, useful for
   Node-RED automations, alerting rules, and SCADA integrations:

       {prefix}/{location}/inputs/t_hot_in      → "40.12"
       {prefix}/{location}/outputs/t_cold_out   → "24.51"
       {prefix}/{location}/faults               → "fouling,fan_fault"

Typical standalone usage::

    python -m pipeline.mqtt_publisher --scenario fouling --speed 60

Typical embedded usage (alongside the OPC-UA server)::

    publisher = MQTTPublisher.from_env(location="rockhampton")
    with publisher:
        publisher.publish(timestamp, inputs, outputs, active_faults)
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

from simulator.cooling_tower import SimulatorInputs, SimulatorOutputs
from simulator.weather import DEFAULT_LOCATION

log = logging.getLogger(__name__)

_DEFAULT_HOST   = "localhost"
_DEFAULT_PORT   = 1883
_DEFAULT_PREFIX = "cooling_tower"


class MQTTPublisher:
    """
    Thin wrapper around paho-mqtt that publishes simulator readings.

    Use as a context manager to guarantee clean connect/disconnect::

        with MQTTPublisher.from_env() as pub:
            pub.publish(ts, inputs, outputs, "none")
    """

    def __init__(
        self,
        host: str = _DEFAULT_HOST,
        port: int = _DEFAULT_PORT,
        topic_prefix: str = _DEFAULT_PREFIX,
        location: str = DEFAULT_LOCATION,
        qos: int = 1,
        client_id: str = "cooling-tower-sim",
    ) -> None:
        self._host = host
        self._port = port
        self._prefix = topic_prefix
        self._location = location
        self._qos = qos

        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id,
        )
        self._client.on_connect    = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        self._client.on_publish    = self._on_publish

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls, env_file: str = ".env", **overrides) -> "MQTTPublisher":
        """
        Create a publisher from environment variables or a .env file.

        Variables read:
            MQTT_HOST           (default: localhost)
            MQTT_PORT           (default: 1883)
            MQTT_TOPIC_PREFIX   (default: cooling_tower)
        """
        load_dotenv(env_file, override=False)
        return cls(
            host=overrides.get("host", os.environ.get("MQTT_HOST", _DEFAULT_HOST)),
            port=int(overrides.get("port", os.environ.get("MQTT_PORT", _DEFAULT_PORT))),
            topic_prefix=overrides.get(
                "topic_prefix",
                os.environ.get("MQTT_TOPIC_PREFIX", _DEFAULT_PREFIX),
            ),
            **{k: v for k, v in overrides.items() if k not in ("host", "port", "topic_prefix")},
        )

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    def __enter__(self) -> "MQTTPublisher":
        self.connect()
        return self

    def __exit__(self, *_) -> None:
        self.disconnect()

    # ------------------------------------------------------------------
    # Connection management
    # ------------------------------------------------------------------

    def connect(self) -> None:
        """Connect to the broker and start the background network loop."""
        self._client.connect(self._host, self._port, keepalive=60)
        self._client.loop_start()

    def disconnect(self) -> None:
        """Stop the network loop and disconnect cleanly."""
        self._client.loop_stop()
        self._client.disconnect()

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish(
        self,
        timestamp: datetime,
        inputs: SimulatorInputs,
        outputs: SimulatorOutputs,
        active_faults: str = "none",
    ) -> None:
        """
        Publish one simulation step to MQTT.

        Sends:
          - One flat JSON telemetry message to …/telemetry
          - Individual float messages for every input and output variable
          - One string message to …/faults
        """
        base = f"{self._prefix}/{self._location}"

        # --- 1. Flat telemetry JSON ---
        payload = _build_telemetry(timestamp, inputs, outputs, active_faults)
        self._publish(f"{base}/telemetry", json.dumps(payload))

        # --- 2. Per-variable input topics ---
        input_values = {
            "t_hot_in":         inputs.t_hot_in,
            "water_flow_m3hr":  inputs.water_flow_m3hr,
            "t_amb":            inputs.t_amb,
            "t_wb":             inputs.t_wb,
            "fan_speed_pct":    inputs.fan_speed_pct,
            "cond_makeup":      inputs.cond_makeup,
        }
        for name, value in input_values.items():
            self._publish(f"{base}/inputs/{name}", f"{value:.4f}")

        # --- 3. Per-variable output topics ---
        output_values = {
            "t_cold_out":        outputs.t_cold_out,
            "t_approach":        outputs.t_approach,
            "t_range":           outputs.t_range,
            "evaporation_m3hr":  outputs.evaporation_m3hr,
            "blowdown_m3hr":     outputs.blowdown_m3hr,
            "makeup_m3hr":       outputs.makeup_m3hr,
            "coc":               outputs.coc,
            "cond_basin":        outputs.cond_basin,
            "ntu":               outputs.ntu,
            "effectiveness":     outputs.effectiveness,
            "fouling_factor":    outputs.fouling_factor,
        }
        for name, value in output_values.items():
            self._publish(f"{base}/outputs/{name}", f"{value:.4f}")

        # --- 4. Fault state ---
        self._publish(f"{base}/faults", active_faults, retain=True)

    def _publish(self, topic: str, payload: str, retain: bool = False) -> None:
        result = self._client.publish(topic, payload, qos=self._qos, retain=retain)
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            log.warning("Publish to %s failed: rc=%d", topic, result.rc)

    # ------------------------------------------------------------------
    # Paho callbacks
    # ------------------------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties) -> None:
        if reason_code == 0:
            log.info("MQTT connected to %s:%d", self._host, self._port)
        else:
            log.error("MQTT connection failed: reason_code=%s", reason_code)

    def _on_disconnect(self, client, userdata, flags, reason_code, properties) -> None:
        log.info("MQTT disconnected (reason_code=%s)", reason_code)

    def _on_publish(self, client, userdata, mid, reason_code, properties) -> None:
        log.debug("MQTT message published mid=%d", mid)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_telemetry(
    timestamp: datetime,
    inputs: SimulatorInputs,
    outputs: SimulatorOutputs,
    active_faults: str,
) -> dict:
    """Build the flat telemetry dict consumed by Grafana's MQTT datasource."""
    return {
        "timestamp":        timestamp.isoformat(),
        # inputs
        "t_hot_in":         round(inputs.t_hot_in, 3),
        "water_flow_m3hr":  round(inputs.water_flow_m3hr, 3),
        "t_amb":            round(inputs.t_amb, 3),
        "t_wb":             round(inputs.t_wb, 3),
        "fan_speed_pct":    round(inputs.fan_speed_pct, 1),
        "cond_makeup":      round(inputs.cond_makeup, 1),
        # outputs
        "t_cold_out":       round(outputs.t_cold_out, 3),
        "t_approach":       round(outputs.t_approach, 3),
        "t_range":          round(outputs.t_range, 3),
        "evaporation_m3hr": round(outputs.evaporation_m3hr, 4),
        "blowdown_m3hr":    round(outputs.blowdown_m3hr, 4),
        "makeup_m3hr":      round(outputs.makeup_m3hr, 4),
        "coc":              round(outputs.coc, 3),
        "cond_basin":       round(outputs.cond_basin, 2),
        "ntu":              round(outputs.ntu, 4),
        "effectiveness":    round(outputs.effectiveness, 4),
        "fouling_factor":   round(outputs.fouling_factor, 4),
        "active_faults":    active_faults,
    }


# ---------------------------------------------------------------------------
# CLI entry point — mirrors opcua_server.py's CLI for consistency
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse
    import asyncio
    import dataclasses

    import pandas as pd

    from simulator.faults import FaultInjector, FanFault, FoulingFault, HighConductivityFault
    from simulator.runner import RunConfig, _generate_process_inputs, _interpolate_weather
    from simulator.weather import QUEENSLAND_LOCATIONS, fetch_weather
    from simulator.cooling_tower import CoolingTowerSimulator

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s — %(message)s",
    )

    parser = argparse.ArgumentParser(description="Cooling Tower MQTT Publisher")
    parser.add_argument("--scenario", default="baseline",
                        choices=["baseline", "fouling", "fan_fault", "high_cond", "combined"])
    parser.add_argument("--start",    default="2023-01-01")
    parser.add_argument("--end",      default="2023-01-07")
    parser.add_argument("--location", default=DEFAULT_LOCATION,
                        choices=list(QUEENSLAND_LOCATIONS.keys()))
    parser.add_argument("--dt",       type=float, default=60.0)
    parser.add_argument("--speed",    type=float, default=60.0,
                        help="Speed factor: 60=1 Hz per step, 1=real-time")
    args = parser.parse_args()

    # Build injector
    injector = FaultInjector()
    if args.scenario == "fouling":
        injector.add(FoulingFault(start_time=0, rate_per_day=0.03))
    elif args.scenario == "fan_fault":
        injector.add(FanFault(start_time=3 * 86400, target_speed_pct=30.0, ramp_seconds=300))
    elif args.scenario == "high_cond":
        injector.add(HighConductivityFault(start_time=2 * 86400))
    elif args.scenario == "combined":
        injector.add(FoulingFault(start_time=0, rate_per_day=0.02))
        injector.add(FanFault(start_time=4 * 86400, target_speed_pct=50.0, ramp_seconds=600))

    config = RunConfig(start_date=args.start, end_date=args.end,
                       location=args.location, dt=args.dt)

    async def _run():
        loc = QUEENSLAND_LOCATIONS[config.location]
        weather = fetch_weather(config.start_date, config.end_date,
                                latitude=loc["latitude"], longitude=loc["longitude"],
                                label=loc["label"])
        sim_start = weather["timestamp"].iloc[0]
        sim_end   = weather["timestamp"].iloc[-1]
        timestamps = pd.date_range(start=sim_start, end=sim_end,
                                   freq=pd.Timedelta(seconds=config.dt), inclusive="left")
        t_amb_arr, t_wb_arr = _interpolate_weather(weather, timestamps)
        t_hot_arr, flow_arr = _generate_process_inputs(config, len(timestamps))
        sim = CoolingTowerSimulator(initial_cond_basin=config.initial_cond_basin,
                                    initial_fouling_factor=config.initial_fouling_factor)
        sleep_per_step = config.dt / args.speed

        with MQTTPublisher.from_env(location=config.location) as pub:
            for i, ts in enumerate(timestamps):
                sim_time = i * config.dt
                inputs = SimulatorInputs(
                    t_hot_in=t_hot_arr[i], water_flow_m3hr=flow_arr[i],
                    t_amb=t_amb_arr[i], t_wb=t_wb_arr[i],
                    fan_speed_pct=config.fan_speed_nominal,
                    cond_makeup=config.cond_makeup_nominal,
                )
                inputs = injector.apply(sim_time, inputs, sim)
                if inputs.t_wb >= inputs.t_hot_in:
                    inputs = SimulatorInputs(
                        **{**dataclasses.asdict(inputs), "t_wb": inputs.t_hot_in - 0.5})
                outputs = sim.step(config.dt, inputs)
                fault_label = ",".join(injector.active_labels) or "none"
                pub.publish(ts.to_pydatetime(), inputs, outputs, fault_label)
                log.info("[%s] T_cold=%.1f°C approach=%.1f°C faults=%s",
                         ts.strftime("%Y-%m-%d %H:%M"), outputs.t_cold_out,
                         outputs.t_approach, fault_label)
                await asyncio.sleep(sleep_per_step)

    asyncio.run(_run())
