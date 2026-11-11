"""The reconciliation row per trial (#78; the full tables come with #82, ADR 029).

For each trial of an engine config: our run of its unit (from a store copy opened read-only),
the scorer's `scoring.yaml` entry, and the last insertion event of the trial's bag dump (the
scorer stores each one it receives, so the last one counts, `ScoringTier2.cc:444-447`). The
port name is read as the scorer reads it: split on `/`, empty parts dropped, the first two
parts the target module and the port (`:815-832`), so a leading `/` does not matter. A row
agrees when our outcome and the scorer's insertion into the target port say the same thing; a
trial without a finished run or without its bag dump has no referee data to compare, so it
neither agrees nor disagrees. Every file read has a size bound.

The store must hold at most one finished attempt per unit: one engine run per store, as the
tracer writes it. Joining a unit's attempts to the engine run that made each one comes with the
full runner (#81); until then a second finished attempt is refused, never guessed at.

    python -m tools.sim.report --store S --config C --manifest M --scoring Y --bags DIR --setting A
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import yaml

from adapters.aic.manifest import ManifestError, load_manifest
from cellrt.runtime.events import MonitorVerdict, RunFinished
from cellrt.runtime.store import EventStore

_TRIAL_ID = re.compile(r"trial_[0-9]{1,4}")
MAX_YAML_BYTES = 1 << 20
MAX_BAG_JSON_BYTES = 16 << 20
COLUMNS = ("trial", "task", "run", "outcome", "verdicts", "inserted", "target", "tier3", "total")


class ReportInputError(ValueError):
    """An input file the report will not read; the message names which one, never its text."""


@dataclasses.dataclass(frozen=True)
class TrialRow:
    trial_id: str
    task_id: str
    run_id: str | None
    outcome: str | None
    verdicts: int
    dumped: bool  # the trial's bag dump exists: the referee's insertion data
    inserted: str | None  # the port name of the last insertion event, as the bag holds it
    target: str  # <target module>/<port>
    tier3: float | None
    total: float | None

    @property
    def into_target(self) -> bool:
        """Whether the scorer would score the insertion as into the target port."""
        if self.inserted is None:
            return False
        parts = port_parts(self.inserted)
        return len(parts) >= 2 and parts[:2] == tuple(self.target.split("/"))

    @property
    def agrees(self) -> bool | None:
        """None without a finished run or without the bag dump; else whether our outcome
        matches an insertion into the target."""
        if self.outcome is None or not self.dumped:
            return None
        return (self.outcome == "success") == self.into_target


def port_parts(name: str) -> tuple[str, ...]:
    """An insertion event's port name as the scorer splits it: on `/`, empty parts dropped."""
    return tuple(part for part in name.split("/") if part)


def trial_rows(
    *,
    store: Path,
    engine_config: Path,
    manifest: Path,
    scoring: Path,
    bags: Path,
    setting: str,
) -> list[TrialRow]:
    config = _yaml(engine_config, "engine config")
    scores = _yaml(scoring, "scoring.yaml")
    placements = load_manifest(manifest)
    rows: list[TrialRow] = []
    with EventStore(store, read_only=True) as events:
        runs = events.runs()
        for trial_id, trial in config["trials"].items():
            if not _TRIAL_ID.fullmatch(str(trial_id)):  # it names a file under `bags`
                raise ReportInputError("engine config: a trial id is not trial_<n>")
            ((task_id, task),) = trial["tasks"].items()
            placed = placements.lookup(task_id)
            finished = [
                r
                for r in runs
                if (r.scenario_id, r.seed, r.setting) == (placed.scenario_id, placed.seed, setting)
                and r.finished
            ]
            if len(finished) > 1:  # which engine run made which is not recorded yet (#81)
                raise ReportInputError("store: a unit has more than one finished attempt")
            run = finished[0] if finished else None
            outcome, verdicts = None, 0
            if run is not None:
                for event in events.events(run.run_id):
                    match event.payload:
                        case MonitorVerdict():
                            verdicts += 1
                        case RunFinished(outcome=end):
                            outcome = end
            score = scores.get(trial_id) if isinstance(scores, dict) else None
            dumped, inserted = _last_insertion(bags / f"{trial_id}.json")
            rows.append(
                TrialRow(
                    trial_id=trial_id,
                    task_id=task_id,
                    run_id=None if run is None else run.run_id,
                    outcome=outcome,
                    verdicts=verdicts,
                    dumped=dumped,
                    inserted=inserted,
                    target=f"{task['target_module_name']}/{task['port_name']}",
                    tier3=_tier(score, "tier_3"),
                    total=_total(score),
                )
            )
    return rows


def format_rows(rows: Sequence[TrialRow]) -> str:
    lines = ["| " + " | ".join((*COLUMNS, "agrees")) + " |"]
    lines.append("|" + "|".join("---" for _ in range(len(COLUMNS) + 1)) + "|")
    for r in rows:
        cells = (
            r.trial_id,
            r.task_id,
            r.run_id,
            r.outcome,
            r.verdicts,
            r.inserted if r.inserted is not None or not r.dumped else "none",
            r.target,
            r.tier3,
            None if r.total is None else round(r.total, 2),
            None if r.agrees is None else ("yes" if r.agrees else "no"),
        )
        lines.append("| " + " | ".join("-" if c is None else str(c) for c in cells) + " |")
    return "\n".join(lines)


def _read(path: Path, limit: int, what: str) -> bytes:
    """At most `limit` bytes, however the file behaves (a FIFO, a file that grows)."""
    try:
        with path.open("rb") as source:
            raw = source.read(limit + 1)
    except OSError:
        raise ReportInputError(f"{what}: unreadable") from None
    if len(raw) > limit:
        raise ReportInputError(f"{what}: over {limit} bytes")
    return raw


def _yaml(path: Path, what: str) -> Any:
    try:
        return yaml.safe_load(_read(path, MAX_YAML_BYTES, what))
    except yaml.YAMLError:
        raise ReportInputError(f"{what}: not YAML") from None


def _last_insertion(path: Path) -> tuple[bool, str | None]:
    """Whether the bag dump exists, and the port of its last insertion event (None: none)."""
    if not path.exists():
        return False, None
    raw = _read(path, MAX_BAG_JSON_BYTES, "bag dump")  # its own refusal, not "not a bag dump"
    try:
        dump = json.loads(raw)
        insertions = dump["insertions"]
        return True, (str(insertions[-1]["port"]) if insertions else None)
    except (ValueError, KeyError, TypeError, IndexError):
        raise ReportInputError("bag dump: not a bag dump") from None


def _tier(score: Any, tier: str) -> float | None:
    try:
        return float(score[tier]["score"])
    except (KeyError, TypeError, ValueError):
        return None


def _total(score: Any) -> float | None:
    tiers = [_tier(score, t) for t in ("tier_1", "tier_2", "tier_3")]
    return None if any(t is None for t in tiers) else sum(t for t in tiers if t is not None)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.sim.report")
    for name in ("store", "config", "manifest", "scoring", "bags"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--setting", default="A", choices=("A", "B", "C"))
    args = parser.parse_args(argv)
    try:
        rows = trial_rows(
            store=args.store,
            engine_config=args.config,
            manifest=args.manifest,
            scoring=args.scoring,
            bags=args.bags,
            setting=args.setting,
        )
    except (ReportInputError, ManifestError) as ex:  # fixed messages only
        print(f"report: {ex}", file=sys.stderr)
        return 1
    print(format_rows(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
