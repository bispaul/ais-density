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

-- ── Top 20 cargo/tanker cells (per category) ──────────────────────────────
-- One row per (cell, category); a cell can appear once for Cargo and once for
-- Tanker. Ranked by unique vessels (honest density).
SELECT region, "window", category, cell_hex, vessels, pings,
       round(median_sog, 1) AS med_sog,
       round(avg_sog, 1)    AS avg_sog
FROM read_parquet('data/processed/region=*/window=*/cells_by_type.parquet',
                  hive_partitioning = true)
WHERE lower(category) IN ('cargo', 'tanker')
ORDER BY vessels DESC
LIMIT 20;

-- ── Top 20 cells by combined cargo+tanker vessels (one row per cell) ───────
SELECT cell_hex,
       sum(vessels) AS cargo_tanker_vessels,
       sum(pings)   AS pings
FROM read_parquet('data/processed/region=*/window=*/cells_by_type.parquet',
                  hive_partitioning = true)
WHERE lower(category) IN ('cargo', 'tanker')
GROUP BY cell_hex
ORDER BY cargo_tanker_vessels DESC
LIMIT 20;

SELECT region, "window", cell_hex, category, vessels, pings, median_sog, avg_sog,
       round(h3_cell_to_latlng(cell)[1], 5) AS lat,
       round(h3_cell_to_latlng(cell)[2], 5) AS lng
FROM read_parquet('data/processed/region=*/window=*/cells_by_type.parquet',
                  hive_partitioning = true)
WHERE category IN ('cargo', 'tanker')
ORDER BY category, vessels DESC
LIMIT 20;

-- ══════════════════════════════════════════════════════════════════════════
-- Referee B: behavioral cross-check of the classifier against AIS `status`.
-- status 1 = at anchor, 5 = moored (both stationary). Per commercial cell we
-- compute anchor_share = share of pings reporting status IN (1,5), straight
-- from the interim pings, then join it to the classifier's commercial labels.
-- FINDING: labels agree with behaviour — mean anchor_share is
-- anchored/moored 0.54 (n=127), maneuvering/harbor 0.25, shipping lane 0.01,
-- unclassified 0.01. Kinematics (speed) and the ships' own status declarations
-- point the same way — now on a solid sample after the two-gate (pings) fix.
-- ══════════════════════════════════════════════════════════════════════════

-- Per classified commercial cell, with its behavioral anchor_share attached.
WITH base AS (
  SELECT h3_latlng_to_cell(latitude, longitude, 8) AS cell,
         count(*) AS pings,
         sum(CASE WHEN status IN (1, 5) THEN 1 ELSE 0 END) AS at_rest_pings
  FROM read_parquet('data/interim/region=*/date=*/part.parquet',
                    hive_partitioning = true)
  WHERE vessel_type BETWEEN 70 AND 89          -- cargo + tanker (commercial)
  GROUP BY cell
),
from_raw AS (
  SELECT cell, at_rest_pings * 1.0 / pings AS anchor_share
  FROM base
),
classified AS (
  SELECT *
  FROM read_parquet('data/processed/region=*/window=*/cells_classified.parquet',
                    hive_partitioning = true)
  WHERE stratum = 'commercial'
)
SELECT c.cell_hex, c.label, c.vessels, c.median_sog, r.anchor_share
FROM classified c
LEFT JOIN from_raw r ON r.cell = c.cell
ORDER BY c.label, r.anchor_share DESC;

-- Verdict: mean/median anchor_share per label (a good classifier is monotonic —
-- high for anchored/moored, ~0 for shipping lane).
WITH base AS (
  SELECT h3_latlng_to_cell(latitude, longitude, 8) AS cell,
         count(*) AS pings,
         sum(CASE WHEN status IN (1, 5) THEN 1 ELSE 0 END) AS at_rest_pings
  FROM read_parquet('data/interim/region=*/date=*/part.parquet',
                    hive_partitioning = true)
  WHERE vessel_type BETWEEN 70 AND 89
  GROUP BY cell
),
from_raw AS (
  SELECT cell, at_rest_pings * 1.0 / pings AS anchor_share
  FROM base
),
classified AS (
  SELECT *
  FROM read_parquet('data/processed/region=*/window=*/cells_classified.parquet',
                    hive_partitioning = true)
  WHERE stratum = 'commercial'
)
SELECT c.label,
       count(*)                        AS cells,
       round(avg(r.anchor_share), 3)   AS mean_anchor_share,
       round(median(r.anchor_share), 3) AS median_anchor_share
FROM classified c
LEFT JOIN from_raw r ON r.cell = c.cell
GROUP BY c.label
ORDER BY mean_anchor_share DESC;

-- ── Gate boundary check: stationary commercial cells split at the pings-gate ──
-- median_sog < 1 (at rest) cells partition cleanly at the pings_gate (P90 = 77):
-- dense ones (>= gate) become anchored/moored; sparse ones (< gate) stay
-- unclassified — transient stops, not persistent anchorages. Result:
--   anchored/moored  127 cells, pings 77-9060 (avg 1243)
--   unclassified     104 cells, pings  3-76   (avg   44)
-- Confirms the pings-gate does real work: a razor-sharp split exactly at P90.
SELECT label,
       count(*)          AS cells,
       round(min(pings)) AS min_pings,
       round(max(pings)) AS max_pings,
       round(avg(pings)) AS avg_pings
FROM read_parquet('data/processed/region=*/window=*/cells_classified.parquet',
                  hive_partitioning = true)
WHERE stratum = 'commercial' AND median_sog < 1
GROUP BY label
ORDER BY cells DESC;

-- ══════════════════════════════════════════════════════════════════════════
-- Referee A: spatial cross-check of commercial anchored/moored cells against
-- charted NOAA ENC polygons (see README "Validation reference data" for curls).
-- Requires: INSTALL spatial; LOAD spatial; INSTALL h3 FROM community; LOAD h3;
-- Method: hex FOOTPRINT (h3_cell_to_boundary_wkt) ST_Intersects the polygons —
-- fairer than a centroid test for 0.86-km hexes against narrow boxes. Three-way:
--   charted anchorage (ACHARE harbour 186 ∪ approach 191)
--   berth/harbor      (dredged 228 ∪ wharves 138, buffered ~800 m)
--   neither           (residual)
-- FINDING: 34% land in charted anchorages (up from 25% with centroids/harbour-
-- only). Most of the rest are terminal berths in the inner harbor (33.74/-118.21,
-- 3k-9k pings) — the deferred berth-vs-anchorage case, NOT classifier error. The
-- berth bucket is undercounted because ENC features don't tile private terminal
-- basins (see limitations).
-- ══════════════════════════════════════════════════════════════════════════
WITH anchorage AS (
  SELECT geom FROM ST_Read('data/static/anchorages_la_long_beach.geojson')
  UNION ALL SELECT geom FROM ST_Read('data/static/anchorages_la_long_beach_approach.geojson')
),
harbor AS (
  SELECT ST_Buffer(geom, 0.008) AS geom FROM ST_Read('data/static/harbor_wharves_la_long_beach.geojson')
  UNION ALL SELECT ST_Buffer(geom, 0.008) AS geom FROM ST_Read('data/static/harbor_dredged_la_long_beach.geojson')
),
cells AS (
  SELECT ST_GeomFromText(h3_cell_to_boundary_wkt(cell)) AS hex
  FROM read_parquet('data/processed/region=*/window=*/cells_classified.parquet', hive_partitioning = true)
  WHERE stratum = 'commercial' AND label = 'anchored/moored'
),
tagged AS (
  SELECT EXISTS(SELECT 1 FROM anchorage a WHERE ST_Intersects(c.hex, a.geom)) AS in_anch,
         EXISTS(SELECT 1 FROM harbor h    WHERE ST_Intersects(c.hex, h.geom)) AS in_harbor
  FROM cells c
)
SELECT count(*)                                                    AS anchored,
       count(*) FILTER (WHERE in_anch)                             AS charted_anchorage,
       count(*) FILTER (WHERE NOT in_anch AND in_harbor)           AS berth_harbor,
       count(*) FILTER (WHERE NOT in_anch AND NOT in_harbor)       AS neither
FROM tagged;

-- ── Referee A residual: decompose the "neither" cells by H3 res-6 parent ─────
-- Elegant clustering with zero geometry: group the outside-charted anchored
-- cells by their ~36 km² res-6 parent. 65 cells span 21 parents (largest 10) —
-- dispersed, not one block. Reading cell-count × ping-intensity gives 4 signatures:
--   few cells / high pings  = true stationary features:
--     * inner-harbor berths      33.73/-118.21  avg ~3.1k  (ENC harbor polys miss them)
--     * El Segundo tanker moorings 33.90/-118.48 avg ~1.3k  (a mooring isn't an ACHARE)
--     * holding near Anchorage F / platform belt 33.60/-118.05 avg ~1.5k
--   many cells / low pings  = drift smear across the outer basin toward Catalina
--     * 10 cells @ ~120 pings — loitering under bare steerageway (post-2021 JIT queuing)
WITH anchorage AS (
  SELECT geom FROM ST_Read('data/static/anchorages_la_long_beach.geojson')
  UNION ALL SELECT geom FROM ST_Read('data/static/anchorages_la_long_beach_approach.geojson')
),
harbor AS (
  SELECT ST_Buffer(geom, 0.008) AS geom FROM ST_Read('data/static/harbor_wharves_la_long_beach.geojson')
  UNION ALL SELECT ST_Buffer(geom, 0.008) AS geom FROM ST_Read('data/static/harbor_dredged_la_long_beach.geojson')
),
cells AS (
  SELECT cell, pings, ST_GeomFromText(h3_cell_to_boundary_wkt(cell)) AS hex
  FROM read_parquet('data/processed/region=*/window=*/cells_classified.parquet', hive_partitioning = true)
  WHERE stratum = 'commercial' AND label = 'anchored/moored'
),
neither AS (
  SELECT h3_cell_to_parent(cell, 6) AS parent, pings
  FROM cells c
  WHERE NOT EXISTS (SELECT 1 FROM anchorage a WHERE ST_Intersects(c.hex, a.geom))
    AND NOT EXISTS (SELECT 1 FROM harbor   h WHERE ST_Intersects(c.hex, h.geom))
)
SELECT h3_h3_to_string(parent) AS parent,
       count(*)          AS cells,
       round(avg(pings)) AS avg_pings,
       round(h3_cell_to_latlng(parent)[1], 3) AS lat,
       round(h3_cell_to_latlng(parent)[2], 3) AS lng
FROM neither
GROUP BY parent
ORDER BY cells DESC;
