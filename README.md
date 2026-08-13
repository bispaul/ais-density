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

## Limitations

- **Berth vs anchorage not split (v1).** Both are labelled `anchored/moored`;
  distinguishing a berth from an anchorage is out of scope.
- **Commercial-stratum speed is approximate.** The commercial `median_sog` is a
  ping-weighted blend of the cargo and tanker per-category medians, since the
  exact combined median isn't recoverable from the aggregated grid.
- **`vessel_type` is self-reported.** AIS ship-type codes are operator-declared
  and may be wrong or missing (`~1%` are null/0).
