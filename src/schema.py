from collections.abc import Mapping
from typing import Literal

from duckdb import DuckDBPyConnection

SchemaKind = Literal["canonical", "legacy"]


class SchemaError(Exception):
    """Raw file's columns match no known AIS schema."""

    def __init__(self, missing: set[str], unexpected: set[str]) -> None:
        self.missing, self.unexpected = missing, unexpected
        super().__init__(
            f"Unknown schema. Missing: {sorted(missing)}; unexpected: {sorted(unexpected)}"
        )  # The canonical schema: what every downstream stage may rely on.


CANONICAL = {
    "mmsi": "BIGINT",
    "base_date_time": "TIMESTAMP",
    "longitude": "DOUBLE",
    "latitude": "DOUBLE",
    "sog": "DOUBLE",
    "cog": "DOUBLE",
    "heading": "BIGINT",
    "vessel_type": "BIGINT",
    "status": "BIGINT",
    "length": "BIGINT",
    "width": "BIGINT",
    "draft": "DOUBLE",
    "transceiver": "VARCHAR",
}

# Columns that exist in raw but we deliberately drop.
DROPPED = {"vessel_name", "imo", "call_sign", "cargo"}

# Legacy 2018–2024 CamelCase names → canonical. Kept as a documented
# fallback in case a non-csv2 file ever enters the pipeline.
LEGACY_MAP = {
    "MMSI": "mmsi",
    "BaseDateTime": "base_date_time",
    "LAT": "latitude",
    "LON": "longitude",
    "SOG": "sog",
    "COG": "cog",
    "Heading": "heading",
    "VesselType": "vessel_type",
    "Status": "status",
    "Length": "length",
    "Width": "width",
    "Draft": "draft",
    "TransceiverClass": "transceiver",
}


def probe_columns(con: DuckDBPyConnection, path: str) -> dict[str, str]:
    rows = con.execute("DESCRIBE SELECT * FROM read_csv_auto(?)", [path]).fetchall()
    return {name: dtype for name, dtype, *_ in rows}


def check_schema(cols: Mapping[str, str]) -> SchemaKind:
    names = set(cols)
    if set(CANONICAL) <= names:
        return "canonical"
    if set(LEGACY_MAP) <= names:
        return "legacy"
    missing = set(CANONICAL) - names
    unexpected = names - set(CANONICAL) - DROPPED
    raise SchemaError(missing, unexpected)
