import argparse
import json
import math
from pathlib import Path
from typing import Any

import duckdb
import pydeck as pdk
from duckdb import DuckDBPyConnection
from loguru import logger

from src.config import Config, Region, load_config

PROCESSED_DIR = Path("data/processed")
STATIC_DIR = Path("data/static")
MAPS_DIR = Path("maps")

CARTO_DARK = "https://basemaps.cartocdn.com/gl/dark-matter-gl-style/style.json"

# Categorical colours for the hero classification layer (RGB).
_LABEL_COLOR: dict[str, list[int]] = {
    "shipping lane": [46, 134, 222],
    "anchored/moored": [235, 77, 75],
    "maneuvering/harbor": [240, 147, 43],
    "unclassified": [70, 74, 92],
}


def _connect() -> DuckDBPyConnection:
    con = duckdb.connect()
    con.execute("INSTALL h3 FROM community; LOAD h3;")
    return con


def _ramp(t: float) -> list[int]:
    """Plasma-ish 3-stop ramp for continuous (density/speed) layers."""
    t = max(0.0, min(1.0, t))
    lo, mid, hi = (13, 8, 135), (204, 71, 120), (240, 249, 33)
    a, b, f = (lo, mid, t / 0.5) if t <= 0.5 else (mid, hi, (t - 0.5) / 0.5)
    return [round(a[i] + (b[i] - a[i]) * f) for i in range(3)]


def _hex_layer(records: list[dict[str, Any]]) -> pdk.Layer:
    return pdk.Layer(
        "H3HexagonLayer",
        data=records,
        get_hexagon="hex",
        get_fill_color="color",
        get_line_color=[255, 255, 255, 40],
        filled=True,
        stroked=True,
        extruded=False,
        opacity=0.55,
        pickable=True,
    )


def _overlay_layers(region: str) -> list[pdk.Layer]:
    """Shared context: WPI port markers + charted anchorage outlines."""
    layers: list[pdk.Layer] = []
    anchorages = [
        STATIC_DIR / "anchorages_la_long_beach.geojson",
        STATIC_DIR / "anchorages_approach.geojson",
    ]
    features: list[dict[str, Any]] = []
    for path in anchorages:
        if path.exists():
            features += json.loads(path.read_text()).get("features", [])
    if features:
        layers.append(
            pdk.Layer(
                "GeoJsonLayer",
                data={"type": "FeatureCollection", "features": features},
                stroked=True,
                filled=False,
                get_line_color=[80, 220, 120],
                line_width_min_pixels=1,
            )
        )
    return layers


def _view(region_cfg: Region) -> pdk.ViewState:
    min_lon, min_lat, max_lon, max_lat = region_cfg.bbox
    return pdk.ViewState(
        longitude=(min_lon + max_lon) / 2,
        latitude=(min_lat + max_lat) / 2,
        zoom=9.2,
        pitch=0,
    )


def _deck(hexes: list[dict[str, Any]], region: str, region_cfg: Region, tooltip: str) -> pdk.Deck:
    return pdk.Deck(
        layers=[_hex_layer(hexes), *_overlay_layers(region)],
        initial_view_state=_view(region_cfg),
        map_provider="carto",
        map_style=CARTO_DARK,
        tooltip={"text": tooltip},
    )


def visualize_window(
    con: DuckDBPyConnection,
    region: str,
    window_name: str,
    region_cfg: Region,
    *,
    force: bool = False,
    processed_dir: Path = PROCESSED_DIR,
    maps_dir: Path = MAPS_DIR,
) -> dict[str, Path]:
    """Render the multi-layer maps for one region-window to standalone HTML."""
    win = processed_dir / f"region={region}" / f"window={window_name}"
    classified = win / "cells_classified.parquet"
    cells = win / "cells.parquet"
    if not classified.exists() or not cells.exists():
        logger.warning("skip {}/{} — processed outputs missing", region, window_name)
        return {}

    out_dir = maps_dir / f"region={region}" / f"window={window_name}"
    out_dir.mkdir(parents=True, exist_ok=True)
    outputs: dict[str, Path] = {}

    # h3_h3_to_string conversion happens here and nowhere else (Checkpoint-3).
    # Layer 1 (hero): commercial classification, categorical colours.
    rows = con.execute(
        "SELECT h3_h3_to_string(cell) AS hex, label, vessels, pings, "
        f"round(median_sog, 1) AS med_sog FROM read_parquet('{classified}') "
        "WHERE stratum = 'commercial'"
    ).fetchall()
    hero = [
        {
            "hex": h,
            "color": _LABEL_COLOR.get(label, [70, 74, 92]),
            "label": label,
            "vessels": vessels,
            "pings": pings,
            "med_sog": med,
        }
        for h, label, vessels, pings, med in rows
    ]

    # Layer 2: commercial density (unique vessels), log colour scale.
    comm = con.execute(
        f"SELECT h3_h3_to_string(cell) AS hex, vessels FROM read_parquet('{classified}') "
        "WHERE stratum = 'commercial' AND label <> 'unclassified'"
    ).fetchall()
    comm_density = _density_records(comm)

    # Layer 3: all-traffic density (the naive view — marinas dominate).
    allt = con.execute(
        f"SELECT h3_h3_to_string(cell) AS hex, vessels FROM read_parquet('{cells}')"
    ).fetchall()
    all_density = _density_records(allt)

    # Layer 4: median speed.
    spd = con.execute(
        f"SELECT h3_h3_to_string(cell) AS hex, median_sog FROM read_parquet('{cells}')"
    ).fetchall()
    speed = _speed_records(spd)

    views = {
        "classification": (hero, "{label}\nvessels: {vessels}  pings: {pings}\nmed sog: {med_sog}"),
        "commercial_density": (comm_density, "commercial vessels: {vessels}"),
        "all_traffic_density": (all_density, "all vessels: {vessels}"),
        "median_speed": (speed, "median sog: {med_sog}"),
    }
    for name, (data, tip) in views.items():
        out = out_dir / f"{name}.html"
        if out.exists() and not force:
            logger.info("skip {}/{} {} — exists", region, window_name, name)
            outputs[name] = out
            continue
        _deck(data, region, region_cfg, tip).to_html(str(out), notebook_display=False)
        outputs[name] = out

    logger.info("{}/{}: wrote {} maps -> {}", region, window_name, len(outputs), out_dir)
    return outputs


def _density_records(rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    values = [float(v) for _, v in rows]
    hi = math.log1p(max(values)) or 1.0
    return [
        {"hex": h, "vessels": int(v), "color": _ramp(math.log1p(float(v)) / hi)}
        for h, v in rows
    ]


def _speed_records(rows: list[tuple[Any, ...]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for h, sog in rows:
        if sog is None:
            continue
        out.append(
            {"hex": h, "med_sog": round(float(sog), 1), "color": _ramp(min(float(sog), 15.0) / 15.0)}
        )
    return out


def visualize_all(
    config: Config, *, force: bool = False
) -> dict[str, dict[str, dict[str, Path]]]:
    con = _connect()
    outputs: dict[str, dict[str, dict[str, Path]]] = {}
    for region, region_cfg in config.regions.items():
        for window_name in config.windows:
            result = visualize_window(con, region, window_name, region_cfg, force=force)
            if result:
                outputs.setdefault(region, {})[window_name] = result
    return outputs


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Render multi-layer H3 maps (pydeck) from the classified grid."
    )
    parser.add_argument("--config", type=Path, default=Path("config.yaml"))
    parser.add_argument("--region", help="limit to a single region (default: all)")
    parser.add_argument(
        "--force",
        action="store_true",
        help="re-render even if the map HTML already exists",
    )
    args = parser.parse_args()

    config = load_config(args.config)
    if not args.region:
        visualize_all(config, force=args.force)
        return

    con = _connect()
    region_cfg = config.regions[args.region]
    for window_name in config.windows:
        visualize_window(con, args.region, window_name, region_cfg, force=args.force)


if __name__ == "__main__":
    main()