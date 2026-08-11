import duckdb
import pytest

from src.schema import CANONICAL, LEGACY_MAP, SchemaError, check_schema, probe_columns


def test_probe_columns(tmp_path):
    csv = tmp_path / "mini.csv"
    csv.write_text("mmsi,latitude\n123456789,33.7\n")
    con = duckdb.connect()
    cols = probe_columns(con, str(csv))
    assert set(cols) == {"mmsi", "latitude"}
    assert cols["mmsi"] == "BIGINT"
    assert cols["latitude"] == "DOUBLE"


def test_check_schema_canonical():
    cols = dict.fromkeys(CANONICAL, "VARCHAR")
    assert check_schema(cols) == "canonical"


def test_check_schema_legacy():
    cols = dict.fromkeys(LEGACY_MAP, "VARCHAR")
    assert check_schema(cols) == "legacy"


def test_check_schema_unknown_raises():
    with pytest.raises(SchemaError) as exc:
        check_schema({"foo": "VARCHAR", "bar": "VARCHAR"})
    assert exc.value.missing
    assert exc.value.unexpected == {"foo", "bar"}
