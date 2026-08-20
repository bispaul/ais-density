import argparse
from datetime import timedelta
from pathlib import Path

import duckdb
from duckdb import DuckDBPyConnection
from loguru import logger

from src.config import Config, Window, load_config

INTERIM_DIR = Path("data/interim")
PROCESSED_DIR = Path("data/processed")
CATEGORY_CSV = "data/static/vessel_type_codes.csv"

# Unique-vessel count is the honest density headline; ping count is secondary
# because reporting frequency varies by vessel and speed.
_METRICS = (
    "count(DISTINCT mmsi) AS vessels, "
    "count(*) AS pings, "
    "median(sog) AS median_sog, "
    "avg(sog) AS avg_sog"
)

# Broad ITU category, falling back to the tens-block for codes absent from the
# lookup (reserved slots); NULL vessel_type is genuinely 'Not available'.
_CATEGORY_EXPR = """coalesce(vt.category, CASE
    WHEN src.vessel_type IS NULL           THEN 'Not available'
    WHEN src.vessel_type BETWEEN 1  AND 19 THEN 'Reserved'
    WHEN src.vessel_type BETWEEN 20 AND 29 THEN 'WIG'
    WHEN src.vessel_type BETWEEN 30 AND 39 THEN 'Special craft'
    WHEN src.vessel_type BETWEEN 40 AND 49 THEN 'High-speed craft'
    WHEN src.vessel_type BETWEEN 50 AND 59 THEN 'Special craft'
    WHEN src.vessel_type BETWEEN 60 AND 69 THEN 'Passenger'
    WHEN src.vessel_type BETWEEN 70 AND 79 THEN 'Cargo'
    WHEN src.vessel_type BETWEEN 80 AND 89 THEN 'Tanker'
    WHEN src.vessel_type BETWEEN 90 AND 99 THEN 'Other'
    ELSE 'Unknown' END)"""


def _window_paths(interim_dir: Path, region: str, window: Window) -> list[str]:
    """Interim partitions for a window's date range that actually exist on disk."""
    paths: list[str] = []
    day = window.start
    while day <= window.end:
        part = interim_dir / f"region={region}" / f"date={day:%Y-%m-%d}" / "part.parquet"
        if part.exists():
            paths.append(str(part))
        day += timedelta(days=1)
    return paths


def _base_cte(paths: list[str], resolution: int) -> str:
    files = ", ".join(f"'{p}'" for p in paths)
    return (
        "WITH src AS ("
        f"SELECT h3_latlng_to_cell(latitude, longitude, {resolution}) AS cell, "
        "mmsi, sog, vessel_type, hour(base_date_time) AS hour "
        f"FROM read_parquet([{files}]))"
    )


def grid_window(
    con: DuckDBPyConnection,
    region: str,
    window_name: str,
    window: Window,
    resolution: int,
    *,
    force: bool = False,
    interim_dir: Path = INTERIM_DIR,
    processed_dir: Path = PROCESSED_DIR,
) -> dict[str, Path]:
    """Aggregate one region-window onto an H3 grid, writing three parquets."""
    paths = _window_paths(interim_dir, region, window)
    if not paths:
        logger.warning("skip {}/{} — no interim data for window", region, window_name)
        return {}

    out_dir = processed_dir / f"region={region}" / f"window={window_name}"
    out_dir.mkdir(parents=True, exist_ok=True)

    cells = out_dir / "cells.parquet"
    by_type = out_dir / "cells_by_type.parquet"
    by_hour = out_dir / "cells_by_hour.parquet"

    outputs = {"cells": cells, "cells_by_type": by_type, "cells_by_hour": by_hour}
    if not force and all(p.exists() for p in outputs.values()):
        logger.info("skip {}/{} — grid outputs exist", region, window_name)
        return outputs

    base = _base_cte(paths, resolution)
    con.execute(
        f"COPY ({base} "
        f"SELECT cell, h3_h3_to_string(cell) AS cell_hex, {_METRICS} "
        f"FROM src GROUP BY cell) TO '{cells}' (FORMAT parquet)"
    )
    con.execute(
        f"COPY ({base}, cat AS ("
        "SELECT src.cell AS cell, src.mmsi AS mmsi, src.sog AS sog, "
        f"{_CATEGORY_EXPR} AS category "
        f"FROM src LEFT JOIN read_csv_auto('{CATEGORY_CSV}') AS vt "
        "ON src.vessel_type = vt.code) "
        f"SELECT cell, h3_h3_to_string(cell) AS cell_hex, category, {_METRICS} "
        f"FROM cat GROUP BY cell, category) TO '{by_type}' (FORMAT parquet)"
    )
    con.execute(
        f"COPY ({base} "
        f"SELECT cell, h3_h3_to_string(cell) AS cell_hex, hour, {_METRICS} "
        f"FROM src GROUP BY cell, hour) TO '{by_hour}' (FORMAT parquet)"
    )

    n_cells = con.execute(f"SELECT count(*) FROM read_parquet('{cells}')").fetchone()
    assert n_cells is not None
    logger.info(
        "{}/{} res={}: {} cells -> {}",
        region,
        window_name,
        resolution,
        n_cells[0],
        out_dir,
    )
    return outputs


def _connect() -> DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL h3 FROM community;")
    con.execute("LOAD h3;")
    return con


def grid_all(
    config: Config, *, force: bool = False
) -> dict[str, dict[str, dict[str, Path]]]:
    con = _connect()
    outputs: dict[str, dict[str, dict[str, Path]]] = {}
    for region in config.regions:
        resolution = config.resolution_for(region)
        for window_name, window in config.windows.items():
            result = grid_window(
                con, region, window_name, window, resolution, force=force
            )
            if result:
                outputs.setdefault(region, {})[window_name] = result
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Aggregate cleaned AIS partitions onto an H3 grid, per window."
    )
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--region", help="limit to a single region (default: all)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="recompute even if the window's grid outputs already exist",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    if not args.region:
        grid_all(config, force=args.force)
        return

    con = _connect()
    resolution = config.resolution_for(args.region)
    for window_name, window in config.windows.items():
        grid_window(con, args.region, window_name, window, resolution, force=args.force)


if __name__ == "__main__":
    main()
