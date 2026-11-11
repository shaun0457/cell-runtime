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

**A perturbation that did not take effect** (ADR 026). Each scene of a family that changes one
value is checked on its counted trials' `world_ready` readbacks against its port type's
reference (`tools/sim/applied.py`): the first counted trial of a `nominal` scene, in the
session's own manifest or else in a reference session's (`--reference-manifest` and
`--reference-run`, whose engine runs need not be all of that session's). The held-out manifest
holds no `nominal` scene, so its report needs the tuning session as its reference, and refuses
a counted scene of a checked family that could not be checked against one. A scene with a trial
outside its tolerance is "not applied": listed, and left out of the A/B table, the claim and the
confusion table. Each reference is printed with its engine run's commit and the spread of its
port type's other nominal trials about it.

**The claim.** ADR 030 makes its one pooled claim on the held-out manifest (its name starts with
`heldout`); on any other manifest the A/B table is descriptive and no claim is printed.

**The tables.**
- Discovery: per scene, each trial's class per setting (ADR 026), and whether A's are mixed.
- Applied: per scene, the value its request changes, the readback farthest from it, and whether
  the perturbation took effect.
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
        [--reference-manifest M --reference-run DIR [--reference-run DIR ...]]
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
from cellrt.runtime.events import Event, WorldReady
from cellrt.runtime.store import EventStore, RunInfo
from tools.sim.applied import (
    GRASP_SHIFT,
    GRASP_SHIFT_TURN_TOLERANCE,
    GRASP_TURN,
    SHIFT,
    TILT,
    TOLERANCES,
    Change,
    Reading,
    Spread,
    checked_family,
    read_back,
    reads_back,
    requested_change,
    spread,
)
from tools.sim.batch import SETTINGS

LEDGER = Path("ledger.jsonl")
CONFIG = Path("engine.yaml")
SCORING = Path("aic") / "scoring.yaml"
BAGS = Path("bags")
STORE_COPY = Path("snapshot") / "store.sqlite"

MAX_YAML_BYTES = 1 << 20
MAX_BAG_JSON_BYTES = 16 << 20
MAX_STORE_COPY_BYTES = 2 << 30  # a held-out session's store is about 0.1 GiB
MAX_LEDGER_BYTES = 64 << 10
MAX_RUNS = 500
POOLED = "all"
HELD_OUT_PREFIX = "heldout"  # ADR 030: the manifest the claim is made on
NOMINAL_PREFIX = "nominal"  # ADR 026: the control scenes, each port type's reference
APPLIED, NOT_APPLIED = "applied", "not applied"
NOT_CHECKED, NO_REFERENCE, NO_READBACK = "not checked", "no reference", "no readback"

_TRIAL_ID = re.compile(r"trial_[0-9]{1,4}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}|unknown")
_FULL_COMMIT = re.compile(r"[0-9a-f]{40}")
_TAG = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")  # as tools/sim/batch.py checks a plan's tag
_EXIT = re.compile(r"[a-z0-9_]{1,64}")  # "ok" or a reason code
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")  # C0, DEL and C1
_PORT_TYPE = re.compile(r"[a-z0-9_]{1,40}")  # "sfp", "sc": a reference's key, printed
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
    ready: WorldReady | None = None  # the counted run's readback of the spawned scene

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
    ready: WorldReady | None = None


@dataclass(frozen=True)
class Reference:
    """A port type's reference (ADR 026): its `nominal` scene's request, and the readback of
    the first counted trial of it that read every pose; the commit of the engine run it came
    from, and how far the port type's other such trials read from it."""

    trial: Mapping[str, Any]
    ready: WorldReady
    run_id: str
    commit: str = "-"  # "-": the engine runs were not given
    spread: Spread | None = None


@dataclass(frozen=True)
class Applied:
    """Whether a scene's perturbation took effect, from its counted trials' readbacks."""

    scene: str
    family: str
    status: str
    change: Change | None = None
    worst: Reading | None = None  # the readback farthest from the request
    read: int = 0  # counted trials read back
    counted: int = 0


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


def collect(
    manifest: Manifest, runs: Sequence[EngineRun], *, every_run: bool = True
) -> list[Trial]:
    """Each unit and setting the engine runs held, with the trial that counts (ADR 028).
    `every_run` False skips the check that no engine run was left out: for a reference session,
    whose nominal trials are all that is read."""
    ordered = sorted(runs, key=lambda r: r.started_wall)
    by_unit: dict[tuple[str, str], list[_Attempt]] = defaultdict(list)
    latest: list[_Stored] = []
    for run in ordered:
        attempts, stored = _attempts(run, manifest)
        if stored is not None:
            latest = stored  # each copy holds the whole store as it was after its engine run
        for task_id, attempt in attempts:
            by_unit[(task_id, run.setting)].append(attempt)
    if every_run:
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
                ready=None if counted is None else counted.ready,
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
    _check_store_copy(store_path)
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


def _check_store_copy(path: Path) -> None:
    """A store copy is a regular file, and with its WAL within `MAX_STORE_COPY_BYTES`: every run
    in it is read."""
    size = 0
    for part in (path, path.with_name(path.name + "-wal")):
        try:
            mode = part.stat()
        except FileNotFoundError:
            continue  # a store closed cleanly has no WAL
        except OSError:
            raise ReportInputError("store copy: unreadable") from None
        if not stat.S_ISREG(mode.st_mode):
            raise ReportInputError("store copy: not a regular file")
        size += mode.st_size
    if size > MAX_STORE_COPY_BYTES:
        raise ReportInputError(f"store copy: over {MAX_STORE_COPY_BYTES} bytes")


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
    ready = next((e.payload for e in events if isinstance(e.payload, WorldReady)), None)
    return _Attempt(run, trial_id, judged, referee, summarize_run(events), ready)


def _first(store: EventStore, run_id: str) -> Event:
    return _events(store, run_id, limit=1)[0]  # a listed run has its run_started


def _events(store: EventStore, run_id: str, limit: int | None = None) -> list[Event]:
    """The run's events; a payload the schema does not hold is refused by a fixed message,
    since pydantic's would quote the store's content."""
    try:
        return store.events(run_id, limit=limit)
    except (pydantic.ValidationError, json.JSONDecodeError, RecursionError):
        raise ReportInputError(_NOT_OURS) from None


# applied ----------------------------------------------------------------------------------------


def references(
    manifest: Manifest, trials: Sequence[Trial], runs: Sequence[EngineRun] = ()
) -> dict[str, Reference]:
    """Per port type, the first counted trial of a `nominal` scene (by setting, seed and unit)
    whose readback gives every pose the checks read (`reads_back`), with the commit of its
    engine run (one of `runs`) and the spread of the port type's other such trials about it."""
    commit_of = {r.tag: r.commit for r in runs}
    found: dict[str, list[tuple[Trial, Mapping[str, Any], WorldReady, str]]] = defaultdict(list)
    for t in sorted(trials, key=lambda t: (t.setting, t.seed, t.task_id)):
        ready, judged = t.ready, t.judged
        if not t.family.startswith(NOMINAL_PREFIX) or judged is None or ready is None:
            continue
        request = manifest.scenes[t.scene].trial
        port_type = _port_type(request)
        if port_type is not None and reads_back(request, ready):
            found[port_type].append((t, request, ready, judged.run_id))
    out = {}
    for port_type, ((t, request, ready, run_id), *others) in found.items():
        out[port_type] = Reference(
            request,
            ready,
            run_id,
            commit_of.get(t.tag or "", "-"),
            spread(request, ready, [(r, w) for _, r, w, _ in others]),
        )
    return out


def applied_scenes(
    manifest: Manifest, trials: Sequence[Trial], refs: Mapping[str, Reference]
) -> dict[str, Applied]:
    """Each scene's check (module docstring), by scene id."""
    by_scene: dict[str, list[Trial]] = defaultdict(list)
    for t in trials:
        by_scene[t.scene].append(t)
    out = {}
    for scene, members in sorted(by_scene.items()):
        family = manifest.scenes[scene].family
        request = manifest.scenes[scene].trial
        counted = [t for t in members if t.judged is not None]
        ref = refs.get(_port_type(request) or "")
        change = None if ref is None else requested_change(request, ref.trial)
        if not checked_family(family) or (ref is not None and change is None):
            out[scene] = Applied(scene, family, NOT_CHECKED, counted=len(counted))
            continue
        if ref is None or change is None:
            out[scene] = Applied(scene, family, NO_REFERENCE, counted=len(counted))
            continue
        readings = [
            reading
            for t in counted
            if t.ready is not None
            and (reading := read_back(change, request, t.ready, ref.trial, ref.ready)) is not None
        ]
        if not readings:
            out[scene] = Applied(scene, family, NO_READBACK, change, counted=len(counted))
            continue
        status = APPLIED if all(r.applied for r in readings) else NOT_APPLIED
        worst = max(readings, key=lambda r: r.badness)
        out[scene] = Applied(scene, family, status, change, worst, len(readings), len(counted))
    return out


def not_applied(applied: Mapping[str, Applied]) -> set[str]:
    return {scene for scene, a in applied.items() if a.status == NOT_APPLIED}


def check_held_out(applied: Mapping[str, Applied]) -> None:
    """ADR 030's claim leaves out the scenes whose perturbation did not take effect, so on the
    held-out manifest every counted scene of a checked family must have been checked: refuse
    one with no reference, or whose request does not change one value from its reference's (a
    wrong reference session, or a manifest the check does not fit). A scene with no readback
    stays in, named by the Applied table."""
    unchecked = [
        a
        for a in applied.values()
        if a.counted and checked_family(a.family) and a.status in (NO_REFERENCE, NOT_CHECKED)
    ]
    if unchecked:
        raise ReportInputError(
            "--reference-run: a held-out scene of a checked family could not be checked against"
            f" a reference ({len(unchecked)} scenes)"
        )


def _port_type(trial: Mapping[str, Any]) -> str | None:
    task = trial.get("task")
    port_type = task.get("port_type") if isinstance(task, Mapping) else None
    if not isinstance(port_type, str) or not _PORT_TYPE.fullmatch(port_type):
        return None
    return port_type


# tables -----------------------------------------------------------------------------------------


def render(
    runs: Sequence[EngineRun],
    trials: Sequence[Trial],
    *,
    held_out: bool,
    applied: Mapping[str, Applied] | None = None,
    refs: Mapping[str, Reference] | None = None,
) -> str:
    """The tables; the scenes `applied` marks "not applied" are left out of every rate."""
    applied = applied or {}
    left_out = not_applied(applied)
    rated = [t for t in trials if t.scene not in left_out]
    return "\n\n".join(
        (
            session_line(runs),
            "## Discovery",
            discovery_table(trials),
            "## Applied",
            applied_table(applied, refs or {}),
            "## Reconciliation",
            reconciliation_table(trials),
            "## A/B",
            ab_table(rated, held_out=held_out),
            "## Confusion",
            confusion_table(rated),
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


APPLIED_TABLE = (
    "scene",
    "family",
    "change",
    "requested",
    "read back",
    "tolerance",
    "read",
    "status",
)


def applied_table(applied: Mapping[str, Applied], refs: Mapping[str, Reference]) -> str:
    rows: list[list[object]] = []
    for scene in sorted(applied):
        a = applied[scene]
        c, w = a.change, a.worst
        rows.append(
            [
                a.scene,
                a.family,
                None if c is None else c.kind,
                None if c is None else _amount(c.kind, c.size),
                None if w is None else _reading(w),
                None if c is None else _tolerance(c.kind),
                f"{a.read}/{a.counted}",
                a.status,
            ]
        )
    named = "; ".join(
        f"{port_type}: {r.run_id} (commit {r.commit})" for port_type, r in sorted(refs.items())
    )
    left_out = sorted(not_applied(applied))
    notes = [
        f"reference per port type: {named or 'none'}",
        *(_spread_note(port_type, r.spread) for port_type, r in sorted(refs.items())),
        "left out of every rate (not applied): " + (", ".join(left_out) or "none"),
    ]
    if any(a.status == NO_REFERENCE for a in applied.values()):
        notes.append(
            "no reference: no counted nominal trial of the port type read every pose; the"
            " held-out session takes the tuning session's (--reference-manifest, --reference-run)"
        )
    unread = sorted(scene for scene, a in applied.items() if a.status == NO_READBACK)
    if unread:
        notes.append(
            "kept in every rate, unchecked (no readback: no counted trial read the poses its"
            " check needs): " + ", ".join(unread)
        )
    lines = "\n".join(f"- {_CONTROL.sub('?', n)}" for n in notes)
    return _table(APPLIED_TABLE, rows) + "\n\n" + lines


def _spread_note(port_type: str, s: Spread | None) -> str:
    if s is None or s.trials == 0:
        return f"nominal spread, {port_type}: no other nominal trial read every pose"
    wider = "; wider than a tolerance" if s.wider_than_a_tolerance else ""
    return (
        f"nominal spread, {port_type}: {s.trials} other trials; port"
        f" {_amount(TILT, s.port_turn)}, {_amount(SHIFT, s.port_shift)} in the board's frame;"
        f" plug {_amount(GRASP_SHIFT, s.plug_shift)}, {_amount(GRASP_TURN, s.plug_turn)} in the"
        f" gripper's frame{wider}"
    )


def _amount(kind: str, value: float) -> str:
    if kind in (SHIFT, GRASP_SHIFT):
        return f"{value * 1000.0:.2f} mm"
    if kind == GRASP_TURN:
        return f"{value:.3f} rad"
    return f"{math.degrees(value):.2f} deg"


def _reading(r: Reading) -> str:
    turn = "" if r.turn is None else f", turned {r.turn:.3f} rad"
    return _amount(r.change.kind, r.measured) + turn


def _tolerance(kind: str) -> str:
    within = _amount(kind, TOLERANCES[kind])
    if kind == GRASP_SHIFT:
        return f"{within}; turn {GRASP_SHIFT_TURN_TOLERANCE:.3f} rad"
    return within


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
                _mapping(j),
                j.trial_class,
            ]
        )
    return _table(RECONCILIATION, rows)


def _mapping(j: JudgedTrial) -> str | None:
    """How the insertion's time was placed: on the run's clock, not at all with verdicts to
    judge (the whole-trial fallback), or not at all with none; None: no insertion."""
    if j.insertion_t is not None:
        return "mapped"
    if j.fallback:
        return "whole trial"
    return "unmapped" if j.inserted else None


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
    cannot split the row, and a control character (an ESC sequence) cannot reach the
    terminal."""
    if value is None:
        return "-"
    text = _CONTROL.sub("?", " ".join(str(value).splitlines()))
    return text.replace("|", "\\|")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.sim.report")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--run", type=Path, action="append", required=True, dest="runs")
    parser.add_argument(
        "--commit", help="the commit every engine run must name; required for the held-out"
    )
    parser.add_argument(
        "--reference-manifest",
        type=Path,
        help="the manifest of a session whose nominal trials are the references (ADR 026);"
        " required for the held-out",
    )
    parser.add_argument(
        "--reference-run", type=Path, action="append", default=[], dest="reference_runs"
    )
    args = parser.parse_args(argv)
    try:
        manifest = load_manifest(args.manifest)
        held_out = manifest.name.startswith(HELD_OUT_PREFIX)
        if held_out and args.commit is None:
            raise ReportInputError("--commit: the held-out manifest needs its commit named")
        if args.commit is not None and not _FULL_COMMIT.fullmatch(args.commit):
            raise ReportInputError("--commit: not a commit's 40 hex digits")  # nor "unknown"
        if (args.reference_manifest is None) != (not args.reference_runs):
            raise ReportInputError("--reference-manifest and --reference-run go together")
        if held_out and args.reference_manifest is None:
            raise ReportInputError(
                "--reference-manifest: the held-out manifest holds no nominal scene; name the"
                " tuning session's"
            )
        runs = load_runs(args.runs)
        if args.commit is not None and any(r.commit != args.commit for r in runs):
            raise ReportInputError("ledger: an engine run names another commit than --commit")
        trials = collect(manifest, runs)
        refs: dict[str, Reference] = {}
        if args.reference_manifest is not None:
            other = load_manifest(args.reference_manifest)
            other_runs = load_runs(args.reference_runs)
            refs = references(other, collect(other, other_runs, every_run=False), other_runs)
        refs |= references(manifest, trials, runs)  # the session's own, where it has one, win
        applied = applied_scenes(manifest, trials, refs)
        if held_out:
            check_held_out(applied)
    except (ReportInputError, ManifestError, ScoringInputError) as ex:  # fixed messages only
        print(f"report: {ex}", file=sys.stderr)
        return 1
    print(render(runs, trials, held_out=held_out, applied=applied, refs=refs))
    return 0


if __name__ == "__main__":
    sys.exit(main())
