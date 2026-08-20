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

# Okabe-Ito colourblind-safe categorical palette for the hero layer (RGB).
_LABEL_COLOR: dict[str, list[int]] = {
    "shipping lane": [86, 180, 233],  # sky blue
    "anchored/moored": [213, 94, 0],  # vermillion
    "maneuvering/harbor": [240, 228, 66],  # yellow
}

_ACHARE_COLOR = [80, 220, 120]
# Sequential ramp mirroring _ramp() stops, for the density/speed legend bars.
_RAMP_CSS = "linear-gradient(90deg, rgb(13,8,135), rgb(204,71,120), rgb(240,249,33))"
_TOOLTIP_STYLE = {
    "backgroundColor": "#11141c",
    "color": "#e8eaf0",
    "fontSize": "12px",
    "fontFamily": "-apple-system, Segoe UI, Roboto, sans-serif",
    "borderRadius": "6px",
    "padding": "6px 9px",
    "boxShadow": "0 2px 8px rgba(0,0,0,0.4)",
}

DOCS_IMG = Path("docs/img")
TRANSIT_TIF = STATIC_DIR / "transit_2024_la_long_beach.tif"

# The four "neither" residual feature groups from the Referee-A decomposition.
_NEITHER_FEATURES: list[tuple[float, float, str]] = [
    (-118.21, 33.73, "Inner-harbor berths"),
    (-118.48, 33.90, "El Segundo mooring"),
    (-118.05, 33.60, "Anchorage-F / platform belt"),
    (-118.33, 33.47, "Outer-basin drift smear"),
]


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


def _hex_layer(records: list[dict[str, Any]], *, stroked: bool = True) -> pdk.Layer:
    return pdk.Layer(
        "H3HexagonLayer",
        data=records,
        get_hexagon="hex",
        get_fill_color="color",
        get_line_color=[255, 255, 255, 40],
        filled=True,
        stroked=stroked,
        extruded=False,
        opacity=0.65,
        pickable=True,
        auto_highlight=True,
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
                get_line_color=_ACHARE_COLOR,
                line_width_min_pixels=2,
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


def _deck(
    hexes: list[dict[str, Any]],
    region: str,
    region_cfg: Region,
    tooltip: str,
    *,
    stroked: bool = True,
) -> pdk.Deck:
    return pdk.Deck(
        layers=[_hex_layer(hexes, stroked=stroked), *_overlay_layers(region)],
        initial_view_state=_view(region_cfg),
        map_provider="carto",
        map_style=CARTO_DARK,
        tooltip={"html": tooltip, "style": _TOOLTIP_STYLE},
    )


def _legend_categorical() -> str:
    items = (
        ("Shipping lane", _LABEL_COLOR["shipping lane"]),
        ("Anchored / moored", _LABEL_COLOR["anchored/moored"]),
        ("Maneuvering / harbor", _LABEL_COLOR["maneuvering/harbor"]),
    )
    rows = "".join(
        f'<div class="ais-row"><span class="ais-swatch" '
        f'style="background:rgb({c[0]},{c[1]},{c[2]})"></span>{name}</div>'
        for name, c in items
    )
    achare = (
        '<div class="ais-row"><span class="ais-swatch" style="background:transparent;'
        f'border:2px solid rgb({_ACHARE_COLOR[0]},{_ACHARE_COLOR[1]},{_ACHARE_COLOR[2]})">'
        "</span>Charted anchorage (ACHARE)</div>"
    )
    return rows + achare


def _legend_gradient(lo: str, hi: str) -> str:
    return (
        f'<div class="ais-bar" style="background:{_RAMP_CSS}"></div>'
        f'<div class="ais-scale"><span>{lo}</span><span>{hi}</span></div>'
    )


def _inject_panels(
    path: Path, *, title: str, caption: str, legend_title: str, legend_body: str
) -> None:
    """Inject a title/caption panel and a legend into a pydeck HTML export."""
    css = (
        "<style>"
        ".ais-panel,.ais-legend{position:absolute;z-index:10;"
        "font-family:-apple-system,Segoe UI,Roboto,sans-serif;color:#e8eaf0;"
        "background:rgba(17,20,28,0.82);border:1px solid rgba(255,255,255,0.08);"
        "border-radius:8px;padding:10px 12px;backdrop-filter:blur(3px);}"
        ".ais-panel{top:12px;left:12px;max-width:360px;pointer-events:none;}"
        ".ais-panel h1{font-size:15px;margin:0 0 4px;font-weight:600;}"
        ".ais-panel p{font-size:11px;margin:0;color:#aab0be;line-height:1.4;}"
        ".ais-legend{bottom:28px;left:12px;font-size:12px;min-width:150px;}"
        ".ais-legend .ais-head{font-size:10px;text-transform:uppercase;"
        "letter-spacing:.06em;color:#aab0be;margin-bottom:7px;}"
        ".ais-row{display:flex;align-items:center;gap:8px;margin:4px 0;}"
        ".ais-swatch{width:13px;height:13px;border-radius:3px;flex:none;}"
        ".ais-bar{width:100%;height:11px;border-radius:3px;}"
        ".ais-scale{display:flex;justify-content:space-between;font-size:10px;"
        "color:#aab0be;margin-top:4px;}"
        "</style>"
    )
    panel = f'<div class="ais-panel"><h1>{title}</h1><p>{caption}</p></div>'
    legend = (
        f'<div class="ais-legend"><div class="ais-head">{legend_title}</div>'
        f"{legend_body}</div>"
    )
    html = path.read_text()
    html = html.replace("</head>", css + "</head>", 1)
    html = html.replace("</body>", panel + legend + "</body>", 1)
    path.write_text(html)


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
        "WHERE stratum = 'commercial' AND label <> 'unclassified'"
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

    comm_max = max((r["vessels"] for r in comm_density), default=1)
    all_max = max((r["vessels"] for r in all_density), default=1)

    views: dict[str, dict[str, Any]] = {
        "classification": {
            "data": hero,
            "stroked": True,
            "tooltip": (
                "<b>{label}</b><br/>vessels {vessels} &middot; pings {pings}"
                "<br/>median SOG {med_sog} kn"
            ),
            "title": "Commercial traffic classification",
            "caption": (
                "Cargo &amp; tanker cells, res-8 H3, two-gate classifier (SOG + density). "
                "Lane cells at res 8; discontinuities reflect the P90 density gate. "
                "Unclassified cells omitted for clarity."
            ),
            "legend_title": "Classification",
            "legend": _legend_categorical(),
        },
        "commercial_density": {
            "data": comm_density,
            "stroked": False,
            "tooltip": "<b>Commercial density</b><br/>{vessels} unique vessels",
            "title": "Commercial vessel density",
            "caption": "Unique cargo/tanker vessels per res-8 cell, log colour scale.",
            "legend_title": "Unique vessels (log)",
            "legend": _legend_gradient("1", str(comm_max)),
        },
        "all_traffic_density": {
            "data": all_density,
            "stroked": False,
            "tooltip": "<b>All-traffic density</b><br/>{vessels} unique vessels",
            "title": "All-traffic density (na\u00efve view)",
            "caption": (
                "Every vessel class per res-8 cell, log scale. Marinas dominate \u2014 "
                "contrast with the stratified commercial view."
            ),
            "legend_title": "Unique vessels (log)",
            "legend": _legend_gradient("1", str(all_max)),
        },
        "median_speed": {
            "data": speed,
            "stroked": False,
            "tooltip": "<b>Median speed</b><br/>{med_sog} kn",
            "title": "Median speed over ground",
            "caption": "Per-cell median SOG; lanes run fast, anchorages near zero.",
            "legend_title": "Median SOG (kn)",
            "legend": _legend_gradient("0", "15+"),
        },
    }
    for name, spec in views.items():
        out = out_dir / f"{name}.html"
        if out.exists() and not force:
            logger.info("skip {}/{} {} — exists", region, window_name, name)
            outputs[name] = out
            continue
        deck = _deck(
            spec["data"], region, region_cfg, spec["tooltip"], stroked=spec["stroked"]
        )
        deck.to_html(str(out), notebook_display=False)
        _inject_panels(
            out,
            title=spec["title"],
            caption=spec["caption"],
            legend_title=spec["legend_title"],
            legend_body=spec["legend"],
        )
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


def _rgb01(rgb: list[int]) -> tuple[float, float, float]:
    return (rgb[0] / 255, rgb[1] / 255, rgb[2] / 255)


def _parse_wkt_polygon(wkt: str) -> list[tuple[float, float]]:
    body = wkt[wkt.index("((") + 2 : wkt.index("))")]
    pts: list[tuple[float, float]] = []
    for pair in body.split(","):
        x, y = pair.split()
        pts.append((float(x), float(y)))
    return pts


def _achare_rings() -> list[list[tuple[float, float]]]:
    rings: list[list[tuple[float, float]]] = []
    paths = [
        STATIC_DIR / "anchorages_la_long_beach.geojson",
        STATIC_DIR / "anchorages_approach.geojson",
    ]
    for path in paths:
        if not path.exists():
            continue
        for feat in json.loads(path.read_text()).get("features", []):
            geom = feat.get("geometry") or {}
            coords = geom.get("coordinates") or []
            if geom.get("type") == "Polygon":
                rings.append([(float(x), float(y)) for x, y in coords[0]])
            elif geom.get("type") == "MultiPolygon":
                for poly in coords:
                    rings.append([(float(x), float(y)) for x, y in poly[0]])
    return rings


def _load_transit_4326(tif: Path) -> tuple[Any, tuple[float, float, float, float]]:
    """Reproject the EPSG:3857 transit raster to lon/lat; return (array, extent)."""
    import numpy as np
    import rasterio
    from rasterio.warp import Resampling, calculate_default_transform, reproject

    with rasterio.open(tif) as src:
        transform, width, height = calculate_default_transform(
            src.crs, "EPSG:4326", src.width, src.height, *src.bounds
        )
        dst = np.zeros((height, width), dtype="float32")
        reproject(
            source=rasterio.band(src, 1),
            destination=dst,
            src_transform=src.transform,
            src_crs=src.crs,
            dst_transform=transform,
            dst_crs="EPSG:4326",
            resampling=Resampling.average,
        )
    left, top = transform.c, transform.f
    right = left + transform.a * width
    bottom = top + transform.e * height
    dst[dst <= 0] = np.nan
    return dst, (left, right, bottom, top)


def validation_panel(
    con: DuckDBPyConnection,
    region: str,
    window_name: str,
    region_cfg: Region,
    *,
    processed_dir: Path = PROCESSED_DIR,
    out_dir: Path = DOCS_IMG,
    transit_tif: Path = TRANSIT_TIF,
) -> Path | None:
    """Two-panel README validation figure: classified cells vs NOAA transit raster."""
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np
    from matplotlib.patches import Patch, Polygon

    win = processed_dir / f"region={region}" / f"window={window_name}"
    classified = win / "cells_classified.parquet"
    if not classified.exists():
        logger.warning("skip panel {}/{} — classified parquet missing", region, window_name)
        return None

    rows = con.execute(
        "SELECT label, h3_cell_to_boundary_wkt(cell) AS wkt "
        f"FROM read_parquet('{classified}') "
        "WHERE stratum = 'commercial' AND label <> 'unclassified'"
    ).fetchall()

    min_lon, min_lat, max_lon, max_lat = region_cfg.bbox
    aspect = 1.0 / math.cos(math.radians((min_lat + max_lat) / 2))

    fig, (ax_a, ax_b) = plt.subplots(
        1, 2, figsize=(15, 7.2), facecolor="#0d0f16", constrained_layout=True
    )
    for ax in (ax_a, ax_b):
        ax.set_facecolor("#0d0f16")
        ax.set_xlim(min_lon, max_lon)
        ax.set_ylim(min_lat, max_lat)
        ax.set_aspect(aspect)
        for spine in ax.spines.values():
            spine.set_color("#333844")
        ax.tick_params(colors="#8a90a0", labelsize=7)

    # Panel (a): classified cells + ACHARE outlines + the four "neither" callouts.
    for label, wkt in rows:
        rgb = _LABEL_COLOR.get(label, [70, 74, 92])
        ax_a.add_patch(
            Polygon(
                _parse_wkt_polygon(wkt),
                closed=True,
                facecolor=[c / 255 for c in rgb],
                edgecolor="none",
                alpha=0.8,
            )
        )
    for ring in _achare_rings():
        ax_a.add_patch(
            Polygon(
                ring,
                closed=True,
                fill=False,
                edgecolor=[c / 255 for c in _ACHARE_COLOR],
                linewidth=0.9,
            )
        )
    for lon, lat, text in _NEITHER_FEATURES:
        ax_a.annotate(
            text,
            xy=(lon, lat),
            xytext=(lon, lat + 0.07),
            color="#e8eaf0",
            fontsize=8,
            ha="center",
            arrowprops={"arrowstyle": "->", "color": "#e8eaf0", "lw": 0.8},
            bbox={"boxstyle": "round,pad=0.2", "fc": "#11141c", "ec": "none", "alpha": 0.85},
        )
    handles = [
        Patch(facecolor=_rgb01(_LABEL_COLOR["shipping lane"]), label="Shipping lane"),
        Patch(
            facecolor=_rgb01(_LABEL_COLOR["anchored/moored"]),
            label="Anchored / moored",
        ),
        Patch(
            facecolor=_rgb01(_LABEL_COLOR["maneuvering/harbor"]),
            label="Maneuvering / harbor",
        ),
        Patch(
            facecolor="none",
            edgecolor=_rgb01(_ACHARE_COLOR),
            label="Charted anchorage (ACHARE)",
        ),
    ]
    leg = ax_a.legend(handles=handles, loc="lower left", fontsize=7.5, framealpha=0.85)
    leg.get_frame().set_facecolor("#11141c")
    leg.get_frame().set_edgecolor("none")
    for txt in leg.get_texts():
        txt.set_color("#e8eaf0")
    ax_a.set_title(
        "(a) Classified commercial cells vs charted anchorages",
        color="#e8eaf0",
        fontsize=11,
    )

    # Panel (b): georeferenced NOAA transit raster, same lon/lat extent.
    if transit_tif.exists():
        arr, extent = _load_transit_4326(transit_tif)
        im = ax_b.imshow(
            np.log1p(arr),
            extent=extent,
            origin="upper",
            cmap="magma",
            interpolation="nearest",
        )
        cbar = fig.colorbar(im, ax=ax_b, fraction=0.046, pad=0.04)
        cbar.set_label("log(1 + transits)", color="#e8eaf0", fontsize=8)
        cbar.ax.yaxis.set_tick_params(color="#8a90a0", labelsize=7)
        plt.setp(cbar.ax.get_yticklabels(), color="#8a90a0")
    else:
        ax_b.text(
            0.5,
            0.5,
            "transit raster unavailable",
            transform=ax_b.transAxes,
            color="#8a90a0",
            ha="center",
        )
    ax_b.set_title(
        "(b) NOAA 2024 AIS Transit Counts — log(1 + transits)",
        color="#e8eaf0",
        fontsize=11,
    )

    out_dir.mkdir(parents=True, exist_ok=True)
    out = out_dir / f"validation_panel_{region}.png"
    fig.savefig(out, dpi=140, facecolor="#0d0f16")
    plt.close(fig)
    logger.info("wrote validation panel -> {}", out)
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
                validation_panel(con, region, window_name, region_cfg)
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
        validation_panel(con, args.region, window_name, region_cfg)


if __name__ == "__main__":
    main()