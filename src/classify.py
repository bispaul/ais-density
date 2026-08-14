import argparse
from pathlib import Path

import duckdb
from duckdb import DuckDBPyConnection
from loguru import logger

from src.config import ClassifyConfig, Config, load_config
from src.obs import ClassRecord, record_classes

PROCESSED_DIR = Path("data/processed")

# Classes produced by the activity gate; 'unclassified' cells sit below it.
_ACTIVE_LABELS = ("anchored/moored", "shipping lane", "maneuvering/harbor")
# A healthy class sits in this band; outside it the thresholds are likely mistuned.
_SANITY_MIN, _SANITY_MAX = 5, 500


# Anchored cells are dense in *pings* (few vessels, many reports) so they gate on
# the ping quantile; movement classes gate on the vessel quantile.
_CLASSIFY_CASE = (
    "CASE WHEN c.median_sog < $anchor_sog_max AND c.pings >= g.pings_gate "
    "THEN 'anchored/moored' "
    "WHEN c.median_sog >= $lane_sog_min AND c.vessels >= g.vessels_gate "
    "THEN 'shipping lane' "
    "WHEN c.vessels >= g.vessels_gate THEN 'maneuvering/harbor' "
    "ELSE 'unclassified' END"
)


def classify_window(
    con: DuckDBPyConnection,
    region: str,
    window_name: str,
    cfg: ClassifyConfig,
    *,
    force: bool = False,
    processed_dir: Path = PROCESSED_DIR,
) -> list[ClassRecord]:
    """Classify one region-window's cells across the all-traffic and commercial strata."""
    win_dir = processed_dir / f"region={region}" / f"window={window_name}"
    cells = win_dir / "cells.parquet"
    by_type = win_dir / "cells_by_type.parquet"
    out = win_dir / "cells_classified.parquet"

    if not cells.exists() or not by_type.exists():
        logger.warning("skip {}/{} — grid outputs missing", region, window_name)
        return []
    if out.exists() and not force:
        logger.info("skip {}/{} — classification exists", region, window_name)
        return []

    # Commercial median/avg sog is a ping-weighted blend of the cargo and tanker
    # per-category medians (the exact combined median isn't recoverable from the
    # aggregated grid) — see limitations.
    con.execute(
        "CREATE OR REPLACE TEMPORARY TABLE stratum_cells AS "
        "SELECT cell, cell_hex, vessels, pings, median_sog, avg_sog, 'all' AS stratum "
        f"FROM read_parquet('{cells}') "
        "UNION ALL "
        "SELECT cell, any_value(cell_hex), sum(vessels), sum(pings), "
        "sum(median_sog * pings) / sum(pings), "
        "sum(avg_sog * pings) / sum(pings), 'commercial' "
        f"FROM read_parquet('{by_type}') "
        "WHERE lower(category) IN ('cargo', 'tanker') GROUP BY cell, cell_hex"
    )

    # Activity gates: runtime quantiles of the input's own distribution, per stratum.
    # Config holds the levels (0.90); the values below hold only for this
    # stratum/region/window, so they are logged and written to the ledger to make
    # shifts across runs (e.g. 7-day vs 30-day) traceable.
    con.execute(
        "CREATE OR REPLACE TEMPORARY TABLE gates AS "
        "SELECT stratum, quantile_cont(vessels, $aq) AS vessels_gate, "
        "quantile_cont(pings, $pq) AS pings_gate, count(*) AS n_cells "
        "FROM stratum_cells GROUP BY stratum",
        {"aq": cfg.activity_quantile, "pq": cfg.anchor_ping_quantile},
    )
    gate_map: dict[str, tuple[float, float]] = {}
    for stratum, vessels_gate, pings_gate, n_cells in con.execute(
        "SELECT stratum, vessels_gate, pings_gate, n_cells FROM gates ORDER BY stratum"
    ).fetchall():
        gate_map[stratum] = (vessels_gate, pings_gate)
        logger.info(
            "{}/{} [{}] gates: vessels P{:.0f}={}, pings P{:.0f}={} (n={} cells)",
            region,
            window_name,
            stratum,
            cfg.activity_quantile * 100,
            round(vessels_gate),
            cfg.anchor_ping_quantile * 100,
            round(pings_gate),
            n_cells,
        )

    con.execute(
        "COPY (SELECT c.cell, c.cell_hex, c.stratum, c.vessels, c.pings, "
        "c.median_sog, c.avg_sog, "
        f"{_CLASSIFY_CASE} AS label "
        "FROM stratum_cells c JOIN gates g USING (stratum)) "
        f"TO '{out}' (FORMAT parquet)",
        {"anchor_sog_max": cfg.anchor_sog_max, "lane_sog_min": cfg.lane_sog_min},
    )

    rows = con.execute(
        f"SELECT stratum, label, count(*) FROM read_parquet('{out}') "
        "GROUP BY stratum, label ORDER BY stratum, label"
    ).fetchall()
    records = [
        ClassRecord(
            region=region,
            window=window_name,
            stratum=stratum,
            label=label,
            cells=cells_n,
            vessels_gate=gate_map[stratum][0],
            pings_gate=gate_map[stratum][1],
        )
        for stratum, label, cells_n in rows
    ]
    record_classes(records)

    for record in records:
        if record.label in _ACTIVE_LABELS and not (
            _SANITY_MIN <= record.cells <= _SANITY_MAX
        ):
            logger.warning(
                "{}/{} [{}] {} = {} cells — thresholds likely mistuned",
                region,
                window_name,
                record.stratum,
                record.label,
                record.cells,
            )

    commercial = sum(
        r.cells for r in records if r.stratum == "commercial" and r.label in _ACTIVE_LABELS
    )
    logger.info(
        "{}/{}: classified {} commercial cells -> {}",
        region,
        window_name,
        commercial,
        out,
    )
    return records


def classify_all(config: Config, *, force: bool = False) -> list[ClassRecord]:
    con = duckdb.connect()
    records: list[ClassRecord] = []
    for region in config.regions:
        for window_name in config.windows:
            records.extend(
                classify_window(con, region, window_name, config.classify, force=force)
            )
    return records


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Rule-based classification of H3 grid cells into activity classes."
    )
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--region", help="limit to a single region (default: all)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="reclassify even if the output already exists",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    if not args.region:
        classify_all(config, force=args.force)
        return

    con = duckdb.connect()
    for window_name in config.windows:
        classify_window(con, args.region, window_name, config.classify, force=args.force)


if __name__ == "__main__":
    main()
