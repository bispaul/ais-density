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
FROM read_parquet('data/interim/*/date=*/part.parquet', hive_partitioning = true)
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
FROM read_parquet('data/interim/*/date=*/part.parquet', hive_partitioning = true)
GROUP BY date
ORDER BY date;

-- Same split, plus how many distinct vessel_type codes appear each day.
SELECT date,
       count(DISTINCT mmsi) FILTER (WHERE transceiver = 'B') AS class_b,
       count(DISTINCT mmsi) FILTER (WHERE transceiver = 'A') AS class_a,
       count(DISTINCT vessel_type) AS vessel_type_cnt
FROM read_parquet('data/interim/*/date=*/part.parquet', hive_partitioning = true)
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
FROM read_parquet('data/interim/*/date=*/part.parquet', hive_partitioning = true)
GROUP BY date, vessel_type
ORDER BY date, cnt desc;

-- ── Region rollup (region parsed from the path) ───────────────────────────
-- region isn't a Hive column (folder is a bare `la_long_beach/`, not
-- `region=...`), so recover it from the file path with filename = true.
-- hive_types casts the date partition to a real DATE for chronological sorting.
-- Result: la_long_beach, 7 days, 2,033,802 cleaned rows total.
SELECT
  regexp_extract(filename, 'interim/([^/]+)/', 1) AS region,
  date,
  count(*) AS rows_clean
FROM read_parquet('data/interim/*/date=*/part.parquet',
                  hive_partitioning = true,
                  hive_types = {'date': DATE},
                  filename = true)
GROUP BY region, date
ORDER BY date;
