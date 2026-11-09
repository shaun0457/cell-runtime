"""A metrics table as a stable, diffable snapshot, and the comparison with a committed one.

The behavioural regression gate runs a small mock eval and compares its table with a baseline
file. Counts, `None` and missing cells must match exactly; rates and means may differ by at most
`BASELINE_TOLERANCE`, so the last bits of float arithmetic on another platform do not fail the
gate while any real change in behaviour does.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from cellrt.eval.metrics import MetricsTable

BASELINE_TOLERANCE = 1e-9
# Seeds a baseline may name: the gate runs on every CI job, so it must stay small.
MAX_BASELINE_SEEDS = 20
_MISSING = "<missing>"


class BaselineConfig(BaseModel):
    """The eval a baseline file records: run it again to check the file."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    scenarios: str = Field(min_length=1)
    settings: list[str] = Field(min_length=1)
    seeds: int = Field(ge=1, le=MAX_BASELINE_SEEDS)
    k: int = Field(ge=1)


class BaselineFile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    config: BaselineConfig
    table: dict[str, Any]


def snapshot(table: MetricsTable) -> dict[str, Any]:
    """The table as plain JSON data, keyed so that every cell has a stable name."""
    return {
        "k": table.k,
        "rows": {
            row.setting: {k: v for k, v in dataclasses.asdict(row).items() if k != "setting"}
            for row in table.rows
        },
        "confusion": {
            f"{truth}|{predicted}": n for (truth, predicted), n in table.confusion.items()
        },
        "gains": {
            g.setting: {"won": g.won, "unexplained": list(g.unexplained)} for g in table.gains
        },
    }


def compare(expected: Mapping[str, Any], actual: Mapping[str, Any]) -> list[str]:
    """One line per cell that differs, sorted by cell name; empty when the tables match."""
    want, got = _flatten(expected), _flatten(actual)
    return [
        f"{cell}: expected {want.get(cell, _MISSING)}, got {got.get(cell, _MISSING)}"
        for cell in sorted(want.keys() | got.keys())
        if not _same(want.get(cell, _MISSING), got.get(cell, _MISSING))
    ]


def _same(a: object, b: object) -> bool:
    """Floats match within the tolerance; everything else (an int, None, a string, a list of
    run ids) only when equal and of the same type, so a count that turns into a float differs."""
    if isinstance(a, float) and isinstance(b, float):
        return abs(a - b) <= BASELINE_TOLERANCE
    return type(a) is type(b) and a == b


def _flatten(value: Mapping[str, Any], prefix: str = "") -> dict[str, object]:
    cells: dict[str, object] = {}
    for key, v in value.items():
        name = f"{prefix}{key}"
        if isinstance(v, Mapping):
            cells.update(_flatten(v, f"{name}."))
        elif isinstance(v, list):  # compared whole and exactly (today: run ids only)
            cells[name] = tuple(v)
        else:
            cells[name] = v
    return cells
