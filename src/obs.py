import csv
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import BaseModel

LEDGER_PATH = Path("data/run_ledger.csv")
CLASS_LEDGER_PATH = Path("data/class_ledger.csv")

_FIELDS = ["ran_at", "region", "date", "rows_raw", "rows_bbox", "rows_clean", "output"]
_CLASS_FIELDS = ["ran_at", "region", "window", "stratum", "label", "cells"]


class RunRecord(BaseModel):
    """One day's ingest funnel: raw → bbox → cleaned row counts."""

    region: str
    date: date
    rows_raw: int
    rows_bbox: int
    rows_clean: int
    output: Path


class ClassRecord(BaseModel):
    """Per-class cell count for one classified region-window-stratum."""

    region: str
    window: str
    stratum: str
    label: str
    cells: int


def record_run(record: RunRecord, ledger: Path = LEDGER_PATH) -> None:
    """Append a run to the CSV ledger, writing the header if the file is new."""
    ledger.parent.mkdir(parents=True, exist_ok=True)
    new_file = not ledger.exists()
    with open(ledger, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_FIELDS)
        if new_file:
            writer.writeheader()
        writer.writerow(
            {
                "ran_at": datetime.now(UTC).isoformat(timespec="seconds"),
                "region": record.region,
                "date": record.date.isoformat(),
                "rows_raw": record.rows_raw,
                "rows_bbox": record.rows_bbox,
                "rows_clean": record.rows_clean,
                "output": str(record.output),
            }
        )


def record_classes(
    records: list[ClassRecord], ledger: Path = CLASS_LEDGER_PATH
) -> None:
    """Append per-class cell counts to the classification ledger."""
    ledger.parent.mkdir(parents=True, exist_ok=True)
    new_file = not ledger.exists()
    ran_at = datetime.now(UTC).isoformat(timespec="seconds")
    with open(ledger, "a", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=_CLASS_FIELDS)
        if new_file:
            writer.writeheader()
        for record in records:
            writer.writerow(
                {
                    "ran_at": ran_at,
                    "region": record.region,
                    "window": record.window,
                    "stratum": record.stratum,
                    "label": record.label,
                    "cells": record.cells,
                }
            )
