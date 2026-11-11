"""The Phase 2 tables (#82; ADR 026, 028, 029, 030): each unit's counted trial, judged against
the referee, then four tables.

**Inputs.** The manifest the units come from, and one directory per engine run, laid out as
`tools/sim/batch.py` writes it:
- `engine.yaml`: the engine config it ran;
- `ledger.jsonl`: its one ledger row (the setting, the units, the wall times, the commit and the
  config's SHA-256);
- `aic/scoring.yaml`: the scorer's file, absent when the engine stopped before writing it;
- `bags/<trial id>.json`: each trial's bag dump;
- `snapshot/store.sqlite`: the store copy taken after the run, opened read-only; absent when
  nothing wrote to the store.

**Which trial counts** (ADR 028). An engine run's attempt at a unit is the unit's run (its
scenario and seed, in the engine run's setting) whose `run_started` was recorded between the
engine run's start and end. Per unit and setting, the first engine run (by start) whose attempt
finished and has referee data, its `scoring.yaml` entry and its bag dump, counts. A unit with
none is infrastructure: listed, and left out of every rate. The engine runs must name one commit
(one session, one commit; `--commit` names the one required, and the held-out manifest needs
it), each engine config must be the file its ledger row hashed, and no unit may appear twice in
one engine run.

**Every engine run of the session.** An attempt at one of the manifest's units that the latest
store copy holds, recorded between the first engine run's start and the last one's end, must lie
in the window of an engine run given, of its setting: otherwise a `--run` was left out, and the
report refuses rather than count its units as infrastructure. Attempts before the first engine
run given are another session's and are not checked, so a left-out first or last engine run is
not seen.

**The claim.** ADR 030 makes its one pooled claim on the held-out manifest (its name starts with
`heldout`); on any other manifest the A/B table is descriptive and no claim is printed.

**The tables.**
- Discovery: per scene, each trial's class per setting (ADR 026), and whether A's are mixed.
- Reconciliation: per trial, our outcome against the insertion, our time over 20 N against the
  scorer's force penalty, the bag's contact messages against the scorer's contacts score, and
  whether the insertion's time was mapped (ADR 029). The port of a wrong-port insertion is
  named.
- A/B: per family and pooled, the referee's success with its Wilson interval and the secondary
  metrics, then ADR 030's one pooled claim.
- Confusion: per setting and verdict class, how the verdicts counted, and the false-alarm count
  with and without the trials judged as a whole.

Every file read has a size bound, and every refusal is a fixed message naming the input.

    python -m tools.sim.report --manifest M --run DIR [--run DIR ...] [--commit SHA]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import re
import sqlite3
import stat
import sys
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import fmean
from typing import Any

import pydantic
import yaml

from adapters.aic.manifest import Manifest, ManifestError, load_manifest
from adapters.aic.scoring import ScoringInputError, referee_trial
from cellrt.eval.intervals import SceneTally, claim, format_interval, wilson
from cellrt.eval.metrics import RunSummary, summarize_run
from cellrt.eval.referee import (
    FALSE_SUCCESS,
    INFRASTRUCTURE,
    JudgedTrial,
    RefereeTrial,
    judge,
)
from cellrt.runtime.events import Event
from cellrt.runtime.store import EventStore, RunInfo
from tools.sim.batch import SETTINGS

LEDGER = Path("ledger.jsonl")
CONFIG = Path("engine.yaml")
SCORING = Path("aic") / "scoring.yaml"
BAGS = Path("bags")
STORE_COPY = Path("snapshot") / "store.sqlite"

MAX_YAML_BYTES = 1 << 20
MAX_BAG_JSON_BYTES = 16 << 20
MAX_LEDGER_BYTES = 64 << 10
MAX_RUNS = 500
POOLED = "all"
HELD_OUT_PREFIX = "heldout"  # ADR 030: the manifest the claim is made on

_TRIAL_ID = re.compile(r"trial_[0-9]{1,4}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}|unknown")
_FULL_COMMIT = re.compile(r"[0-9a-f]{40}")
_TAG = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")  # as tools/sim/batch.py checks a plan's tag
_EXIT = re.compile(r"[a-z0-9_]{1,64}")  # "ok" or a reason code
_NOT_A_ROW = "ledger: not a ledger row"
_NOT_OURS = "store copy: an event the schema does not hold"


class ReportInputError(ValueError):
    """An input the report will not read; the message names which one, never its text."""


@dataclass(frozen=True)
class EngineRun:
    """One engine run's directory, read (see the module docstring)."""

    path: Path
    tag: str
    setting: str
    started_wall: float
    ended_wall: float
    hours: float
    exit: str
    commit: str
    trials: tuple[tuple[str, str, tuple[str, str]], ...]  # (trial id, task id, target), in order
    scores: Mapping[str, Any] | None  # scoring.yaml by trial id; None: the scorer wrote none


@dataclass(frozen=True)
class Trial:
    """A unit's counted trial in one setting; without one (judged None), infrastructure."""

    task_id: str
    scene: str
    family: str
    setting: str
    seed: int
    engine_runs: int  # the engine runs whose config held the unit
    tag: str | None  # the engine run whose trial counts
    trial_id: str | None
    judged: JudgedTrial | None
    referee: RefereeTrial | None
    summary: RunSummary | None

    @property
    def trial_class(self) -> str:
        return INFRASTRUCTURE if self.judged is None else self.judged.trial_class


@dataclass(frozen=True)
class _Attempt:
    run: EngineRun
    trial_id: str
    judged: JudgedTrial | None = None
    referee: RefereeTrial | None = None
    summary: RunSummary | None = None


@dataclass(frozen=True)
class _Stored:
    """An attempt in a store copy, with the wall time of its `run_started`."""

    info: RunInfo
    started_wall: float


# reading ----------------------------------------------------------------------------------------


def load_runs(paths: Sequence[Path]) -> list[EngineRun]:
    """Each directory's engine run, checked; one session: one commit, no tag twice."""
    if len(paths) > MAX_RUNS:
        raise ReportInputError(f"over {MAX_RUNS} engine runs")
    runs = [load_run(p) for p in paths]
    if len({r.tag for r in runs}) != len(runs):
        raise ReportInputError("two engine runs share a tag")
    if len({r.commit for r in runs}) > 1:
        raise ReportInputError("ledger: the engine runs name more than one commit")
    return runs


def load_run(path: Path) -> EngineRun:
    row = _ledger_row(path / LEDGER)
    raw = _read(path / CONFIG, MAX_YAML_BYTES, "engine config")
    if hashlib.sha256(raw).hexdigest() != row["config_sha256"]:
        raise ReportInputError("engine config: not the file its ledger row hashed")
    trials = _trials(_yaml(raw, "engine config"))
    if [task_id for _, task_id, _ in trials] != row["units"]:
        raise ReportInputError("ledger: its units are not the engine config's")
    scores = None
    if (path / SCORING).exists():
        scores = _yaml(_read(path / SCORING, MAX_YAML_BYTES, "scoring.yaml"), "scoring.yaml")
        if not isinstance(scores, dict):
            raise ReportInputError("scoring.yaml: not a scorer file")
    return EngineRun(
        path=path,
        tag=row["tag"],
        setting=row["setting"],
        started_wall=float(row["started_wall"]),
        ended_wall=float(row["ended_wall"]),
        hours=float(row["hours"]),
        exit=row["exit"],
        commit=row["commit"],
        trials=trials,
        scores=scores,
    )


def _ledger_row(path: Path) -> dict[str, Any]:
    raw = _read(path, MAX_LEDGER_BYTES, "ledger")
    try:
        lines = [line for line in raw.decode("utf-8").splitlines() if line.strip()]
        if len(lines) != 1:
            raise ReportInputError("ledger: not one row")
        row = json.loads(lines[0])
    except (ValueError, RecursionError) as ex:
        if isinstance(ex, ReportInputError):
            raise
        raise ReportInputError(_NOT_A_ROW) from None
    if not isinstance(row, dict):
        raise ReportInputError(_NOT_A_ROW)
    texts = ("tag", "setting", "exit", "commit", "config_sha256")
    numbers = ("started_wall", "ended_wall", "hours")
    if not all(isinstance(row.get(k), str) for k in texts) or not all(
        isinstance(row.get(k), int | float) and not isinstance(row.get(k), bool) for k in numbers
    ):
        raise ReportInputError(_NOT_A_ROW)
    try:
        row.update({k: float(row[k]) for k in numbers})  # an int too large: OverflowError
    except OverflowError:
        raise ReportInputError(_NOT_A_ROW) from None
    if not all(math.isfinite(row[k]) for k in numbers):
        raise ReportInputError(_NOT_A_ROW)
    units = row.get("units")
    if not isinstance(units, list) or not all(isinstance(u, str) for u in units):
        raise ReportInputError(_NOT_A_ROW)
    if (
        row["setting"] not in SETTINGS
        or not _TAG.fullmatch(row["tag"])
        or not _EXIT.fullmatch(row["exit"])
        or not _COMMIT.fullmatch(row["commit"])
        or not _SHA256.fullmatch(row["config_sha256"])
        or not row["started_wall"] <= row["ended_wall"]
    ):
        raise ReportInputError(_NOT_A_ROW)
    if len(set(units)) != len(units):  # one goal per unit: a second would join the same attempt
        raise ReportInputError("ledger: a unit twice in one engine run")
    return row


def _trials(config: Any) -> tuple[tuple[str, str, tuple[str, str]], ...]:
    trials = config.get("trials") if isinstance(config, dict) else None
    if not isinstance(trials, dict):
        raise ReportInputError("engine config: no trials")
    out = []
    for trial_id, trial in trials.items():
        if not isinstance(trial_id, str) or not _TRIAL_ID.fullmatch(trial_id):
            raise ReportInputError("engine config: a trial id is not trial_<n>")  # names a file
        try:
            ((task_id, task),) = trial["tasks"].items()
            target = (task["target_module_name"], task["port_name"])
        except (TypeError, KeyError, AttributeError, ValueError):
            raise ReportInputError("engine config: a trial without its one task") from None
        if not all(isinstance(x, str) for x in (task_id, *target)):
            raise ReportInputError("engine config: a trial without its one task")
        out.append((trial_id, task_id, target))
    return tuple(out)


def _read(path: Path, limit: int, what: str) -> bytes:
    """At most `limit` bytes of a regular file, however the file behaves (a file that grows)."""
    try:
        if not stat.S_ISREG(path.stat().st_mode):  # a FIFO would block the open
            raise ReportInputError(f"{what}: not a regular file")
        with path.open("rb") as source:
            raw = source.read(limit + 1)
    except OSError:
        raise ReportInputError(f"{what}: unreadable") from None
    if len(raw) > limit:
        raise ReportInputError(f"{what}: over {limit} bytes")
    return raw


def _yaml(raw: bytes, what: str) -> Any:
    try:
        return yaml.safe_load(raw)
    # ValueError: a timestamp that is no date (2026-02-30); RecursionError: deep nesting
    except (yaml.YAMLError, ValueError, RecursionError):
        raise ReportInputError(f"{what}: not YAML") from None


def _bag_dump(path: Path) -> Any | None:
    """The trial's bag dump, or None when there is none."""
    if not path.exists():
        return None
    raw = _read(path, MAX_BAG_JSON_BYTES, "bag dump")  # its own refusal, not "not a bag dump"
    try:
        return json.loads(raw)
    except (ValueError, RecursionError):
        raise ReportInputError("bag dump: not a bag dump") from None


# counting ---------------------------------------------------------------------------------------


def collect(manifest: Manifest, runs: Sequence[EngineRun]) -> list[Trial]:
    """Each unit and setting the engine runs held, with the trial that counts (ADR 028)."""
    ordered = sorted(runs, key=lambda r: r.started_wall)
    by_unit: dict[tuple[str, str], list[_Attempt]] = defaultdict(list)
    latest: list[_Stored] = []
    for run in ordered:
        attempts, stored = _attempts(run, manifest)
        if stored is not None:
            latest = stored  # each copy holds the whole store as it was after its engine run
        for task_id, attempt in attempts:
            by_unit[(task_id, run.setting)].append(attempt)
    scenarios = {manifest.lookup(task_id).scenario_id for task_id in manifest.units}
    _check_every_run_given(ordered, latest, scenarios)
    trials = []
    for (task_id, setting), attempts in sorted(by_unit.items()):
        unit = manifest.units[task_id]
        counted = next((a for a in attempts if a.judged is not None), None)
        trials.append(
            Trial(
                task_id=task_id,
                scene=unit.scene,
                family=manifest.scenes[unit.scene].family,
                setting=setting,
                seed=unit.seed,
                engine_runs=len(attempts),
                tag=None if counted is None else counted.run.tag,
                trial_id=None if counted is None else counted.trial_id,
                judged=None if counted is None else counted.judged,
                referee=None if counted is None else counted.referee,
                summary=None if counted is None else counted.summary,
            )
        )
    return trials


def _check_every_run_given(
    runs: Sequence[EngineRun], stored: Sequence[_Stored], scenarios: set[str]
) -> None:
    """Refuse a session with an engine run left out (see the module docstring)."""
    if not runs:
        return
    first, last = runs[0].started_wall, max(r.ended_wall for r in runs)
    for s in stored:
        if s.info.scenario_id not in scenarios or not first <= s.started_wall <= last:
            continue
        if not any(
            r.setting == s.info.setting and r.started_wall <= s.started_wall <= r.ended_wall
            for r in runs
        ):
            raise ReportInputError("store copy: an attempt outside every engine run given")


def _attempts(
    run: EngineRun, manifest: Manifest
) -> tuple[list[tuple[str, _Attempt]], list[_Stored] | None]:
    """The engine run's attempt at each of its units, and every attempt its store copy holds
    (None without a store copy)."""
    placed = {task_id: manifest.lookup(task_id) for _, task_id, _ in run.trials}  # unknown: refused
    store_path = run.path / STORE_COPY
    if not store_path.exists():  # nothing wrote to the store: no attempt reached the policy
        return [(task_id, _Attempt(run, trial_id)) for trial_id, task_id, _ in run.trials], None
    try:
        with EventStore(store_path, read_only=True) as store:
            stored = [_Stored(info, _first(store, info.run_id).wall_ts) for info in store.runs()]
            out = []
            for trial_id, task_id, target in run.trials:
                unit = (placed[task_id].scenario_id, placed[task_id].seed, run.setting)
                mine = [
                    s.info
                    for s in stored
                    if (s.info.scenario_id, s.info.seed, s.info.setting) == unit
                    and run.started_wall <= s.started_wall <= run.ended_wall
                ]
                out.append((task_id, _attempt(run, store, trial_id, target, mine)))
    except sqlite3.Error:
        raise ReportInputError("store copy: unreadable") from None
    return out, stored


def _attempt(
    run: EngineRun,
    store: EventStore,
    trial_id: str,
    target: tuple[str, str],
    mine: Sequence[RunInfo],
) -> _Attempt:
    if len(mine) > 1:  # one goal per trial, one attempt per goal
        raise ReportInputError("store copy: a unit has two attempts in one engine run")
    score = None if run.scores is None else run.scores.get(trial_id)
    if not mine or not mine[0].finished or score is None:
        return _Attempt(run, trial_id)
    dump = _bag_dump(run.path / BAGS / f"{trial_id}.json")
    if dump is None:
        return _Attempt(run, trial_id)
    events = _events(store, mine[0].run_id)
    referee = referee_trial(score, dump, target)
    try:
        judged = judge(events, referee)
    except ValueError:  # finished, yet not run_started ... run_finished: the copy is not ours
        raise ReportInputError("store copy: a finished run out of order") from None
    return _Attempt(run, trial_id, judged, referee, summarize_run(events))


def _first(store: EventStore, run_id: str) -> Event:
    return _events(store, run_id, limit=1)[0]  # a listed run has its run_started


def _events(store: EventStore, run_id: str, limit: int | None = None) -> list[Event]:
    """The run's events; a payload the schema does not hold is refused by a fixed message,
    since pydantic's would quote the store's content."""
    try:
        return store.events(run_id, limit=limit)
    except (pydantic.ValidationError, json.JSONDecodeError, RecursionError):
        raise ReportInputError(_NOT_OURS) from None


# tables -----------------------------------------------------------------------------------------


def render(runs: Sequence[EngineRun], trials: Sequence[Trial], *, held_out: bool) -> str:
    return "\n\n".join(
        (
            session_line(runs),
            "## Discovery",
            discovery_table(trials),
            "## Reconciliation",
            reconciliation_table(trials),
            "## A/B",
            ab_table(trials, held_out=held_out),
            "## Confusion",
            confusion_table(trials),
        )
    )


def session_line(runs: Sequence[EngineRun]) -> str:
    commits = sorted({r.commit for r in runs})
    hours = sum(r.hours for r in runs)
    not_ok = [f"{r.tag} ({r.exit})" for r in runs if r.exit != "ok"]
    line = f"engine runs: {len(runs)}; commit: {', '.join(commits) or '-'}; VM hours: {hours:.2f}"
    return line + (f"; not ok: {', '.join(not_ok)}" if not_ok else "")


def discovery_table(trials: Sequence[Trial]) -> str:
    settings = _settings(trials)
    scenes: dict[str, dict[str, list[Trial]]] = defaultdict(lambda: defaultdict(list))
    families: dict[str, str] = {}
    for t in sorted(trials, key=lambda t: (t.scene, t.setting, t.seed)):
        scenes[t.scene][t.setting].append(t)
        families[t.scene] = t.family
    rows = []
    for scene in sorted(scenes):
        cells = [", ".join(t.trial_class for t in scenes[scene][s]) or None for s in settings]
        rows.append([scene, families[scene], *cells, _mixed(scenes[scene]["A"])])
    return _table(("scene", "family", *settings, "A mixed"), rows)


def _mixed(trials: Sequence[Trial]) -> str | None:
    """ADR 026: two counted A trials of a scene in different classes."""
    counted = [t.trial_class for t in trials if t.judged is not None]
    if len(counted) < 2:
        return None
    return "yes" if len(set(counted)) > 1 else "no"


RECONCILIATION = (
    "unit",
    "setting",
    "trial",
    "run",
    "outcome",
    "insertion",
    "agrees",
    ">20N s",
    "force penalty",
    "contacts",
    "contacts score",
    "score",
    "time",
    "class",
)


def reconciliation_table(trials: Sequence[Trial]) -> str:
    rows: list[list[object]] = []
    for t in trials:
        j, r, s = t.judged, t.referee, t.summary
        if j is None or r is None or s is None:
            empty = [None] * (len(RECONCILIATION) - 3)
            rows.append([t.task_id, t.setting, *empty, f"{t.trial_class} ({t.engine_runs} runs)"])
            continue
        rows.append(
            [
                t.task_id,
                t.setting,
                f"{t.tag}/{t.trial_id}",
                j.run_id,
                j.outcome,
                _insertion(r),
                "yes" if (j.outcome == "success") == j.inserted else "no",
                f"{s.over_limit_s:.3f}",
                r.force_penalty,
                r.contact_messages,
                r.contacts_score,
                None if r.total is None else round(r.total, 2),
                "mapped" if j.insertion_t is not None else ("whole trial" if j.fallback else None),
                j.trial_class,
            ]
        )
    return _table(RECONCILIATION, rows)


def _insertion(r: RefereeTrial) -> str:
    if r.inserted:
        return "target"
    if r.wrong_port and r.last_port is not None:
        return "wrong port: " + "/".join(r.last_port)
    return "none"


AB = (
    "family",
    "setting",
    "trials",
    "inserted",
    "Wilson 95%",
    "runtime success",
    "false success",
    "recovered",
    "unsafe",
    "with contacts",
    "mean s",
    "infrastructure",
)


def ab_table(trials: Sequence[Trial], *, held_out: bool) -> str:
    """Per family and pooled; the claim only on the held-out manifest (ADR 030)."""
    rows: list[list[object]] = []
    for family in [*sorted({t.family for t in trials}), POOLED]:
        for setting in _settings(trials):
            members = [t for t in trials if t.setting == setting and family in (POOLED, t.family)]
            if not members:
                continue
            counted = _counted(members)
            inserted = sum(j.inserted for j, _, _ in counted)
            durations = [s.duration_s for _, _, s in counted]
            rows.append(
                [
                    family,
                    setting,
                    len(counted),
                    inserted,
                    format_interval(wilson(inserted, len(counted))),
                    sum(j.outcome == "success" for j, _, _ in counted),
                    sum(j.trial_class == FALSE_SUCCESS for j, _, _ in counted),
                    sum(any(v.counted == "recovered" for v in j.verdicts) for j, _, _ in counted),
                    sum(s.unsafe for _, _, s in counted),
                    sum(r.contact_messages > 0 for _, r, _ in counted),
                    f"{fmean(durations):.1f}" if durations else None,
                    len(members) - len(counted),
                ]
            )
    if not held_out:
        return _table(AB, rows) + "\n\n" + NO_CLAIM_HERE
    return _table(AB, rows) + "\n\n" + claim_line(trials)


NO_CLAIM_HERE = "claim: none; ADR 030 makes it on the held-out manifest only (descriptive table)"


def claim_line(trials: Sequence[Trial]) -> str:
    """ADR 030's pooled claim over every counted A and B trial given."""
    a = {t.task_id: t for t in trials if t.setting == "A" and t.judged is not None}
    b = {t.task_id: t for t in trials if t.setting == "B" and t.judged is not None}
    if not a or not b:
        return "claim: needs counted trials in both A and B"
    scenes = []
    for scene in sorted({t.scene for t in a.values()} & {t.scene for t in b.values()}):
        sa = [t for t in a.values() if t.scene == scene]
        sb = [t for t in b.values() if t.scene == scene]
        scenes.append(SceneTally(scene, _wins(sa), len(sa), _wins(sb), len(sb)))
    ka, kb = _wins(a.values()), _wins(b.values())
    result = claim(ka, len(a), kb, len(b), scenes)
    return (
        f"claim (pooled, ADR 030): A {format_interval(result.a)} ({ka}/{len(a)}),"
        f" B {format_interval(result.b)} ({kb}/{len(b)}); scenes: B better {result.b_wins},"
        f" A better {result.a_wins}, tied {result.ties}; sign test p = {result.p:.4f}:"
        f" {result.verdict}"
    )


CONFUSION = (
    "setting",
    "verdict class",
    "verdicts",
    "false alarm",
    "tie",
    "late",
    "recovered",
    "real failure",
    "unconfirmed",
)


def confusion_table(trials: Sequence[Trial]) -> str:
    counts: dict[tuple[str, str], Counter[str]] = defaultdict(Counter)
    false_alarms = without_fallback = 0
    fallback: list[str] = []
    for t in trials:
        if t.judged is None:
            continue
        if t.judged.fallback:
            fallback.append(f"{t.task_id} ({t.setting})")
        for v in t.judged.verdicts:
            c = counts[(t.setting, v.failure_class)]
            c["verdicts"] += 1
            c[v.counted] += 1
            c["tie"] += v.tie
            c["late"] += v.late
            c["unconfirmed"] += v.unconfirmed
            if v.counted == "false_alarm":
                false_alarms += 1
                without_fallback += not t.judged.fallback
    keys = ("verdicts", "false_alarm", "tie", "late", "recovered", "real_failure", "unconfirmed")
    rows = [[*key, *(counts[key][k] for k in keys)] for key in sorted(counts)]
    notes = [
        f"false alarms: {false_alarms}; without the {len(fallback)} trials judged as a whole:"
        f" {without_fallback}",
        "judged as a whole (no time mapping): " + (", ".join(fallback) if fallback else "none"),
        "unconfirmed: a real failure the run stopped at, so the false-alarm count is a lower"
        " bound (ADR 029)",
    ]
    return _table(CONFUSION, rows) + "\n\n" + "\n".join(f"- {n}" for n in notes)


def _settings(trials: Iterable[Trial]) -> list[str]:
    present = {t.setting for t in trials}
    return [s for s in SETTINGS if s in present]


def _counted(trials: Iterable[Trial]) -> list[tuple[JudgedTrial, RefereeTrial, RunSummary]]:
    return [
        (t.judged, t.referee, t.summary)
        for t in trials
        if t.judged is not None and t.referee is not None and t.summary is not None
    ]


def _wins(trials: Iterable[Trial]) -> int:
    return sum(t.judged is not None and t.judged.inserted for t in trials)


def _table(header: Sequence[str], rows: Iterable[Sequence[object]]) -> str:
    lines = ["| " + " | ".join(header) + " |", "|" + "|".join("---" for _ in header) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(_cell(c) for c in row) + " |")
    return "\n".join(lines)


def _cell(value: object) -> str:
    """A table cell: a `|` or a line break in a value read from a file (a port name, a class)
    cannot split the row."""
    if value is None:
        return "-"
    return " ".join(str(value).splitlines()).replace("|", "\\|")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.sim.report")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--run", type=Path, action="append", required=True, dest="runs")
    parser.add_argument(
        "--commit", help="the commit every engine run must name; required for the held-out"
    )
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        held_out = manifest.name.startswith(HELD_OUT_PREFIX)
        if held_out and args.commit is None:
            raise ReportInputError("--commit: the held-out manifest needs its commit named")
        if args.commit is not None and not _FULL_COMMIT.fullmatch(args.commit):
            raise ReportInputError("--commit: not a commit's 40 hex digits")  # nor "unknown"
        runs = load_runs(args.runs)
        if args.commit is not None and any(r.commit != args.commit for r in runs):
            raise ReportInputError("ledger: an engine run names another commit than --commit")
        trials = collect(manifest, runs)
    except (ReportInputError, ManifestError, ScoringInputError) as ex:  # fixed messages only
        print(f"report: {ex}", file=sys.stderr)
        return 1
    print(render(runs, trials, held_out=held_out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
