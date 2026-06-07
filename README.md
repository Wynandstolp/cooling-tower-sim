# Cooling Tower Simulator

A physics-based cooling tower simulator in Python that feeds a modern industrial data stack. Built as a portfolio project demonstrating the ability to bridge OT (Operational Technology) and IT worlds.

## Why a cooling tower?

Cooling towers appear in power generation, chemicals, HVAC, mining, and refineries — they're one of the most common pieces of industrial equipment. The physics are well-understood, the failure modes are realistic, and the full signal chain (sensor → historian → dashboard) maps directly to real industrial data infrastructure.

The stack mirrors what you'd find in an actual plant:

| Layer | This project | Real-world equivalent |
|---|---|---|
| Physics engine | Python (`simulator/`) | Physical plant |
| Protocol | OPC-UA server (`asyncua`) | PLC / SCADA / historian |
| Transport | MQTT (Mosquitto + paho-mqtt) | Industrial IoT messaging |
| Storage | TimescaleDB (PostgreSQL + TimescaleDB extension) | PI Historian / InfluxDB |
| Visualisation | Grafana | AVEVA Insight / Seeq |

---

## Physics model

The simulator implements a **mechanical draft, counterflow cooling tower** using the NTU-effectiveness method.

### Heat transfer

```
NTU         = KaV/L × fouling_factor × (L/G)^-0.6
effectiveness = 1 - exp(-NTU)
T_cold_out  = T_hot_in - effectiveness × (T_hot_in - T_wb)
```

- `KaV/L = 1.8` — nominal tower characteristic for film-fill packing (Merkel, 1925)
- `L/G` — water-to-air mass flow ratio; air flow driven by fan: `G = G_design × (fan_speed / 100)^0.8`
- `fouling_factor` — degrades NTU from 1.0 toward 0.0 as packing fouls over time

### Evaporation

```
E = 0.00085 × L_m3hr × (T_hot_in - T_cold_out)    [m³/hr]
```

### Water quality and blowdown

```
CoC      = Cond_basin / Cond_makeup
Blowdown = E / (CoC_target - 1)    when CoC ≥ CoC_target (default 4.0)
Q_makeup = E + Blowdown + Drift
```

See `docs/model_parameters.md` for full parameter justification with literature references.

### Weather data

Ambient conditions (dry bulb, wet bulb) are fetched from the **Open-Meteo API** for real Queensland locations, giving genuine day/night cycles and seasonal variation. Wet bulb temperature is derived from dry bulb and dew point via the Stull approximation.

---

## Fault injection

Five injectable fault scenarios simulate real failure modes:

| Fault class | Mechanism | Real-world cause |
|---|---|---|
| `FoulingFault` | `fouling_factor` degrades linearly over days | Scale buildup on packing |
| `FanFault` | Fan speed ramps to a target % over configurable seconds | Motor or belt failure |
| `HighConductivityFault` | Blowdown valve closes, CoC climbs unchecked | Stuck blowdown valve |
| `LowFlowFault` | Water flow drops suddenly | Upstream pump issue |

Multiple faults can be stacked and each has configurable `start_time` and `end_time` (seconds from simulation epoch).

---

## Project structure

```
cooling-tower-sim/
├── simulator/
│   ├── cooling_tower.py      # CoolingTowerSimulator — core physics
│   ├── weather.py            # Open-Meteo weather fetcher + wet bulb calc
│   ├── faults.py             # Injectable fault scenarios
│   └── runner.py             # Time-stepping simulation loop
├── pipeline/
│   ├── timescale_writer.py   # Bulk-writes simulation output to TimescaleDB
│   ├── mqtt_publisher.py     # Publishes readings to MQTT broker
│   └── opcua_server.py       # Exposes simulator outputs via OPC-UA
├── db/
│   └── schema.sql            # TimescaleDB hypertable schema
├── dashboards/
│   └── grafana_dashboard.json
├── docs/
│   └── model_parameters.md   # Parameter reference with literature sources
├── tests/
├── docker-compose.yml
├── pyproject.toml
└── .env.example
```

---

## Getting started

### Prerequisites

- Python 3.11+
- Docker and Docker Compose

### 1. Clone and install

```bash
git clone <repo-url>
cd cooling-tower-sim
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### 2. Configure environment

```bash
cp .env.example .env
```

The defaults work for local Docker. Edit `.env` if you need custom credentials.

### 3. Start infrastructure

```bash
docker-compose up -d
```

This starts:
- **TimescaleDB** on `localhost:5432`
- **Mosquitto** (MQTT broker) on `localhost:1883`
- **Grafana** on `http://localhost:3000` (admin / admin)

### 4. Run a simulation and load data

```python
from simulator.runner import RunConfig, run
from pipeline.timescale_writer import TimescaleWriter

config = RunConfig(start_date="2025-01-01", end_date="2025-01-07")
df = run(config)

with TimescaleWriter.from_env() as writer:
    run_id = writer.write_run(df, config, label="baseline_week")
    print(f"run_id={run_id}, {len(df)} rows written")
```

### 5. Open Grafana

Go to `http://localhost:3000`, select the **Cooling Tower Simulator** dashboard, set the time range to match your simulation dates, and pick a run from the **Run** dropdown.

---

## Running scenarios

Each call to `write_run()` creates a new run ID that appears in the Grafana dropdown. This lets you compare scenarios side by side.

### Baseline

```python
config = RunConfig(start_date="2025-01-01", end_date="2025-01-31")
df = run(config)
```

### Fouling over 30 days

```python
from simulator.faults import FaultInjector, FoulingFault

injector = FaultInjector()
injector.add(FoulingFault(start_time=0, rate_per_day=0.03))

df = run(config, injector)
```

### Fan failure at day 3

```python
from simulator.faults import FaultInjector, FanFault

injector = FaultInjector()
injector.add(FanFault(start_time=3 * 86400, target_speed_pct=30.0, ramp_seconds=300))

df = run(config, injector)
```

### Combined: fouling + fan fault

```python
injector = FaultInjector()
injector.add(FoulingFault(start_time=0, rate_per_day=0.02))
injector.add(FanFault(start_time=10 * 86400, target_speed_pct=50.0, ramp_seconds=600))

df = run(config, injector)
```

---

## Running tests

```bash
pytest
```

---

## Key outputs

| Variable | Description | Typical range |
|---|---|---|
| `t_cold_out` | Cold water outlet temperature | 20–38°C |
| `t_approach` | `T_cold_out − T_wb` — primary performance metric | 3–8°C |
| `t_range` | `T_hot_in − T_cold_out` | 5–15°C |
| `fouling_factor` | Packing condition (1.0 = clean, →0 = fouled) | 0.5–1.0 |
| `coc` | Cycles of concentration — drives blowdown | 1–6 |
| `cond_basin` | Basin conductivity | 200–1500 µS/cm |
| `ntu` | Number of transfer units | 0.5–2.5 |

---

## Configuration reference

`RunConfig` controls all simulation parameters:

| Parameter | Default | Description |
|---|---|---|
| `start_date` | — | ISO date, e.g. `"2025-01-01"` |
| `end_date` | — | ISO date, inclusive |
| `dt` | `60.0` s | Simulation timestep |
| `location` | `"rockhampton"` | Queensland weather location |
| `t_hot_in_nominal` | `40.0°C` | Hot water inlet setpoint |
| `water_flow_nominal` | `150.0 m³/hr` | Water flow setpoint |
| `fan_speed_nominal` | `100.0%` | Fan speed |
| `seed` | `42` | RNG seed for reproducibility |

Available locations: `rockhampton`, `brisbane`, `townsville`, `cairns`, `mount_isa`.
