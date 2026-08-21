# AIS Ship Traffic Density Mapping — Execution Plan

Region: LA/Long Beach · Data: MarineCadastre 2024 Broadcast Points · Stack: DuckDB (+h3 extension), Python, pooch, Parquet, Kepler.gl/pydeck

Rule of the game: after every **CHECKPOINT**, paste the requested output into our chat before moving on. That's how mistakes get caught while they're still cheap.

---

## Phase 0 — Setup (½ day)

**Task 0.1 — Repo skeleton.**
Create the repo `ais-density` with this layout:

```
ais-density/
├── config.yaml
├── Makefile
├── pyproject.toml          # managed by uv
├── uv.lock                 # committed — reproducibility guarantee
├── src/
│   ├── __init__.py
│   ├── config.py
│   ├── obs.py
│   ├── schema.py            # constants + assertions; pure logic separated from I/O
│   ├── download.py
│   ├── ingest.py
│   ├── grid.py
│   ├── classify.py
│   └── visualize.py
├── tests/
│   ├── __init__.py
│   └── test_schema.py       # first tests: check_schema verdicts + error diff
├── notebooks/              # exploration only, never pipeline logic
└── data/
    ├── raw/                # gitignored
    ├── interim/            # gitignored
    ├── processed/
    └── static/
```

Add `.gitignore` covering `data/raw/`, `data/interim/`, `*.pyc`, `.venv`. Commit immediately — first commit should be the empty skeleton, so your history tells the story.

**Task 0.2 — Environment (uv).**
`uv init` with Python 3.12, then `uv add duckdb pooch pyyaml pydantic polars pydeck folium loguru` (+ `uv add --dev ruff pytest mypy`). Commit both `pyproject.toml` and `uv.lock`. All execution goes through `uv run` (including in the Makefile) — no manual venv activation anywhere. Configure in `pyproject.toml`: `[tool.pytest.ini_options] testpaths = ["tests"]` and `[tool.mypy] strict = true` scoped to src. Add `make lint` (ruff + mypy) and `make test` targets. Typing policy: full hints, Literal/StrEnum for closed sets, Mapping over dict in signatures, Path over str; no ABCs unless multiple implementations exist (prefer Protocol).

**Task 0.3 — Verify DuckDB h3 + zstd.**
In a Python REPL: `INSTALL h3 FROM community; LOAD h3;` then run `SELECT h3_latlng_to_cell(33.7, -118.2, 8);`. Confirm it returns a cell ID.

**Task 0.4 — Place static inputs.**
Move your downloaded WPI GeoJSON to `data/static/wpi.geojson`. Open it and inspect 5–10 features that fall inside the LA box: what properties exist? Is there anything marking anchorages?

> **CHECKPOINT 0** — paste: (a) the h3 cell ID from task 0.3, (b) the property keys of one WPI feature and how many WPI ports fall in the LA bbox. I'll tell you whether WPI is Tier-2-capable (validation) or Tier-1-only (cosmetic) for your region.

---

## Phase 1 — Config + Download (1 day)

**Task 1.1 — Write `config.yaml`.**
Multi-region shape from the start:

```yaml
windows:                     # named analysis periods; run one now, add more later (S2)
  jul_2024:
    start: 2024-07-01
    end:   2024-07-07        # widen to 07-31 for the final run
h3:
  resolution: 8
regions:
  la_long_beach:
    bbox: [-118.80, 33.30, -117.80, 34.00]
```

Processed outputs use hive-style partitioning `data/processed/region=<r>/window=<w>/` (region and window become queryable columns via `hive_partitioning=true`); interim stays date-partitioned and window-agnostic. Stage scripts take `--region` and `--window`. Never blend windows in one aggregate.

**Task 1.2 — Write `config.py`.**
Pydantic models: `Region(name, bbox, h3_resolution)`, `Window(name, start, end)`, `Config(windows, regions, ...)`. Validate: bbox has 4 floats, min < max on both axes, dates parse and start ≤ end per window, window names are valid path segments. A `load_config()` function everything else imports. No other file reads YAML directly.

**Task 1.3 — Discover the real file URLs.**
Go to the 2024 "See File List" page and copy the actual URL pattern for daily files (verify extension: `.csv.zst` vs `.zip` — do not assume). Put the pattern in config under `source.base_url`.

**Task 1.4 — Logging + run ledger (before any stage code).**
`uv add loguru`. A tiny `src/obs.py`: configures loguru to stderr + `logs/{stage}_{ts}.log` (gitignore `logs/`), and exposes `record_run(stage, region, window, date, rows_in, rows_out, seconds)` appending to `data/run_ledger.csv`. Rules for every stage: log start/end with parameters, log every skip with its reason, log row counts at every filter boundary, write outputs to a temp name and rename on success (atomic — no partial partitions). Failures name the failing unit and continue where sensible (downloads), summarizing at the end.

**Task 1.5 — Write `download.py` with pooch.**
Build the file list from the config date range; `pooch.create` with `path="data/raw"`; registry with `None` hashes initially. Fetch loop. After first successful download, record the SHA256 pooch prints into a `manifest.json` (filename → hash, size, download date).
Common mistake to avoid: don't parallelize the downloads — sequential is polite to a government server and your bottleneck is bandwidth anyway.

**Task 1.6 — Download ONE day only.** Not seven. One.

> **CHECKPOINT 1** — paste: the exact URL used, the file size on disk, and the output of a DuckDB `DESCRIBE SELECT * FROM read_csv_auto('data/raw/<file>')`. I'll verify the schema matches the 2018–2024 dictionary before you write any ingest code against it.

---

## Phase 2 — Ingest (2 days)

**Task 2.1 — Explore before coding.**
In a notebook, one DuckDB query against the raw file: row count, min/max of LAT/LON/SOG, count of NULL MMSIs, distinct VesselType values with counts. Look at the numbers before deciding cleaning rules.

**Task 2.2 — Write the canonical schema map.**
A dict in `ingest.py`: both the 2018–2024 CamelCase names and the 2025 snake_case names → your canonical snake_case (`mmsi, ts, lat, lon, sog, cog, heading, vessel_type, length, width, draft`). Detect which schema a file has by its columns, apply the right map.

**Task 2.3 — Write `ingest.py`.**
Per day, one DuckDB `COPY (SELECT ... WHERE ...) TO parquet`:
- bbox filter from config (parameterized, never hardcoded)
- validity: lat ∈ [-90,90], lon ∈ [-180,180], sog ∈ [0,40], MMSI is 9 digits
- select only needed columns (drop VesselName, IMO, CallSign — you don't need identity, and it keeps files small)
- output: `data/interim/<region>/date=YYYY-MM-DD/part.parquet`
- **log per day: rows in raw → rows after bbox → rows after cleaning**, via `record_run` into `data/run_ledger.csv`. This becomes README material.
- idempotent: skip a day if its output partition already exists (unless `--force`)

**Task 2.4 — Run on your one day.** Sanity-check the output parquet: row count, and `SELECT MIN(lat), MAX(lat), MIN(lon), MAX(lon)` must be inside your bbox.

**Task 2.5 — Download and ingest the remaining 6 days.**

> **CHECKPOINT 2** — paste the ingest_stats table (7 rows: raw/bbox/clean counts per day) plus total interim size on disk. Red flags I'll look for: bbox retention wildly different across days, cleaning dropping >5% of bbox rows, or interim size suspiciously large (means you kept columns you shouldn't have).

---

## Phase 3 — Grid + Aggregate (2 days)

**Task 3.1 — Vessel-type bucketing.**
CASE expression mapping AIS codes → buckets: 70–79 cargo, 80–89 tanker, 30 fishing, 60–69 passenger, 31/32/52 tug/tow, else other/unknown. Put it in one place (a SQL snippet in `grid.py`).

**Task 3.2 — Write `grid.py`.**
One DuckDB query over the interim parquet glob for the region:
- `h3_latlng_to_cell(lat, lon, <res from config>)` as cell
- group by cell (and separately by cell × vessel bucket, and cell × hour-of-day)
- metrics per cell: `COUNT(DISTINCT mmsi)` (honest density), raw ping count, `MEDIAN(sog)`, `AVG(sog)`
- output: `data/processed/region=<r>/window=<w>/cells.parquet` (+ `cells_by_type.parquet`, `cells_by_hour.parquet`)

Common mistake to avoid: don't compute density from ping counts alone — reporting frequency varies by vessel and speed, which biases slow vessels upward. Unique-vessel count is the headline metric; keep ping count as a secondary column.

**Task 3.3 — Sanity queries.**
Top 20 cells by unique vessels — convert a few cell IDs back to lat/lng (`h3_cell_to_latlng`) and check on Google Maps that they're where you'd expect (port entrances, precautionary area). If your hottest cell is on land, something's wrong.

> **CHECKPOINT 3** — paste: total cell count at res 8, the top-5 cells with their lat/lng and metrics, and median sog for the top-5. I'll sanity-check the geography before you build classification on top of it.

---

## Phase 4 — Classification (1–2 days)

**Task 4.1 — Rule-based classifier in `classify.py`.**
Config-driven (`classify: {activity_quantile: 0.90, anchor_sog_max: 1.0, lane_sog_min: 6.0}`) — quantile *levels* in config, values computed at runtime from the input's own distribution (July actuals: P90 = 36 vessels overall). Rules on cells with `vessels ≥ q(activity_quantile)`:
- **anchored/moored**: median_sog < anchor_sog_max
- **shipping lane**: median_sog ≥ lane_sog_min
- **maneuvering/harbor**: in between
- below the activity gate: unclassified

Run the same rules on **two strata**: all traffic (`cells.parquet`) and commercial-only (cargo+tanker aggregated from `cells_by_type`, with its *own* quantiles). Commercial is the headline classification; all-traffic is context. Output `data/processed/region=<r>/window=<w>/cells_classified.parquet` with a `stratum` column; log per-class cell counts to the run ledger (sanity: any class <5 or >500 cells means mistuned thresholds). The berth-vs-anchorage split is out of scope for v1 — label both "anchored/moored" and note it in limitations.

**Task 4.2 — Validate against the three referees.**
(a) **Chart 18751 / NOAA ENC anchorage boxes**: overlay commercial anchored/moored cells on the charted anchorage areas — the spatial check. (b) **`status` cross-check**: per classified cell, share of pings with status 1 (anchor) / 5 (moored) — the behavioral check from inside the data; disagreement with kinematics is itself a finding. (c) WPI is Tier-1 markers only (±1.8 km point precision) — no distance claims.

**Task 4.3 — Download NOAA Transit Counts for 2024**, clip to your bbox, and do the visual comparison against your lane cells. One paragraph of findings.

> **CHECKPOINT 4** — paste: count of cells per class, and 2–3 sentences on how well anchorage/lane labels matched WPI + Transit Counts. If the match is poor we debug the rules together before you invest in visualization.

---

## Phase 5 — Visualization (2 days)

**Task 5.1 — `visualize.py` builds the multi-layer map** (pydeck H3HexagonLayer or Kepler; dark CARTO basemap, OpenSeaMap seamarks as a toggleable overlay):
Layer order follows the story — commercial first, raw density as context, drift finding as the closer:
- Layer 1 (default on): **commercial classification** — lane / anchored-moored / maneuvering as categorical colors. This is the hero layer.
- Layer 2: commercial density (unique vessels), log or quantile color scale — linear will make one hot cell wash out everything
- Layer 3: all-traffic density (the naïve view — marinas dominate; kept to tell the stratification story)
- Layer 4: median speed
- Layer 5: density by vessel bucket (toggleable)
- Layer 6: WPI port markers with decoded-attribute tooltips; anchorage (ACHARE) polygons as outlines
- Export: standalone HTML per region + 3–4 PNG screenshots (dark for the interactive, check README screenshots render on GitHub's white theme)

**Task 5.1b — Validation panel (rolled over from Checkpoint 4 — the still-owed overlay figure).** One static two-panel figure:
(a) classified commercial cells in class colors + ACHARE polygons + the four "neither" feature groups annotated (inner-harbor berths, El Segundo mooring, Anchorage-F/platform-belt holding, outer-basin drift smear);
(b) the NOAA 2024 Transit Counts raster, georeferenced (respect the geotransform via rasterio — no pixel axes), same extent.
This is the README's validation figure; caption states the log(1+transits) scale.

**Task 5.2 — The one insight pass.** Spend an hour just *looking* with the layers on. The findings list already banked: naïve-vs-stratified density arc, July 4th fleet analysis, the anchored-cells decomposition (berths / El Segundo / F-adjacent holding / drift signature). Add anything new the map surfaces (lane directionality, hour-of-day patterns) — then freeze the findings list for the README.

> **CHECKPOINT 5** — share: the hero (classification) layer screenshot, the naïve all-traffic density screenshot, and the validation panel. I'll review color scale, resolution readability, and whether the annotations carry the findings without the prose.

---

## Phase 6 — Scale-up + Packaging (2 days)

**Task 6.1 — Final run: widen config to 30 days** (all of July 2024), `make` from scratch on a clean `data/interim`, confirm the pipeline survives untouched. Record total runtime and peak memory (`/usr/bin/time -v` or Activity Monitor).

**Task 6.2 — Makefile** with the region loop we designed; `make all` must reproduce everything from an empty `data/` (minus raw downloads if cached).

**Task 6.3 — README**, in this order: hero screenshot → one-paragraph what/why → architecture diagram (the 5-stage flow) → data volumes processed (from the run ledger: "X million records → Y cells") → findings (naïve-vs-stratified density arc, the July 4th vessel-level analysis, plus map observations) → validation section (chart 18751/ENC + Transit Counts + status cross-check) → how to run → limitations (AIS gaps/spoofing, US-only, 1-min downsampling, single month, berth-vs-anchorage unsplit).

**Task 6.4 — Repo hygiene.** No notebooks with pipeline logic, no dead code, config documented, license file.

> **CHECKPOINT 6** — paste the README draft. Final review.

---

## Stretch goals (only after Checkpoint 6)

- **S1 — Second region** (Houston, res 9): should be pure YAML. If it needs code changes, that's a design bug worth fixing.
- **S2 — Temporal comparison**: January 2024 vs July 2024, one diff map + paragraph.
- **S3 — Streaming tail**: aisstream.io WebSocket (httpx-ws or websockets) → dlt pipeline → same parquet layout. Update the architecture diagram to show both ingestion paths.

---

## Standing rules (apply to every phase)

1. Commit at every task boundary with a message saying what works now.
2. Nothing reads YAML except `config.py`; nothing hardcodes the bbox, dates, or resolution.
3. Network I/O only in `download.py` / stream code; everything else reads local files.
4. Every stage is skip-if-output-exists, with `--force` to override.
5. When a number surprises you, stop and paste it into chat before "fixing" it.
