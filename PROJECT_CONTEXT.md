# PROJECT_CONTEXT.md — Cooling Tower Simulator

## Purpose

This file provides full context for continuing development of the cooling tower simulation project. It should be read at the start of every Claude Code session before writing any code.

---

## Project goal

Build a physics-based cooling tower simulator in Python that feeds a modern industrial data stack. The end goal is a **portfolio project** demonstrating the ability to bridge OT (Operational Technology) and IT worlds — targeting roles as an industrial data engineer or OT/IT integration engineer.

The project is being built by a software engineer (~8 years experience, Ruby/Rails/AWS background) with a Chemical/Process Engineering degree, actively pivoting toward Python and industrial data roles.

---

## Why this project

- Cooling towers appear universally across power generation, chemicals, HVAC, mining, and refineries — immediately relatable to any industrial employer
- Rich variable set with real failure modes (Legionella risk, fouling, scaling) makes monitoring genuinely interesting
- Physics-based simulation signals engineering credibility beyond a generic data engineering project
- The full stack (simulator → OPC-UA → MQTT → TimescaleDB → Grafana) maps directly to real industrial data infrastructure

---

## System description

**Mechanical draft, counterflow cooling tower (induced draft)**

Hot process water enters the top, falls through packing, exchanges heat with air drawn upward by a fan at the top. Cooled water collects in the basin and returns to process.

---

## Variables

### Inputs (time-varying)
| Variable | Symbol | Typical range |
|---|---|---|
| Hot water inlet temperature | T_hot_in | 35–45°C |
| Water flow rate | L | 50–200 m³/hr |
| Ambient dry bulb temperature | T_amb | 15–35°C |
| Ambient wet bulb temperature | T_wb | 10–28°C |
| Fan speed | N_fan | 0–100% |
| Makeup water conductivity | Cond_makeup | 100–300 µS/cm |

### Outputs (calculated)
| Variable | Symbol | Notes |
|---|---|---|
| Cold water outlet temperature | T_cold_out | Key performance metric |
| Approach temperature | T_approach | T_cold_out − T_wb (target: 3–8°C) |
| Range | T_range | T_hot_in − T_cold_out |
| Evaporation rate | E | ~1% of flow per 5.5°C range |
| Cycles of concentration | CoC | Cond_basin / Cond_makeup |
| Basin conductivity | Cond_basin | Drives blowdown logic |
| Makeup water flow | Q_makeup | Replaces evaporation + blowdown |

---

## Core physics

### 1. Heat balance — NTU-effectiveness method

```
NTU = KaV/L × (L/G)^-0.6        # Tower characteristic, degraded by fouling
effectiveness = 1 - exp(-NTU)    # Simplified single-stream approximation
T_cold_out = T_hot_in - effectiveness × (T_hot_in - T_wb)
```

- `KaV/L` is the nominal tower characteristic (function of packing geometry)
- `G` is air mass flow rate, driven by fan speed: `G = G_design × (fan_speed_pct / 100)^0.8`
- `fouling_factor` (0–1) degrades NTU over time to simulate packing fouling

### 2. Evaporation loss

```
E = 0.00085 × L × (T_hot_in - T_cold_out)    [m³/hr]
```

### 3. Blowdown logic

```
CoC = Cond_basin / Cond_makeup
Blowdown = E / (CoC_target - 1)    # Triggered when CoC > CoC_target
```

### 4. Makeup water

```
Q_makeup = E + Blowdown + Drift
```

---

## Anomaly scenarios

These fault modes should be injectable at simulation runtime:

| Scenario | Mechanism | Real-world cause |
|---|---|---|
| Fouling | `fouling_factor` degrades slowly over days | Scale buildup on packing |
| Fan fault | Fan speed drops suddenly | Motor/belt failure |
| High conductivity | CoC climbs, blowdown valve stuck closed | Makeup water valve fault |
| Hot weather spike | T_wb rises, approach narrows | Ambient conditions |
| Low flow | L drops suddenly, range increases | Upstream pump issue |

---

## Driving inputs

Ambient conditions (T_amb, T_wb) should be driven from **real historical weather data** — Bureau of Meteorology (BOM) free data for a Queensland location. This gives genuine day/night cycles and seasonal variation.

Process inputs (T_hot_in, L) should vary realistically around a setpoint with slow drift and occasional step changes.

---

## Target stack

| Layer | Technology | Real-world equivalent |
|---|---|---|
| Simulator | Python (this project) | Physical plant |
| Data source protocol | asyncua (OPC-UA server) | PLC / SCADA / historian |
| Transport | MQTT (mosquitto + paho-mqtt) | Industrial IoT messaging |
| Storage | TimescaleDB (Postgres extension) | PI Historian / InfluxDB |
| Processing | Python (pandas / polars) | Seeq / custom pipelines |
| Visualisation | Grafana | AVEVA Insight / Seeq |

---

## Recommended project structure

```
cooling-tower-sim/
├── PROJECT_CONTEXT.md          # This file
├── README.md                   # Portfolio-facing description
├── pyproject.toml              # Dependencies
├── .python-version             # pyenv (3.11.x)
├── simulator/
│   ├── __init__.py
│   ├── cooling_tower.py        # CoolingTowerSimulator class
│   ├── weather.py              # BOM data loader + wet bulb calc
│   ├── faults.py               # Injectable fault scenarios
│   └── runner.py               # Main simulation loop
├── pipeline/
│   ├── opcua_server.py         # Exposes simulator outputs via OPC-UA
│   ├── mqtt_publisher.py       # Publishes to MQTT broker
│   └── timescale_writer.py     # Writes to TimescaleDB
├── db/
│   └── schema.sql              # TimescaleDB hypertable setup
├── dashboards/
│   └── grafana_dashboard.json  # Exportable Grafana config
├── tests/
│   └── test_cooling_tower.py   # Unit tests for physics equations
└── data/
    └── weather_cache/          # Cached Open-Meteo weather data (Parquet)
```

---

## Build order

1. `simulator/cooling_tower.py` — core physics, standalone, fully testable
2. `simulator/weather.py` — BOM data loader, wet bulb calculation
3. `simulator/faults.py` — fault injection logic
4. `simulator/runner.py` — ties simulator + weather + faults into a time-stepping loop
5. `db/schema.sql` — TimescaleDB schema
6. `pipeline/timescale_writer.py` — write simulation output to DB
7. `pipeline/opcua_server.py` — expose via OPC-UA
8. `pipeline/mqtt_publisher.py` — MQTT transport
9. Grafana dashboard setup

**Start with step 1. Get the physics right and tested before touching infrastructure.**

---

## Session prompt for Claude Code

When starting a new session, paste this:

> "I'm building a physics-based cooling tower simulator in Python as a portfolio project targeting industrial data engineering roles. Read PROJECT_CONTEXT.md in the repo root before we start. We're building in the order specified in the build order section. Tell me where we're up to based on what files exist, then continue from there."