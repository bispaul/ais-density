-- 01_exploration.sql — AIS density exploration for the LA/Long Beach window.
-- Requires the DuckDB spatial extension for the WPI query:
--   INSTALL spatial; LOAD spatial;

-- ── Ports inside the region bbox ──────────────────────────────────────────
-- Sanity-check the bbox against NOAA's World Port Index: it should enclose a
-- real port complex. Result: 6 ports fall inside [-118.80,33.30,-117.80,34.00],
-- confirming the box covers the LA/Long Beach terminals (not open ocean).
SELECT FID,INDEX_NO,REGION_NO,
    PORT_NAME,
    COUNTRY,
    ST_X(geom) AS lon,
    ST_Y(geom) AS lat
FROM ST_Read('data/static/wpi.geojson')
WHERE ST_Intersects(
    geom,
    ST_MakeEnvelope(-118.80, 33.30, -117.80, 34.00)
);

-- region bbox, [min_lon, min_lat, max_lon, max_lat]:
-- [-118.80, 33.30, -117.80, 34.00]

-- ── Raw file quality profile (one day, US-wide, pre-filter) ────────────────
-- Profiles the untouched daily file to justify the ingest cleaning rules.
-- Results for 2024-07-01 (8,995,093 pings, 22,641 vessels):
--   * bad_mmsi = 0            → MMSI is clean; the 9-digit filter is cheap insurance
--   * max_sog  = 98.7 kn      → physically impossible; 1,172 pings exceed 40 kn
--                               (justifies the sog ∈ [0,40] validity bound)
--   * no_vtype = 90,116       → ~1% of rows have null/0 vessel_type
--   * cnt_vtype = 78          → 78 distinct AIS type codes present
--   * class_b_pings ≈ 2.96M   → ~33% of pings are Class B (recreational)
--   * cnt_transceiver = 2     → exactly A and B, as expected
--   * cnt_status = 15         → all 15 nav-status codes appear
-- NOTE: this is the whole-US raw file, not yet bbox-filtered.
select count(*) as rows
 , count (distinct mmsi) vessels
 , COUNT(*) FILTER (mmsi IS NULL) null_mmsi
 , COUNT(*) FILTER (mmsi < 100000000 OR mmsi > 999999999) AS bad_mmsi
 , min(longitude)
 , max(longitude)
 , min(latitude)
 , max(latitude)
 , sum(case when longitude is null then 1 else 0 end) null_lng
 , sum(case when latitude is null then 1 else 0 end) null_lat
 , min(sog)
 , max(sog)
 , sum(case when sog is null then 1 else 0 end) null_sog
 , COUNT(*) FILTER (sog > 40) sog_over_40
 , min(cog)
 , max(cog)
 , sum(case when cog is null then 1 else 0 end) null_cog
 , COUNT(*) FILTER (vessel_type IS NULL OR vessel_type = 0) AS no_vtype
 , count(distinct vessel_type) cnt_vessel_type
 , MIN(vessel_type) AS min_vtype
 , MAX(vessel_type) AS max_vtype
 , COUNT(*) FILTER (transceiver = 'B')
 , count(distinct transceiver) cnt_transceiver
 , count(distinct status) cnt_status
 , MIN(status) AS min_status
 , MAX(status) AS max_status
FROM read_csv_auto('data/raw/ais-2024-07-01.csv.zst');

-- ── Peek at the cleaned partitions ────────────────────────────────────────
-- All cleaned rows across every ingested day/region (bbox + validity applied).
SELECT *
FROM read_parquet('data/interim/region=*/date=*/part.parquet', hive_partitioning = true)
;

-- ── Class A vs B vessels per day (the holiday-traffic question) ────────────
-- Tests whether the ~16% rise into July 4th is commercial or recreational.
-- Distinct vessels per day:
--   class_a (commercial): 214 → 218 → 227 → 222 → 216 → 212 → 211   (flat)
--   class_b (recreation): 611 → 587 → 633 → 699 → 715 → 682 → 723   (+18% by 07-07)
--   total pings:          266k → 271k → 293k → 297k → 309k → 293k → 305k
-- INSIGHT: commercial fleet is essentially constant; the modest holiday bump is
-- driven by Class B recreational craft. Because Class B pings sparsely, even an
-- ~18% rise in recreational *vessels* only moves total *pings* ~16% — confirming
-- the "gently seasonal, recreational-led" mechanism rather than a 2x spike.
SELECT date,
       count(DISTINCT mmsi) FILTER (WHERE transceiver = 'B') AS class_b,
       count(DISTINCT mmsi) FILTER (WHERE transceiver = 'A') AS class_a
FROM read_parquet('data/interim/region=*/date=*/part.parquet', hive_partitioning = true)
GROUP BY date
ORDER BY date;

-- Same split, plus how many distinct vessel_type codes appear each day.
SELECT date,
       count(DISTINCT mmsi) FILTER (WHERE transceiver = 'B') AS class_b,
       count(DISTINCT mmsi) FILTER (WHERE transceiver = 'A') AS class_a,
       count(DISTINCT vessel_type) AS vessel_type_cnt
FROM read_parquet('data/interim/region=*/date=*/part.parquet', hive_partitioning = true)
GROUP BY date
ORDER BY date;

-- ── Composition by vessel_type ────────────────────────────────────────────
-- Which vessel types dominate, split by A/B, per day. Aggregated over the week
-- the top types (AIS codes) are:
--   37 Pleasure craft — 815 vessels, 901k pings  (recreational, most numerous)
--   36 Sailing        — 280 vessels, 262k pings
--   52 Tug            —  29 vessels, 133k pings   (few boats, dense pings)
--   31 Towing         —  25 vessels, 108k pings
--   60 Passenger      —  54 vessels, 107k pings
--   80 Tanker         —  37 vessels,  77k pings
--   30 Fishing        —  64 vessels,  73k pings
--   70 Cargo          —  54 vessels,  67k pings
-- INSIGHT: recreational types (37/36) dominate the vessel COUNT, while a handful
-- of commercial vessels (tugs/tankers/cargo) generate outsized ping density —
-- exactly why a density map weights toward the commercial channels.
SELECT date,vessel_type,
       count(DISTINCT mmsi) FILTER (WHERE transceiver = 'B') AS class_b,
       count(DISTINCT mmsi) FILTER (WHERE transceiver = 'A') AS class_a,
       count(DISTINCT mmsi) AS cnt
FROM read_parquet('data/interim/region=*/date=*/part.parquet', hive_partitioning = true)
GROUP BY date, vessel_type
ORDER BY date, cnt desc;

-- ── Composition with human-readable type labels ───────────────────────────
-- Joins vessel_type against the vendored ITU-R M.1371 lookup (sourced from the
-- pyais ShipType enum). LEFT JOIN so unknown/0 codes still show as 'Not available'.
SELECT vt.label AS vessel_type,
       count(DISTINCT p.mmsi) AS vessels,
       count(*)               AS pings
FROM read_parquet('data/interim/region=*/date=*/part.parquet', hive_partitioning = true) AS p
LEFT JOIN read_csv_auto('data/static/vessel_type_codes.csv') AS vt
       ON p.vessel_type = vt.code
GROUP BY vt.label
ORDER BY pings DESC;

-- ── Composition rolled up to the broad category ───────────────────────────
-- Groups by the tens-block category from the lookup. The COALESCE derives a
-- category by range for codes absent from the lookup (reserved slots like 8/39):
-- NULL vessel_type is genuinely 'Not available'; 1-19 are 'Reserved'.
WITH labelled AS (
  SELECT p.mmsi,
         coalesce(vt.category,
                  CASE
                    WHEN p.vessel_type IS NULL           THEN 'Not available'
                    WHEN p.vessel_type BETWEEN 1  AND 19 THEN 'Reserved'
                    WHEN p.vessel_type BETWEEN 20 AND 29 THEN 'WIG'
                    WHEN p.vessel_type BETWEEN 30 AND 39 THEN 'Special craft'
                    WHEN p.vessel_type BETWEEN 40 AND 49 THEN 'High-speed craft'
                    WHEN p.vessel_type BETWEEN 50 AND 59 THEN 'Special craft'
                    WHEN p.vessel_type BETWEEN 60 AND 69 THEN 'Passenger'
                    WHEN p.vessel_type BETWEEN 70 AND 79 THEN 'Cargo'
                    WHEN p.vessel_type BETWEEN 80 AND 89 THEN 'Tanker'
                    WHEN p.vessel_type BETWEEN 90 AND 99 THEN 'Other'
                    ELSE 'Unknown (code ' || p.vessel_type || ')'
                  END) AS category
  FROM read_parquet('data/interim/region=*/date=*/part.parquet', hive_partitioning = true) AS p
  LEFT JOIN read_csv_auto('data/static/vessel_type_codes.csv') AS vt
         ON p.vessel_type = vt.code
)
SELECT category,
       count(DISTINCT mmsi) AS vessels,
       count(*)             AS pings
FROM labelled
GROUP BY category
ORDER BY pings DESC;

-- ── Region rollup (region + date as Hive columns) ─────────────────────────
-- interim is partitioned region=<region>/date=<date>, so both are Hive columns.
-- hive_types casts the date partition to a real DATE for chronological sorting.
-- Result: la_long_beach, 7 days, 2,033,802 cleaned rows total.
SELECT
  region,
  date,
  count(*) AS rows_clean
FROM read_parquet('data/interim/region=*/date=*/part.parquet',
                  hive_partitioning = true,
                  hive_types = {'date': DATE})
GROUP BY region, date
ORDER BY date;

-- ══════════════════════════════════════════════════════════════════════════
-- H3 grid outputs, produced by src/grid.py. Partitioned by region and window:
--   data/processed/region=<region>/window=<window>/{cells,cells_by_type,cells_by_hour}.parquet
-- hive_partitioning = true exposes `region` and `window` as columns.
-- ══════════════════════════════════════════════════════════════════════════

-- ── cells.parquet: headline density per H3 cell ───────────────────────────
-- Top cells by unique-vessel count (the honest density metric). pings is a
-- secondary column — a berth/anchorage can have few vessels but many pings.
SELECT region, "window", cell_hex, vessels, pings,
       round(median_sog, 1) AS med_sog,
       round(avg_sog, 1)    AS avg_sog
FROM read_parquet('data/processed/region=*/window=*/cells.parquet',
                  hive_partitioning = true)
ORDER BY vessels DESC
LIMIT 10;

-- Grid totals per region-window. NB: sum(vessels) counts vessel-per-cell pairs,
-- not globally unique vessels (a vessel crossing cells is counted in each).
SELECT region, "window",
       count(*)     AS cells,
       sum(vessels) AS vessel_cell_pairs,
       sum(pings)   AS total_pings
FROM read_parquet('data/processed/region=*/window=*/cells.parquet',
                  hive_partitioning = true)
GROUP BY region, "window";

-- ── cells_by_type.parquet: composition by vessel category ─────────────────
-- Rolls the per-cell × category grid up to category totals.
SELECT category,
       count(*)                  AS cells,
       sum(pings)                AS pings,
       round(avg(median_sog), 1) AS med_sog
FROM read_parquet('data/processed/region=*/window=*/cells_by_type.parquet',
                  hive_partitioning = true)
GROUP BY category
ORDER BY pings DESC;

-- ── cells_by_hour.parquet: diurnal pattern ────────────────────────────────
-- Pings and active cells by hour-of-day (0-23) — when is the port busiest.
SELECT hour,
       sum(pings) AS pings,
       count(*)   AS active_cells
FROM read_parquet('data/processed/region=*/window=*/cells_by_hour.parquet',
                  hive_partitioning = true)
GROUP BY hour
ORDER BY hour;

-- ── Top cells with centroid lat/lng (requires: INSTALL h3; LOAD h3;) ───────
-- h3_cell_to_latlng returns [lat, lng]; index it (DuckDB arrays are 1-based).
SELECT region, "window", cell_hex, vessels, pings, median_sog, avg_sog,
       round(h3_cell_to_latlng(cell)[1], 5) AS lat,
       round(h3_cell_to_latlng(cell)[2], 5) AS lng
FROM read_parquet('data/processed/region=*/window=*/cells.parquet',
                  hive_partitioning = true)
ORDER BY vessels DESC
LIMIT 20;

DESCRIBE SELECT *
FROM read_parquet('data/processed/region=*/window=*/cells_by_type.parquet',
                  hive_partitioning = true);

SELECT region, "window", cell_hex, category, vessels, pings, median_sog, avg_sog,
       round(h3_cell_to_latlng(cell)[1], 5) AS lat,
       round(h3_cell_to_latlng(cell)[2], 5) AS lng
FROM read_parquet('data/processed/region=*/window=*/cells_by_type.parquet',
                  hive_partitioning = true)
WHERE category IN ('cargo', 'tanker')
ORDER BY category, vessels DESC
LIMIT 20;
