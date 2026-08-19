# ais-density

Vessel traffic density from raw AIS point data. Daily NOAA AIS files are
downloaded, filtered to a region and cleaned, then aggregated onto an H3 grid.

## Data source

Daily US AIS from NOAA's `csv2` collection (one Zstandard-compressed CSV per day):

```
https://noaaocm.blob.core.windows.net/ais/csv2/csv{YYYY}/ais-{YYYY-MM-DD}.csv.zst
```

Files land in `data/raw/` and their sha256 is tracked in `data/raw/registry.txt`
for local integrity checks.

## Setup

```bash
uv sync
```

## Configuration

Windows (date ranges) and regions (bounding boxes) are declared in `config.yaml`:

```yaml
windows:
  jul_2024:
    start: 2024-07-01
    end: 2024-07-07

h3:
  resolution: 8            # global default

regions:
  la_long_beach:
    bbox: [-118.80, 33.30, -117.80, 34.00]   # [min_lon, min_lat, max_lon, max_lat]
    # h3:
    #   resolution: 9      # optional per-region override
```

Any number of windows and regions can be added; each is validated on load
(date order, bbox `min < max`, H3 resolution `0–15`). See `src/config.py`.

## Pipeline

```bash
make download        # fetch raw files for all configured windows
make ingest          # filter + clean each day to parquet partitions
make grid            # aggregate cleaned pings onto an H3 grid, per window
make classify        # rule-based activity classes on the grid cells
make all             # download, ingest, grid, classify
```

Pass flags through `ARGS`:

```bash
make download ARGS=--force     # re-download even if the local hash matches
make ingest ARGS=--force       # re-ingest days whose partition already exists
```

### download (`src/download.py`)

Fetches each day's file via `pooch`, showing a per-file download progress bar.
A file is skipped when it already exists and its sha256 matches the registry;
a mismatch triggers a re-download. `--force` bypasses the cache.

### ingest (`src/ingest.py`)

Per day, a single DuckDB `COPY (SELECT ... WHERE ...) TO parquet`:

- **bbox filter** from config (parameterized, never hardcoded)
- **validity**: `lat ∈ [-90, 90]`, `lon ∈ [-180, 180]`, `sog ∈ [0, 40]`, 9-digit MMSI
- **column pruning**: keeps only the canonical schema, dropping identity
  columns (vessel name, IMO, call sign)
- **idempotent**: skips a day whose partition exists unless `--force`

Output partitions:

```
data/interim/region=<region>/date=YYYY-MM-DD/part.parquet
```

Each day's funnel (rows raw → after bbox → after cleaning) is appended to
`data/run_ledger.csv`.

### grid (`src/grid.py`)

Per region-window, one DuckDB pass over the interim partitions aggregates pings
onto an H3 grid (resolution from config). Writes three parquets under
`data/processed/region=<region>/window=<window>/`: `cells.parquet` (per cell),
`cells_by_type.parquet` (cell × vessel category), `cells_by_hour.parquet`
(cell × hour-of-day). Headline metric is `COUNT(DISTINCT mmsi)` (unique vessels);
ping count is kept as a secondary column. Idempotent unless `--force`.

### classify (`src/classify.py`)

Rule-based activity classes on the grid cells. Quantile *levels* come from config
(`classify.activity_quantile`); the vessel-count gate is computed at runtime from
the input's own distribution. Cells at or above the gate are labelled by
`median_sog`: `anchored/moored` (< `anchor_sog_max`), `shipping lane`
(≥ `lane_sog_min`), else `maneuvering/harbor`; cells below the gate are
`unclassified`. Run over two strata — all traffic (`cells.parquet`) and
commercial-only (cargo+tanker from `cells_by_type`, each with its own quantiles).
Writes `cells_classified.parquet` with a `stratum` column and logs per-class cell
counts to `data/class_ledger.csv`.

## Querying the output

```sql
-- one partition
SELECT * FROM 'data/interim/region=la_long_beach/date=2024-07-01/part.parquet';

-- all days/regions with the date partition as a column
SELECT * FROM read_parquet('data/interim/region=*/date=*/part.parquet', hive_partitioning = true);
```

## Development

```bash
make lint            # ruff check + mypy (strict, scoped to src/)
uv run pytest        # tests
```

## Validation reference data

Referee datasets used to sanity-check the classifier live in `data/static/`.

**Charted anchorage / harbor polygons (NOAA ENC via ENC Direct to GIS).**
Pulled from the ArcGIS REST service, clipped to the region bbox
`[-118.80, 33.30, -117.80, 34.00]`, and vendored as GeoJSON:

```bash
BASE="https://encdirect.noaa.gov/arcgis/rest/services/encdirect"
Q="query?geometry=-118.80,33.30,-117.80,34.00&geometryType=esriGeometryEnvelope&inSR=4326&spatialRel=esriSpatialRelIntersects&outFields=*&outSR=4326&f=geojson"

# Anchorage areas (S-57 ACHARE): harbour scale (layer 186) + approach scale (layer 191)
curl -s "$BASE/enc_harbour/MapServer/186/$Q" -o data/static/anchorages_la_long_beach.geojson
curl -s "$BASE/enc_approach/MapServer/191/$Q" -o data/static/anchorages_approach.geojson

# Harbor limits for the berth bucket: dredged basins (228) + wharves/shoreline (138)
curl -s "$BASE/enc_harbour/MapServer/228/$Q" -o data/static/harbor_dredged_la_long_beach.geojson
curl -s "$BASE/enc_harbour/MapServer/138/$Q" -o data/static/harbor_wharves_la_long_beach.geojson
```

Discover layer IDs by browsing `"$BASE/enc_harbour/MapServer/layers?f=json"`.

**NOAA AIS Vessel Transit Counts (Task 4.3).** The annual raster is a ~466 MB
BigTIFF — it won't open in macOS Preview (that's expected, not corruption) and is
**gitignored** (`data/static/*.tif`); read/clip it with GDAL/rasterio, not an
image viewer. Clip to the bbox and compare against the `shipping lane` cells:

```bash
uv run python - <<'PY'
import rasterio, duckdb, numpy as np
from rasterio.warp import transform, transform_bounds
from rasterio.windows import from_bounds

bbox = (-118.80, 33.30, -117.80, 34.00)
with rasterio.open('data/static/ais-transit-count-2024.tif') as r:
    win = from_bounds(*transform_bounds('EPSG:4326', r.crs, *bbox), transform=r.transform)
    data, prof = r.read(1, window=win), r.profile
    prof.update(height=data.shape[0], width=data.shape[1],
                transform=r.window_transform(win), compress='lzw')
    with rasterio.open('data/static/transit_2024_la_long_beach.tif', 'w', **prof) as dst:
        dst.write(data, 1)

con = duckdb.connect(); con.execute('INSTALL h3 FROM community; LOAD h3;')
rows = con.execute("SELECT label, h3_cell_to_latlng(cell)[1], h3_cell_to_latlng(cell)[2] "
    "FROM read_parquet('data/processed/region=*/window=*/cells_classified.parquet', "
    "hive_partitioning=true) WHERE stratum='commercial'").fetchall()
lab = np.array([x[0] for x in rows])
with rasterio.open('data/static/transit_2024_la_long_beach.tif') as r:
    xs, ys = transform('EPSG:4326', r.crs, [x[2] for x in rows], [x[1] for x in rows])
    v = np.array([s[0] for s in r.sample(list(zip(xs, ys)))], float)
for k in ['maneuvering/harbor','shipping lane','anchored/moored','unclassified']:
    print(f'{k:20s} n={int((lab==k).sum()):4d} median_transit={int(np.median(v[lab==k]))}')
PY
```

**Finding.** Sampling NOAA's 2024 transit-count raster (100 m, EPSG:3857) at each
commercial cell centroid, the labels line up with independently-measured traffic:
`maneuvering/harbor` median 2,424, `shipping lane` 298, `anchored/moored` 66,
`unclassified` 30 (raster background p50=22, p90=167). Lane cells sit at ~10× the
`unclassified` background and above the raster's own p90, confirming they fall on
genuine NOAA traffic corridors; the harbor-entrance cells carry the highest
throughput (all traffic funnels through, at low speed). This is a third
independent corroboration alongside the `status` (Referee B) and ENC anchorage
(Referee A) checks.

## Limitations

- **Berth vs anchorage not split (v1).** Both are labelled `anchored/moored`;
  distinguishing a berth from an anchorage is out of scope.
- **Commercial-stratum speed is approximate.** The commercial `median_sog` is a
  ping-weighted blend of the cargo and tanker per-category medians, since the
  exact combined median isn't recoverable from the aggregated grid.
- **`vessel_type` is self-reported.** AIS ship-type codes are operator-declared
  and may be wrong or missing (`~1%` are null/0).
- **ENC harbor polygons don't tile private terminal basins.** In the Referee A
  spatial check, moored ships at LA/Long Beach terminal berths fall outside the
  charted anchorage/dredged/wharf polygons, so the "inside charted" share
  (~34%) undercounts berths — a data-coverage gap, not a classifier error.
