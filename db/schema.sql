-- =============================================================================
-- Cooling Tower Simulator — TimescaleDB Schema
-- =============================================================================
-- Apply with:
--   psql $DATABASE_URL -f db/schema.sql
--
-- Requires the TimescaleDB extension (CREATE EXTENSION IF NOT EXISTS timescaledb;
-- is handled here so the script is idempotent).
-- =============================================================================

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- -----------------------------------------------------------------------------
-- Simulation run registry
-- Tracks metadata for each run so readings can be grouped and filtered.
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS runs (
    run_id          SERIAL          PRIMARY KEY,
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT now(),
    label           TEXT,                        -- human-readable name, e.g. "baseline_jan_2023"
    location        TEXT            NOT NULL,    -- e.g. "rockhampton"
    start_date      DATE            NOT NULL,
    end_date        DATE            NOT NULL,
    dt_seconds      SMALLINT        NOT NULL,    -- simulation timestep
    notes           TEXT                         -- free-text description / fault scenario summary
);


-- -----------------------------------------------------------------------------
-- Main readings hypertable
-- One row per simulation timestep per run.
-- -----------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS tower_readings (
    time                TIMESTAMPTZ     NOT NULL,
    run_id              INTEGER         NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,

    -- -------------------------------------------------------------------------
    -- Inputs
    -- -------------------------------------------------------------------------
    t_hot_in            REAL            NOT NULL,   -- Hot water inlet temperature [°C]
    water_flow_m3hr     REAL            NOT NULL,   -- Water flow rate [m³/hr]
    t_amb               REAL            NOT NULL,   -- Ambient dry bulb temperature [°C]
    t_wb                REAL            NOT NULL,   -- Ambient wet bulb temperature [°C]
    fan_speed_pct       REAL            NOT NULL,   -- Fan speed [%]
    cond_makeup         REAL            NOT NULL,   -- Makeup water conductivity [µS/cm]

    -- -------------------------------------------------------------------------
    -- Outputs
    -- -------------------------------------------------------------------------
    t_cold_out          REAL            NOT NULL,   -- Cold water outlet temperature [°C]
    t_approach          REAL            NOT NULL,   -- Approach temperature = T_cold_out − T_wb [°C]
    t_range             REAL            NOT NULL,   -- Range = T_hot_in − T_cold_out [°C]
    evaporation_m3hr    REAL            NOT NULL,   -- Evaporation loss [m³/hr]
    blowdown_m3hr       REAL            NOT NULL,   -- Blowdown flow [m³/hr]
    makeup_m3hr         REAL            NOT NULL,   -- Total makeup water [m³/hr]
    coc                 REAL            NOT NULL,   -- Cycles of concentration [-]
    cond_basin          REAL            NOT NULL,   -- Basin conductivity [µS/cm]
    ntu                 REAL            NOT NULL,   -- Number of transfer units [-]
    effectiveness       REAL            NOT NULL,   -- Heat transfer effectiveness [-]
    fouling_factor      REAL            NOT NULL,   -- Packing condition [0–1]

    -- -------------------------------------------------------------------------
    -- Fault state
    -- -------------------------------------------------------------------------
    active_faults       TEXT            NOT NULL DEFAULT 'none'
                                        -- comma-separated active fault labels
);

SELECT create_hypertable(
    'tower_readings',
    'time',
    chunk_time_interval => INTERVAL '7 days',
    if_not_exists       => TRUE
);


-- -----------------------------------------------------------------------------
-- Indexes
-- run_id is the primary filter for most queries (isolate a single run).
-- t_approach is the key performance indicator queried most often in Grafana.
-- -----------------------------------------------------------------------------

CREATE INDEX IF NOT EXISTS idx_readings_run_time
    ON tower_readings (run_id, time DESC);

CREATE INDEX IF NOT EXISTS idx_readings_faults
    ON tower_readings (active_faults, time DESC)
    WHERE active_faults <> 'none';


-- -----------------------------------------------------------------------------
-- Compression
-- Compress chunks older than 30 days.  REAL columns compress very well with
-- TimescaleDB's gorilla/delta-delta encoding (~5–10× ratio on float series).
-- -----------------------------------------------------------------------------

ALTER TABLE tower_readings SET (
    timescaledb.compress,
    timescaledb.compress_orderby   = 'time DESC',
    timescaledb.compress_segmentby = 'run_id'
);

SELECT add_compression_policy(
    'tower_readings',
    INTERVAL '30 days',
    if_not_exists => TRUE
);


-- -----------------------------------------------------------------------------
-- Continuous aggregates — pre-computed hourly rollups
-- These power the Grafana "overview" panels without scanning the raw table.
-- -----------------------------------------------------------------------------

CREATE MATERIALIZED VIEW IF NOT EXISTS tower_readings_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', time)     AS bucket,
    run_id,
    AVG(t_cold_out)                 AS t_cold_out_avg,
    MIN(t_cold_out)                 AS t_cold_out_min,
    MAX(t_cold_out)                 AS t_cold_out_max,
    AVG(t_approach)                 AS t_approach_avg,
    MIN(t_approach)                 AS t_approach_min,
    AVG(t_range)                    AS t_range_avg,
    AVG(fan_speed_pct)              AS fan_speed_avg,
    AVG(ntu)                        AS ntu_avg,
    AVG(effectiveness)              AS effectiveness_avg,
    AVG(fouling_factor)             AS fouling_factor_avg,
    SUM(evaporation_m3hr) / 60.0   AS evaporation_m3,   -- m³ consumed in the hour
    SUM(blowdown_m3hr)    / 60.0   AS blowdown_m3,
    SUM(makeup_m3hr)      / 60.0   AS makeup_m3,
    AVG(coc)                        AS coc_avg,
    MAX(coc)                        AS coc_max,
    AVG(cond_basin)                 AS cond_basin_avg
FROM tower_readings
GROUP BY bucket, run_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'tower_readings_hourly',
    start_offset => INTERVAL '2 days',
    end_offset   => INTERVAL '1 hour',
    schedule_interval => INTERVAL '1 hour',
    if_not_exists => TRUE
);


-- -----------------------------------------------------------------------------
-- Daily rollup (built on top of the hourly aggregate for efficiency)
-- -----------------------------------------------------------------------------

CREATE MATERIALIZED VIEW IF NOT EXISTS tower_readings_daily
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 day', time)      AS bucket,
    run_id,
    AVG(t_cold_out)                 AS t_cold_out_avg,
    MIN(t_cold_out)                 AS t_cold_out_min,
    MAX(t_cold_out)                 AS t_cold_out_max,
    AVG(t_approach)                 AS t_approach_avg,
    MIN(t_approach)                 AS t_approach_min,
    AVG(fouling_factor)             AS fouling_factor_avg,
    SUM(evaporation_m3hr) / 60.0   AS evaporation_m3,
    SUM(makeup_m3hr)      / 60.0   AS makeup_m3,
    AVG(coc)                        AS coc_avg,
    MAX(coc)                        AS coc_max,
    MAX(cond_basin)                 AS cond_basin_peak
FROM tower_readings
GROUP BY bucket, run_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy(
    'tower_readings_daily',
    start_offset => INTERVAL '7 days',
    end_offset   => INTERVAL '1 day',
    schedule_interval => INTERVAL '1 day',
    if_not_exists => TRUE
);


-- -----------------------------------------------------------------------------
-- Useful views for quick inspection
-- -----------------------------------------------------------------------------

-- Latest reading per run
CREATE OR REPLACE VIEW latest_readings AS
SELECT DISTINCT ON (run_id)
    r.label,
    r.location,
    tr.*
FROM tower_readings tr
JOIN runs r USING (run_id)
ORDER BY run_id, time DESC;

-- Active fault events with duration
CREATE OR REPLACE VIEW fault_events AS
SELECT
    run_id,
    active_faults,
    MIN(time)                               AS fault_start,
    MAX(time)                               AS fault_end,
    MAX(time) - MIN(time)                   AS duration,
    AVG(t_cold_out)                         AS t_cold_out_avg_during,
    AVG(t_approach)                         AS t_approach_avg_during
FROM tower_readings
WHERE active_faults <> 'none'
GROUP BY run_id, active_faults
ORDER BY run_id, fault_start;
