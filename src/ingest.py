import argparse
from collections.abc import Iterator
from datetime import date, timedelta
from pathlib import Path

import duckdb
from duckdb import DuckDBPyConnection
from loguru import logger

from src.config import Config, load_config
from src.obs import RunRecord, record_run
from src.schema import CANONICAL, LEGACY_MAP, SchemaKind, check_schema, probe_columns

RAW_DIR = Path("data/raw")
INTERIM_DIR = Path("data/interim")

# Physical domain bounds every kept fix must satisfy.
_VALIDITY = (
    "latitude BETWEEN -90 AND 90 "
    "AND longitude BETWEEN -180 AND 180 "
    "AND sog BETWEEN 0 AND 40 "
    "AND mmsi BETWEEN 100000000 AND 999999999"
)
# Parameterized so the bbox always comes from config, never hardcoded.
_BBOX = "longitude BETWEEN ? AND ? AND latitude BETWEEN ? AND ?"


def _select_expr(kind: SchemaKind) -> str:
    """Projection that renames source columns to canonical names, dropping identity."""
    if kind == "canonical":
        return ", ".join(CANONICAL)
    canon_to_raw = {canon: raw for raw, canon in LEGACY_MAP.items()}
    return ", ".join(f'"{canon_to_raw[c]}" AS {c}' for c in CANONICAL)


def _raw_path(raw_dir: Path, day: date) -> Path:
    return raw_dir / f"ais-{day:%Y-%m-%d}.csv.zst"


def _partition(interim_dir: Path, region: str, day: date) -> Path:
    return interim_dir / region / f"date={day:%Y-%m-%d}" / "part.parquet"


def _days(start: date, end: date) -> Iterator[date]:
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def ingest_day(
    con: DuckDBPyConnection,
    region: str,
    bbox: tuple[float, float, float, float],
    day: date,
    *,
    force: bool = False,
    raw_dir: Path = RAW_DIR,
    interim_dir: Path = INTERIM_DIR,
) -> RunRecord | None:
    """Filter one day of raw AIS to a cleaned parquet partition. Idempotent."""
    out = _partition(interim_dir, region, day)
    if out.exists() and not force:
        logger.info("skip {} {} — partition exists", region, day)
        return None

    raw = _raw_path(raw_dir, day)
    if not raw.exists():
        logger.warning("skip {} {} — raw file missing: {}", region, day, raw)
        return None

    kind = check_schema(probe_columns(con, str(raw)))
    cte = f"WITH src AS (SELECT {_select_expr(kind)} FROM read_csv_auto(?)) "

    min_lon, min_lat, max_lon, max_lat = bbox
    bbox_params = [min_lon, max_lon, min_lat, max_lat]

    row = con.execute(
        cte + "SELECT count(*), "
        f"count(*) FILTER (WHERE {_BBOX}), "
        f"count(*) FILTER (WHERE {_BBOX} AND {_VALIDITY}) FROM src",
        [str(raw), *bbox_params, *bbox_params],
    ).fetchone()
    assert row is not None
    rows_raw, rows_bbox, rows_clean = row

    out.parent.mkdir(parents=True, exist_ok=True)
    con.execute(
        f"COPY ({cte}SELECT * FROM src WHERE {_BBOX} AND {_VALIDITY}) "
        f"TO '{out}' (FORMAT parquet)",
        [str(raw), *bbox_params],
    )

    logger.info(
        "{} {}: raw={} bbox={} clean={} -> {}",
        region,
        day,
        rows_raw,
        rows_bbox,
        rows_clean,
        out,
    )
    record = RunRecord(
        region=region,
        date=day,
        rows_raw=rows_raw,
        rows_bbox=rows_bbox,
        rows_clean=rows_clean,
        output=out,
    )
    record_run(record)
    return record


def ingest_all(config: Config, *, force: bool = False) -> list[RunRecord]:
    con = duckdb.connect()
    records: list[RunRecord] = []
    for window in config.windows.values():
        for day in _days(window.start, window.end):
            for region, spec in config.regions.items():
                record = ingest_day(con, region, spec.bbox, day, force=force)
                if record is not None:
                    records.append(record)
    return records


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ingest raw AIS CSVs to cleaned parquet partitions."
    )
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-ingest days whose output partition already exists",
    )
    args = parser.parse_args()
    ingest_all(load_config(args.config), force=args.force)


if __name__ == "__main__":
    main()
