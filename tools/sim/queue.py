"""A Phase 2 session (#81; ADR 026, 028, 030): the queue of its units, and the loop that runs
them one engine run at a time, resuming from what the results disk holds.

**A session** is one directory on the results disk, one commit and one store:

    <results>/<session>/store.sqlite    the session's store (the policy's CELLRT_STORE)
    <results>/<session>/runs/<tag>/     one engine run, as `tools/sim/report.py` reads it:
        engine.yaml, ledger.jsonl, aic/ (scoring.yaml and the bags), bags/<trial id>.json,
        snapshot/store.sqlite, logs/, and outputs.json once its bags and store are copied

An engine run's tag is `<session>-<n>`, n counting from 001. Its manifest, queue and template
must be the files `SHA256SUMS` names (the pre-registration, D4), and every ledger row of the
session must name the checkout's commit (ADR 028: one session, one commit).

**Finished** (ADR 028). A unit, one task id in one setting, is finished when the session holds
a counted trial for it (`report.collect`: a finished attempt with its referee data), or when it
has used `MAX_ATTEMPTS` attempts. Attempts come from the ledger rows:
- an engine run that ended "ok" charges one attempt to every unit it held;
- one that ended otherwise charges one to the unit whose trial its sim log named last ("Trial
  n/m: trial_k", printed before the trial spawns, `aic_engine.cpp:594-598`): the units before
  it lost their `scoring.yaml` and rerun uncharged;
- one whose log names no trial charges no unit. Two in a row in one invocation stop it.
A code that no rerun can fix stops the invocation at once (`STOP_AT_ONCE`): the policy refused
a goal (ADR 027), the engine refused the config at parse, or the results disk failed a guard.
Two engine runs in a row that finish no unit (no counted trial, no unit out of attempts), for
whatever reason (a trial that always times out, bag dumps that always fail), stop it too:
reruns would spend the ceiling on nothing. So a unit whose trial crashes every engine run it is
in gets two attempts in one invocation, and its third in the next one.

**The order** (`queue.json`, ADR 026 and ADR 030), up to `max_trials_per_run` units a run:
- the pilot: A on the queue's `A`; then B on the scenes ADR 026 selects from A's counted
  trials; then the queue's `last` with A;
- tuning and held-out: engine runs alternate A and B, starting with A, each taking the next
  unfinished units of its setting in the queue's order and skipping the families the checkpoint
  did not select; when one setting has none left, the other's runs follow one another.
C-LLM is not run here yet: its call cap (D6) comes with the held-out session.

**Cost** (ADR 028, D8). The hours spent are the ledger rows' (an engine run) plus each run's
`outputs.json` (copying its outputs, which no row holds). The invocation first sets the guard
power-off for what is left of its split's guard after the session's hours, so the guard bounds
the session, not one invocation; when none is left, it sets one minute and stops. Before each
engine run, the session's hours and the phase's hours (the whole results disk) plus the run's
worst case (`worst_case_h`: every wait and call bound reached, the container's creation when
it is not there, the copy's included) must stay within their ceilings, at `USD_PER_HOUR`.

**One invocation per session.** It holds `session.lock` in the session's directory, taken with
O_EXCL and removed when it returns; a lock left by a hard kill must be removed by hand. It also
refuses to start while a sim or a policy runs (a runner killed alone leaves them running).
The checkout's `cellrt/`, `adapters/`, `tools/` and `uv.lock` must be its commit's, as the
ledger records it.

**Resuming.** Just before each engine run starts, the loop notes it in the run directory
(`started.json`: its tag, setting, units, commit and wall-clock start). An engine run killed
too hard to write its ledger row (SIGKILL, the OOM killer, a VM stop past its grace) is
recorded on resume from that note, with exit "killed" and an end at the last write to the
session's store or to the run's logs: its window then holds the attempts it left in the store,
as the report's check needs, and its hours count against the ceilings (ADR 028: "the ledger
shows it"). The note must render the run's `engine.yaml` again byte for byte, and its window
must lie after the run before it and before now. Like any run that ended badly, it charges
the trial its log named last. A run
directory with neither a row nor a note never reached its engine run; the session stops and
asks for it to be moved out. An engine run whose bags and store were not copied (the VM
stopped in between) is copied next: a later store copy holds the run's attempts too, and the
report joins them by the run's window.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
import os
import re
import stat
import sys
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol

from adapters.aic.manifest import Manifest, ManifestError
from adapters.aic.scoring import ScoringInputError
from tools.sim.batch import (
    BAG_DUMP_SCRIPT,
    BAG_DUMP_TIMEOUT_S,
    CALL_TIMEOUT_S,
    CREATE_TIMEOUT_S,
    POLICY_ERRORS,
    POLICY_READY_S,
    POLL_S,
    REPO,
    ROS_SETUP,
    SIM_READY_S,
    SNAPSHOT_GRACE_S,
    EnginePlan,
    EngineResult,
    PlanError,
    Runner,
    append_ledger,
    arm_guard,
    check_path,
    checkout_commit,
    container_exists,
    dump_bags,
    run_engine,
    snapshot_store,
)
from tools.sim.report import (
    MAX_YAML_BYTES,
    EngineRun,
    ReportInputError,
    Trial,
    collect,
    load_run,
)
from tools.sim.scenes import Scene, engine_config, write_yaml, yaml_text

MAX_ATTEMPTS = 3  # ADR 010, ADR 028
MAX_PILOT_B = 12  # D2
MAX_ENGINE_RUNS = 200  # one invocation; the pilot needs about 10
MAX_ENGINE_FAILURES = 2  # in a row, before any trial (ADR 028)
MAX_STALLED_RUNS = 2  # in a row that add no counted trial, for any reason
# What one engine run (`run_engine`) can take beyond its waits' bounds: the bounded calls (the
# teardown's three before and three after, the container check, the image id, the two starts),
# and what each of its three waits can run past its deadline (a poll, then one `alive` call per
# session it watches: 1, 2 and 2). A container's creation is added when it is not there.
ENGINE_CALLS_S = (8 + 2 + 5) * CALL_TIMEOUT_S + 3 * POLL_S
MAX_LOG_SCAN_BYTES = 256 << 20  # a sim log read past this names no trial
MAX_LEDGERS = 2000  # ledger files read for the phase's hours
MAX_QUEUE_BYTES = 1 << 20
MAX_SUMS_BYTES = 1 << 16
MAX_INPUT_BYTES = 16 << 20  # a file SHA256SUMS names (the manifests are about 100 KiB)
MAX_LEDGER_BYTES = 1 << 20  # one ledger file; a longer one fails closed, never undercounts
LOG_CHUNK_BYTES = 1 << 20
USD_PER_HOUR = 0.9  # ADR 028
PHASE_CEILING_USD = 25.0  # D8
# Per split (the manifest name's prefix): the session's ceiling in US$, its guard in minutes.
CEILINGS = {"pilot": (6.0, 298), "tune": (6.0, 400), "heldout": (11.0, 733)}  # ADR 028
MAX_NOTE_BYTES = 1 << 16  # started.json and outputs.json
OUTPUTS = "outputs.json"
STARTED = "started.json"  # the loop's note that it started the run's engine (module doc)
LOCK = "session.lock"  # one invocation per session
KILLED = "killed"  # the exit of a run recorded from its note
# The code a session runs: `config_hash` covers cellrt/ (`code_fingerprint`) and uv.lock
# (`lock_fingerprint`), adapters/ holds the AIC skills, and tools/ renders the engine configs,
# dumps the bags and decides what runs; none may differ from the commit a session records.
CHECKOUT_PATHS = ("cellrt", "adapters", "tools", "uv.lock")
STORE = "store.sqlite"
RUNS = "runs"
SUMS = "SHA256SUMS"
DISK_GUARDS = frozenset({"results_not_mounted", "results_disk_full", "results_dir_exists"})
STOP_AT_ONCE = frozenset({*POLICY_ERRORS.values(), "engine_parse_failed", *DISK_GUARDS})
_SESSION = re.compile(r"[a-z0-9][a-z0-9_.-]{0,55}")  # leaves room for "-NNN" in a 64-char tag
_NAMED = re.compile(rb"Trial [0-9]{1,4}/[0-9]{1,4}: (trial_[0-9]{1,4})")
_SUM_LINE = re.compile(r"([0-9a-f]{64})  ([A-Za-z0-9_.-]{1,64})")


class SessionError(ValueError):
    """A session that will not run; the message is fixed and never quotes a file."""


@dataclasses.dataclass(frozen=True)
class SplitQueue:
    """One split's entry in `queue.json`."""

    a: tuple[str, ...]
    b: tuple[str, ...] = ()
    order: tuple[str, ...] = ()  # the pilot's scenes, in ADR 026's order
    last: tuple[str, ...] = ()  # the pilot's `reversed`, run last with A
    per_run: int = 6

    @property
    def pilot(self) -> bool:
        return bool(self.order)


@dataclasses.dataclass(frozen=True)
class Session:
    """Everything an invocation runs with, checked before anything is written."""

    name: str
    root: Path  # <results>/<session>
    results_mount: Path
    manifest_path: Path
    manifest: Manifest
    scenes: Mapping[str, Scene]
    queue: SplitQueue
    families: frozenset[str] | None  # tuning and held-out: the checkpoint's selection
    template: Mapping[str, Any]
    workspace: Path
    base_env: Path
    commit: str

    @property
    def store(self) -> Path:
        return self.root / STORE

    @property
    def runs_dir(self) -> Path:
        return self.root / RUNS

    @property
    def split(self) -> str:
        return self.manifest.name.split("-", 1)[0]

    def plan(self, tag: str, setting: str, units: Sequence[str]) -> EnginePlan:
        run = self.runs_dir / tag
        limits = [float(self._scene(u).trial["task"]["time_limit"]) for u in units]
        return EnginePlan(
            tag=tag,
            config=run / "engine.yaml",
            manifest=self.manifest_path,
            store=self.store,
            results_dir=run / "aic",
            logs_dir=run / "logs",
            ledger=run / "ledger.jsonl",
            workspace=self.workspace,
            base_env=self.base_env,
            results_mount=self.results_mount,
            setting=setting,
            units=tuple(units),
            time_limit_s=max(limits),
            commit=self.commit,
        )

    def _scene(self, task_id: str) -> Scene:
        return self.scenes[self.manifest.units[task_id].scene]

    def render(self, plan: EnginePlan) -> dict[str, Any]:
        """The engine config of `plan`'s units, from the manifest's own scene entries."""
        units = [(self._scene(u), self.manifest.units[u].seed) for u in plan.units]
        return engine_config(units, self.template)


class Executor(Protocol):
    """What the loop needs from the machine: one engine run, then copying its outputs."""

    def run(self, plan: EnginePlan) -> EngineResult: ...
    def copy_outputs(self, plan: EnginePlan) -> None: ...


@dataclasses.dataclass(frozen=True)
class VmExecutor:
    """The VM's: `run_engine`, then each trial's bag dump and the store copy (ADR 028, step 7),
    and last `outputs.json`, so a run cut short before it is copied again on resume."""

    runner: Runner
    script: Path = BAG_DUMP_SCRIPT
    ros_setup: Path = ROS_SETUP

    def run(self, plan: EnginePlan) -> EngineResult:
        return run_engine(plan, self.runner)

    def copy_outputs(self, plan: EnginePlan) -> None:
        run_dir = plan.config.parent
        started = self.runner.wall()
        dumps = dump_bags(
            plan,
            self.runner,
            script=self.script,
            ros_setup=self.ros_setup,
            out_dir=run_dir / "bags",
        )
        self.runner.sleep(SNAPSHOT_GRACE_S)  # the policy was killed in the teardown
        snapshot_store(plan.store, run_dir / "snapshot")
        write_outputs(run_dir, dumps, max(self.runner.wall() - started, 0.0) / 3600.0)


def write_outputs(run_dir: Path, bags: Mapping[str, str], copy_hours: float) -> None:
    """`outputs.json`, written last: the bag dumps' results and the VM hours the copy took,
    which no ledger row holds (they count against the ceilings like the rows' hours). Whole or
    not at all, since every session's cost check reads it: written beside it, then renamed."""
    data = {"bags": dict(bags), "copy_hours": round(copy_hours, 6)}
    final = run_dir / OUTPUTS
    partial = final.with_name(final.name + ".partial")
    partial.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    os.replace(partial, final)


@dataclasses.dataclass(frozen=True)
class Record:
    """One engine run of the session, read back: its ledger row, the trial its log named last,
    and the hours copying its outputs took (0 before they are copied)."""

    run: EngineRun
    named: str | None
    copy_hours: float = 0.0

    @property
    def hours(self) -> float:
        return self.run.hours + self.copy_hours


# --- inputs ------------------------------------------------------------------------------------


def load_queue(path: Path, split: str) -> SplitQueue:
    """The split's entry of `queue.json`; SessionError with a fixed message otherwise."""
    try:
        data = json.loads(_bounded(path, MAX_QUEUE_BYTES, "queue"))
        entry = data["splits"][split]
        per_run = data["max_trials_per_run"]
        queue = SplitQueue(
            a=tuple(entry["A"]),
            b=tuple(entry.get("B", ())),
            order=tuple(entry.get("order", ())),
            last=tuple(entry.get("last", ())),
            per_run=per_run,
        )
    except SessionError:
        raise
    except (OSError, ValueError, RecursionError, KeyError, TypeError, AttributeError):
        raise SessionError("queue: not a queue with this split") from None
    lists = (queue.a, queue.b, queue.order, queue.last)
    if not all(isinstance(x, str) for xs in lists for x in xs):
        raise SessionError("queue: not a queue with this split")
    if not (isinstance(per_run, int) and not isinstance(per_run, bool) and 1 <= per_run <= 6):
        raise SessionError("queue: max_trials_per_run must be 1 to 6")
    return queue


def check_sums(sums: Path, files: Sequence[Path]) -> None:
    """Every file must be the one `SHA256SUMS` names (D4: the pre-registered files)."""
    try:
        text = _bounded(sums, MAX_SUMS_BYTES, "SHA256SUMS").decode("utf-8")
    except UnicodeDecodeError:
        raise SessionError("SHA256SUMS: unreadable") from None
    listed = {}
    for line in text.splitlines():
        match = _SUM_LINE.fullmatch(line)
        if match is None:
            raise SessionError("SHA256SUMS: not a sums file")
        listed[match.group(2)] = match.group(1)
    for path in files:
        raw = _bounded(path, MAX_INPUT_BYTES, "an input named in SHA256SUMS")
        if listed.get(path.name) != hashlib.sha256(raw).hexdigest():
            raise SessionError("an input is not the file SHA256SUMS names")


def _bounded(path: Path, limit: int, what: str) -> bytes:
    """A regular file's bytes, at most `limit` of them; SessionError naming `what` otherwise (a
    FIFO would block the open, so it is refused first)."""
    try:
        if not stat.S_ISREG(path.stat().st_mode):
            raise SessionError(f"{what}: not a regular file")
        with path.open("rb") as source:
            raw = source.read(limit + 1)
    except OSError:
        raise SessionError(f"{what}: unreadable") from None
    if len(raw) > limit:
        raise SessionError(f"{what}: over its bound")
    return raw


def check_queue(manifest: Manifest, queue: SplitQueue) -> None:
    """Every unit the queue can run is one of the manifest's, B's pilot units included."""
    ids = [*queue.a, *queue.b, *queue.last, *(f"{s}_r0" for s in queue.order)]
    if not all(task_id in manifest.units for task_id in ids):
        raise SessionError("queue: a unit the manifest does not hold")


def checkout_clean(runner: Runner, repo: Path) -> bool:
    """True when the policy's code in the checkout is its commit's: nothing changed, staged or
    not, and nothing untracked under `CHECKOUT_PATHS`. False when git cannot tell."""
    argv = ["git", "-C", str(repo), "status", "--porcelain", "--untracked-files=all", "--"]
    out = runner.output([*argv, *CHECKOUT_PATHS], timeout_s=CALL_TIMEOUT_S)
    return out is not None and out.strip() == ""


def check_session_name(name: str) -> None:
    if not _SESSION.fullmatch(name):
        raise SessionError("session: 1-56 lowercase letters, digits, '_', '.' or '-'")


def check_families(manifest: Manifest, queue: SplitQueue, families: Sequence[str] | None) -> None:
    if queue.pilot:
        if families is not None:
            raise SessionError("--families: the pilot runs every family")
        return
    if not families:
        raise SessionError("--families: name the families the checkpoint selected")
    known = {entry.family for entry in manifest.scenes.values()}
    if not set(families) <= known:
        raise SessionError("--families: a family the manifest does not hold")


# --- reading the session -----------------------------------------------------------------------

_NO_NOTE = "an engine run has no ledger row and never started its engine: move it out of runs/"
_NOT_NOTED = "an engine run's note is not one this runner wrote"


def note_start(plan: EnginePlan, wall: float) -> None:
    """The loop's note, written just before `plan`'s engine run starts (module doc), whole or not
    at all: written beside it, then renamed. Its start is rounded down, so the run's window
    begins no later than the run."""
    note = {
        "commit": plan.commit,
        "setting": plan.setting,
        "started_wall": math.floor(wall * 1000.0) / 1000.0,
        "tag": plan.tag,
        "units": list(plan.units),
    }
    final = plan.config.parent / STARTED
    partial = final.with_name(final.name + ".partial")
    partial.write_text(json.dumps(note, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    os.replace(partial, final)


def recover_killed(session: Session, now: float) -> list[str]:
    """Write the ledger row of each run directory that has none (or an empty one, cut before
    its first byte) but has the loop's note: an engine run killed before `run_engine` wrote its
    row (module doc). Returns their tags. The caller holds the session's lock and has seen no
    sim or policy running, so nothing writes to the store or the logs any more.

    SessionError, before any row is written for that run, for a directory with neither row nor
    note, a note of another commit, a note whose units do not render its `engine.yaml`, or one
    whose window would not fit between the run before it and `now` (a clock that went back)."""
    if not session.runs_dir.exists():
        return []
    paths = sorted(p for p in session.runs_dir.iterdir() if p.is_dir())
    if len(paths) > MAX_ENGINE_RUNS * 2:
        raise SessionError("the session holds too many engine runs")
    recovered = []
    last_end = 0.0
    for path in paths:
        ledger = path / "ledger.jsonl"
        if ledger.exists() and not (_empty(ledger) and (path / STARTED).exists()):
            last_end = max(last_end, _load(path).ended_wall)
            continue
        if not (path / STARTED).exists():
            raise SessionError(_NO_NOTE)
        tag, setting, units, commit, started = _read_note(session, path)
        if commit != session.commit:
            raise SessionError("an engine run of the session names another commit")
        try:
            plan = session.plan(tag, setting, units)
        except PlanError:
            raise SessionError(_NOT_NOTED) from None
        config = _bounded(plan.config, MAX_YAML_BYTES, "an engine run's config")
        if config != yaml_text(session.render(plan)).encode("utf-8"):
            raise SessionError("an engine run's note does not match its engine config")
        written = [*_store_files(session.store), plan.sim_log, plan.policy_log]
        # Rounded up: the row keeps 1 ms, and the window must hold the run's last event.
        ended = math.ceil(max([started, *_mtimes(written)]) * 1000.0) / 1000.0
        if not last_end <= started <= ended <= now:
            raise SessionError("an engine run's note does not fit between its neighbours' times")
        append_ledger(plan, started, ended, KILLED, hashlib.sha256(config).hexdigest(), "unknown")
        last_end = ended
        recovered.append(tag)
    return recovered


def _empty(path: Path) -> bool:
    try:
        return path.stat().st_size == 0
    except OSError:
        return False


def _load(path: Path) -> EngineRun:
    try:
        return load_run(path)
    except ReportInputError as ex:
        raise SessionError(f"an engine run: {ex}") from None


def _read_note(session: Session, path: Path) -> tuple[str, str, list[str], str, float]:
    bad = SessionError(_NOT_NOTED)
    try:
        note = json.loads(_bounded(path / STARTED, MAX_NOTE_BYTES, "note"))
    except (ValueError, RecursionError):  # SessionError (a ValueError) too: one message
        raise bad from None
    if not isinstance(note, dict):
        raise bad
    tag, setting, units = note.get("tag"), note.get("setting"), note.get("units")
    commit, started = note.get("commit"), note.get("started_wall")
    if (
        tag != path.name
        or not isinstance(setting, str)
        or not isinstance(commit, str)
        or not isinstance(units, list)
        or not units
        or not all(isinstance(u, str) and u in session.manifest.units for u in units)
        or len(set(units)) != len(units)
        or isinstance(started, bool)
        or not isinstance(started, int | float)
        or not 0 <= started < 1e11  # wall-clock seconds; an int too large fails here, not later
    ):
        raise bad
    return tag, setting, units, commit, float(started)


def _store_files(store: Path) -> list[Path]:
    return [store, store.with_name(store.name + "-wal")]


def _mtimes(paths: Sequence[Path]) -> list[float]:
    """The modification times of the paths that exist; a path that cannot be read is skipped."""
    times = []
    for path in paths:
        try:
            times.append(path.stat().st_mtime)
        except OSError:
            continue
    return times


def read_session(session: Session) -> list[Record]:
    """Each engine run of the session, in tag order; SessionError for a run without its row or
    of another commit, or one the report will not read."""
    if not session.runs_dir.exists():
        return []
    paths = sorted(p for p in session.runs_dir.iterdir() if p.is_dir())
    if len(paths) > MAX_ENGINE_RUNS * 2:
        raise SessionError("the session holds too many engine runs")
    records = []
    for path in paths:
        if not (path / "ledger.jsonl").exists():  # `recover_killed` has recorded every noted one
            raise SessionError(_NO_NOTE)
        run = _load(path)
        if run.commit != session.commit:
            raise SessionError("an engine run of the session names another commit")
        named = last_named_trial(path / "logs" / f"{run.tag}.sim.log")
        records.append(Record(run, named, copy_hours(path)))
    return records


def copy_hours(run_dir: Path) -> float:
    """The hours `outputs.json` records for copying the run's outputs; 0 before the copy."""
    path = run_dir / OUTPUTS
    if not path.exists():
        return 0.0
    try:
        data = json.loads(_bounded(path, MAX_NOTE_BYTES, "outputs.json"))
    except (ValueError, RecursionError):
        data = None
    value = data.get("copy_hours") if isinstance(data, dict) else None
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value < 1e6:
        raise SessionError("an engine run's outputs.json is not one this runner wrote")
    return float(value)


def last_named_trial(log: Path) -> str | None:
    """The trial id the engine's log named last, read a chunk at a time up to its bound; None
    when it names none, or cannot be read within the bound."""
    last: bytes | None = None
    tail = b""
    read = 0
    try:
        with log.open("rb") as source:
            while chunk := source.read(LOG_CHUNK_BYTES):
                read += len(chunk)
                if read > MAX_LOG_SCAN_BYTES:
                    return None
                window = tail + chunk
                for match in _NAMED.finditer(window):
                    last = match.group(1)
                tail = window[-64:]  # longer than one trial line, so a split line is found
    except OSError:
        return None
    return None if last is None else last.decode("ascii")


def attempts(records: Sequence[Record]) -> Counter[tuple[str, str]]:
    """Each unit's attempts, by ADR 028's rule (module docstring)."""
    charged: Counter[tuple[str, str]] = Counter()
    for r in records:
        if r.run.exit == "ok":
            for _, task_id, _ in r.run.trials:
                charged[(task_id, r.run.setting)] += 1
        elif r.named is not None:
            task_id = next((t for trial_id, t, _ in r.run.trials if trial_id == r.named), None)
            if task_id is not None:
                charged[(task_id, r.run.setting)] += 1
    return charged


def finished_units(records: Sequence[Record], trials: Sequence[Trial]) -> int:
    """How many units (task id, setting) are finished: counted, or out of attempts."""
    done = {(t.task_id, t.setting) for t in trials if t.judged is not None}
    done |= {unit for unit, n in attempts(records).items() if n >= MAX_ATTEMPTS}
    return len(done)


def counted_trials(session: Session, records: Sequence[Record]) -> list[Trial]:
    if not records:
        return []
    try:
        return collect(session.manifest, [r.run for r in records])
    except (ReportInputError, ManifestError, ScoringInputError) as ex:  # fixed messages
        raise SessionError(f"the session's runs: {ex}") from None


def next_tag(session: Session, records: Sequence[Record]) -> str:
    """`<session>-<n>`, one past the highest n the session holds."""
    numbers = []
    for r in records:
        prefix, _, n = r.run.tag.rpartition("-")
        if prefix != session.name or not (n.isdigit() and len(n) == 3):
            raise SessionError("an engine run's tag is not one of this session's")
        numbers.append(int(n))
    following = max(numbers, default=0) + 1
    if following > 999:
        raise SessionError("the session holds 999 engine runs")
    return f"{session.name}-{following:03d}"


# --- the order ---------------------------------------------------------------------------------


def pilot_b(session: Session, trials: Sequence[Trial]) -> list[str]:
    """ADR 026's B units: repeat 0 of each scene where A ended on a verdict that was not a false
    alarm, both A trials first, then one, each group in the pilot's order; at most 12."""
    qualifying: Counter[str] = Counter()
    for t in trials:
        if t.setting != "A" or t.judged is None or not t.judged.verdicts:
            continue
        if t.judged.verdicts[0].counted != "false_alarm":
            qualifying[t.scene] += 1
    last_scenes = {session.manifest.units[u].scene for u in session.queue.last}
    scenes = [s for s in session.queue.order if qualifying[s] and s not in last_scenes]
    ranked = sorted(scenes, key=lambda s: 0 if qualifying[s] >= 2 else 1)  # stable: the order
    return [f"{s}_r0" for s in ranked[:MAX_PILOT_B]]


def next_run(
    session: Session, records: Sequence[Record], trials: Sequence[Trial]
) -> tuple[str, list[str]] | None:
    """The next engine run's setting and units, or None when the queue is empty."""
    counted = {(t.task_id, t.setting) for t in trials if t.judged is not None}
    used = attempts(records)
    queue = session.queue

    def pending(setting: str, units: Sequence[str]) -> list[str]:
        return [
            u
            for u in units
            if (u, setting) not in counted
            and used[(u, setting)] < MAX_ATTEMPTS
            and _selected(session, u)
        ]

    if queue.pilot:
        left = pending("A", queue.a)
        if not left:
            left = pending("B", pilot_b(session, trials))  # A is done: its trials pick B
            if left:
                return "B", left[: queue.per_run]
            left = pending("A", queue.last)
        return ("A", left[: queue.per_run]) if left else None
    a, b = pending("A", queue.a), pending("B", queue.b)
    if not a and not b:
        return None
    if a and b:
        setting = "B" if records and records[-1].run.setting == "A" else "A"
    else:
        setting = "A" if a else "B"
    return setting, (a if setting == "A" else b)[: queue.per_run]


def _selected(session: Session, task_id: str) -> bool:
    if session.families is None:
        return True
    return session.scenes[session.manifest.units[task_id].scene].family in session.families


# --- cost --------------------------------------------------------------------------------------


def worst_case_h(plan: EnginePlan, *, create: bool = True) -> float:
    """The longest the engine run and the copy of its outputs can take, in hours, every bound
    of both reached (ADR 028): the waits for the sim, the policy and the trials, the bounded
    calls around them (`ENGINE_CALLS_S`), the container's creation when `create`, and per trial
    a bag dump, then the container's stop and the snapshot's grace."""
    run_s = SIM_READY_S + POLICY_READY_S + plan.trials_bound_s() + ENGINE_CALLS_S
    if create:
        run_s += CREATE_TIMEOUT_S
    copy_s = len(plan.units) * BAG_DUMP_TIMEOUT_S + CALL_TIMEOUT_S + SNAPSHOT_GRACE_S
    return (run_s + copy_s) / 3600.0


def phase_hours(results_mount: Path) -> float:
    """The VM hours the results disk records: every ledger row of the tracer's directories and
    of every session's engine runs, and every copy's hours in `outputs.json`. In a tracer's
    directory a line not in our ledger format is not ours to count; in a session's run, every
    line is ours, so one that is not a row stops the sum rather than undercount it."""
    tracers = list(results_mount.glob("*/ledger.jsonl"))
    runs = list(results_mount.glob(f"*/{RUNS}/*/ledger.jsonl"))
    copies = list(results_mount.glob(f"*/{RUNS}/*/{OUTPUTS}"))
    if len(tracers) + len(runs) + len(copies) > MAX_LEDGERS:
        raise SessionError("the results disk holds too many ledgers to sum")
    hours = 0.0
    for path, ours in [*((p, False) for p in tracers), *((p, True) for p in runs)]:
        try:
            lines = _bounded(path, MAX_LEDGER_BYTES, "a ledger on the results disk").decode()
        except UnicodeDecodeError:
            raise SessionError("a ledger on the results disk: unreadable") from None
        for line in lines.splitlines():
            if line.strip():
                hours += _row_hours(line, ours=ours)
    return hours + sum(copy_hours(p.parent) for p in copies)


def _row_hours(line: str, *, ours: bool) -> float:
    try:
        row = json.loads(line)
    except (ValueError, RecursionError):
        row = None
    if not isinstance(row, dict) or not {"tag", "units", "config_sha256"} <= row.keys():
        if ours:
            raise SessionError("a ledger of a session's engine run holds a line that is no row")
        return 0.0
    value = row.get("hours")
    if isinstance(value, bool) or not isinstance(value, int | float) or not 0 <= value < 1e6:
        raise SessionError("a ledger row on the results disk has no valid hours")
    return float(value)


def session_hours(records: Sequence[Record]) -> float:
    return sum(r.hours for r in records)


def check_cost(
    session: Session, records: Sequence[Record], plan: EnginePlan, *, create: bool = True
) -> None:
    """SessionError when the run's worst case (`create`: with the container's creation) would
    pass the session's or the phase's ceiling."""
    ceiling_usd, _ = CEILINGS[session.split]
    worst = worst_case_h(plan, create=create)
    spent = session_hours(records)
    if (spent + worst) * USD_PER_HOUR > ceiling_usd:
        raise SessionError("the session's ceiling would be passed: the queue stops here")
    if (phase_hours(session.results_mount) + worst) * USD_PER_HOUR > PHASE_CEILING_USD:
        raise SessionError("the phase's ceiling would be passed: the queue stops here")


# --- the loop ----------------------------------------------------------------------------------


def run_session(
    session: Session,
    runner: Runner,
    executor: Executor,
    *,
    power_off_when_done: bool = False,
    say: Callable[[str], None] = print,
) -> int:
    """Run the session's queue until it is empty (0), or stop (1) or refuse (2), with the
    reason said in a fixed message."""
    if not runner.is_mount(session.results_mount):
        say("session: the results disk is not mounted")
        return 2
    try:
        session.root.mkdir(parents=True, exist_ok=True)
        lock = session.root / LOCK
        os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644))
    except FileExistsError:
        say(f"session: another invocation holds {LOCK}; if none is running, remove it")
        return 2
    except OSError as ex:
        say(f"session: the session's lock could not be taken ({type(ex).__name__})")
        return 2
    try:
        return _run_locked(session, runner, executor, power_off_when_done, say)
    finally:
        lock.unlink(missing_ok=True)


def _run_locked(
    session: Session,
    runner: Runner,
    executor: Executor,
    power_off_when_done: bool,
    say: Callable[[str], None],
) -> int:
    if runner.alive("sim") or runner.alive("policy"):  # a runner killed alone, or the tracer
        say("session: a sim or a policy is still running: stop it before the session goes on")
        return 2
    failures = stalled = 0
    done_before: int | None = None
    try:
        for tag in recover_killed(session, runner.wall()):
            say(f"{tag}: no ledger row; recorded as {KILLED}")
        _, guard_min = CEILINGS[session.split]
        left_min = guard_min - math.ceil(session_hours(read_session(session)) * 60.0)
        if left_min < 1:  # the guard bounds the session, not one invocation (ADR 028)
            # Still a guard: an unwatched VM that resumed a spent session powers off.
            arm_guard(runner, 1)
            say("session: the session's guard time is spent: the queue stops here")
            return 1
        if not arm_guard(runner, left_min):
            say("session: the guard power-off could not be set")
            return 2
        session.runs_dir.mkdir(parents=True, exist_ok=True)
        for _ in range(MAX_ENGINE_RUNS):
            records = read_session(session)
            uncopied = [
                r
                for r in records  # a run cut short before its outputs were copied (module doc)
                if r.run.exit not in DISK_GUARDS and not (r.run.path / OUTPUTS).exists()
            ]
            for r in uncopied:
                executor.copy_outputs(session.plan(r.run.tag, r.run.setting, _units(r)))
            if uncopied:
                records = read_session(session)  # with the copies' hours
            trials = counted_trials(session, records)
            done = finished_units(records, trials)
            if done_before is not None:  # an engine run ran since the last count
                stalled = stalled + 1 if done <= done_before else 0
                if stalled >= MAX_STALLED_RUNS:
                    say(f"session: {stalled} engine runs in a row finished no unit")
                    return 1
            done_before = done
            step = next_run(session, records, trials)
            if step is None:
                say(f"session {session.name}: the queue is empty")
                if power_off_when_done:
                    runner.call(["sudo", "shutdown", "-h", "now"], timeout_s=120.0)
                return 0
            setting, units = step
            plan = session.plan(next_tag(session, records), setting, units)
            # Nothing removes the container between this check and the run's own (the teardown
            # stops it): a run that finds it there does not pay for its creation.
            check_cost(session, records, plan, create=not container_exists(runner))
            config = session.render(plan)
            plan.config.parent.mkdir(parents=True)  # a new run directory, never an old one
            write_yaml(plan.config, config)
            note_start(plan, runner.wall())  # last: a note means the engine run was started
            result = executor.run(plan)
            say(f"{plan.tag} ({setting}, {len(units)} units): {result.exit}")
            if result.exit in DISK_GUARDS:  # nothing ran; the disk itself is the problem
                say(f"session: stopped at {result.exit}")
                return 1
            executor.copy_outputs(plan)
            if result.exit in STOP_AT_ONCE:
                say(f"session: stopped at {result.exit}; no rerun can fix it")
                return 1
            named = last_named_trial(plan.sim_log)
            failures = failures + 1 if result.exit != "ok" and named is None else 0
            if failures >= MAX_ENGINE_FAILURES:
                say(f"session: {failures} engine runs in a row failed before any trial")
                return 1
    except SessionError as ex:
        say(f"session: {ex}")
        return 1
    except OSError as ex:  # the results disk: full, or gone mid-session
        say(f"session: a file on the results disk could not be written ({type(ex).__name__})")
        return 1
    say(f"session: over {MAX_ENGINE_RUNS} engine runs in one invocation")
    return 1


def _units(record: Record) -> list[str]:
    return [task_id for _, task_id, _ in record.run.trials]


def main_session(
    argv: Sequence[str], runner: Runner, executor: Executor | None = None, repo: Path = REPO
) -> int:
    """`python -m tools.sim.batch session ...`: the session's inputs checked before anything is
    written, then `run_session`. The commit is the checkout's (`repo`), never an argument."""
    import argparse

    from adapters.aic.manifest import load_manifest
    from tools.sim.scenes import EngineConfigError, load_template, scenes_of

    parser = argparse.ArgumentParser(prog="python -m tools.sim.batch session")
    parser.add_argument("--session", required=True, help="the session's name")
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--base-env", type=Path, required=True)
    parser.add_argument("--results-mount", type=Path, default=Path.home() / "results")
    parser.add_argument("--families", help="comma-separated: the checkpoint's selection")
    parser.add_argument("--ros-setup", type=Path, default=ROS_SETUP)
    parser.add_argument("--power-off-when-done", action="store_true")
    args = parser.parse_args(argv)

    def refuse(message: str) -> int:
        print(f"session: {message}", file=sys.stderr)
        return 2

    try:
        check_session_name(args.session)
        manifest_path = args.manifest.resolve()
        check_sums(manifest_path.parent / SUMS, [manifest_path, args.queue, args.template])
        manifest = load_manifest(manifest_path)
        if manifest.name.split("-", 1)[0] not in CEILINGS:
            return refuse("the manifest names no split with a ceiling")
        queue = load_queue(args.queue, manifest.name)
        check_queue(manifest, queue)
        families = None if args.families is None else args.families.split(",")
        check_families(manifest, queue, families)
        commit = checkout_commit(runner, repo)
        if commit == "unknown":
            return refuse("the checkout's commit could not be read")
        if not checkout_clean(runner, repo):  # ADR 028: one session, one configuration
            return refuse(
                "the checkout's cellrt/, adapters/, tools/ or uv.lock is not its commit's"
            )
        session = Session(
            name=args.session,
            root=args.results_mount / args.session,
            results_mount=args.results_mount,
            manifest_path=manifest_path,
            manifest=manifest,
            scenes=scenes_of(manifest.model_dump()),
            queue=queue,
            families=None if families is None else frozenset(families),
            template=load_template(args.template),
            workspace=args.workspace,
            base_env=args.base_env,
            commit=commit,
        )
        session.plan(f"{args.session}-001", "A", queue.a[:1] or queue.b[:1])  # paths checked
        if executor is None:
            check_path(args.ros_setup)
            check_path(BAG_DUMP_SCRIPT)
            executor = VmExecutor(runner, ros_setup=args.ros_setup)
    except (SessionError, ManifestError, EngineConfigError, PlanError) as ex:  # fixed messages
        return refuse(str(ex))
    except Exception as ex:  # an input we could not read or parse: its text is not ours
        return refuse(f"the inputs could not be read ({type(ex).__name__})")
    return run_session(
        session,
        runner,
        executor,
        power_off_when_done=args.power_off_when_done,
        say=lambda line: print(line, flush=True),
    )
