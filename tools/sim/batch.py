"""One engine run on the VM (ADR 028): the steps of Phase 1's out-of-repo `vmc_run.sh`, with
our engine config passed as `aic_engine_config_file:=`, every wait bounded, every exit torn
down, and one ledger row per run.

`run_engine(plan, runner)`:
1. refuses to start unless the results disk is mounted with `MIN_FREE_BYTES` free and the run's
   results directory is new (a VM stop leaves the disk unmounted, and files written then hide
   under the mount);
2. stops whatever an earlier run left (the policy and sim sessions, the distrobox container);
3. creates the eval container if it is missing, and reads the image id it runs, for the ledger
   (the image is pulled by a moving tag);
4. writes the policy's env file: it sets `CELLRT_SETTING`, `CELLRT_MANIFEST`, `CELLRT_STORE` and
   `CELLRT_SESSION` from the plan and unsets every other policy variable (`POLICY_ENV`), so a
   `CELLRT_FAULT` left in the operator's shell cannot reach a scene run. Never a key: the model
   key stays in the VM's own env (ADR 017);
5. removes the logs an earlier run under the same tag left, starts the sim with the engine
   config, and waits for the engine to ask for the policy;
6. starts the policy, and waits for it to load;
7. waits for the engine to process every trial, while the sim and the policy both live;
8. on every exit path stops the policy, the sim and the container, with SIGHUP and SIGTERM
   ignored so a second signal cannot cut the teardown short, then appends its row to the
   ledger (`ledger.jsonl`): the times, the units, the exit code, the VM hours, the commit, the
   image id and the engine config's SHA-256. The one exception is an unmounted results disk,
   which holds the ledger: the code is in the result only.

Every wait also watches the sim log for the engine's error lines (`ENGINE_ERRORS`) and the
policy log for its refusals (`POLICY_ERRORS`: a goal refused by the manifest or the settings
ends the run, ADR 027-028), and ends the run at once with that line's code. The bounds:
`SIM_READY_S` (Phase 1 waited 900 s for a cold start), `POLICY_READY_S`, and for the trials
trials x (time_limit / `RTF` + `TRIAL_OVERHEAD_S`). Logs are read a chunk at a time from where
the last poll stopped (`LogScan`), at most `MAX_CHUNKS_PER_POLL` chunks a poll, so memory stays
bounded however long a log grows.

The `Runner` protocol is the only part that touches processes and disks (`TmuxRunner` on the
VM, which names tmux sessions exactly), so the waits, the bounds and the teardown are tested
with a fake one.

After a run, `dump_bags` turns each trial's bag into the referee's JSON inside the eval
container (`in_container/bag_dump.py`), and `snapshot_store` copies the store for the reports.

`python -m tools.sim.batch tracer ...` runs the tracer of #78: the sample config's first trial
as the scene `nominal_sfp`, once, then dumps its bag and copies the store. It first sets the
guard power-off (`shutdown -h +TRACER_GUARD_MIN`, ADR 028), and refuses to run without it.
SIGHUP and SIGTERM become an ordinary exit there, so a dropped SSH session still tears down
and writes its row.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Iterator, Sequence
from pathlib import Path
from typing import Protocol

from adapters.aic.manifest import (
    MANIFEST_INVALID,
    MANIFEST_TOO_LARGE,
    MANIFEST_UNREADABLE,
    TASK_MISMATCH,
    TASK_NOT_IN_MANIFEST,
)
from tools.sim.scenes import MAX_CONFIG_BYTES

RTF = 0.43  # the real-time factor on the g2-standard-8 + L4 (#28)
SIM_READY_S = 900.0
POLICY_READY_S = 180.0
TRIAL_OVERHEAD_S = 60.0
POLL_S = 5.0
CALL_TIMEOUT_S = 120.0
CREATE_TIMEOUT_S = 1800.0  # pulling the eval image on a fresh disk
CHUNK_BYTES = 1 << 20
MAX_CHUNKS_PER_POLL = 64
MAX_OUTPUT_BYTES = 4096  # a command's stdout we read: one id or one commit
MIN_FREE_BYTES = 10 * 1000**3  # ADR 028: 10 GB free on the results disk
SNAPSHOT_GRACE_S = 10.0  # after the teardown, before the store is copied
TRACER_GUARD_MIN = 60  # ADR 028's guard for the tracer session
SIM_READY = "No node with name 'aic_model'"  # the engine waits for the policy (vmc_run.sh)
POLICY_READY = "Using policy"
ALL_DONE = "All Trials Processed"  # aic_engine.cpp:640
ENGINE_ERRORS = {  # ADR 028: each ends the run at once
    "Failed to parse trial": "engine_parse_failed",  # aic_engine.cpp:414-424
    "Engine failed to initialize": "engine_init_failed",  # :352
    "Engine Stopped with Errors": "engine_stopped_with_errors",  # :654
}
REFUSED = "cell-runtime goal refused"  # RuntimePolicy logs every refusal after it
POLICY_ERRORS = {  # ADR 027-028: a refused goal ends the run, and no rerun can fix it
    f"{REFUSED}: {MANIFEST_UNREADABLE}": "manifest_unreadable",
    f"{REFUSED}: {MANIFEST_TOO_LARGE}": "manifest_too_large",
    f"{REFUSED}: {MANIFEST_INVALID}": "manifest_invalid",
    f"{REFUSED}: {TASK_NOT_IN_MANIFEST}": "task_not_in_manifest",
    f"{REFUSED}: {TASK_MISMATCH}": "task_mismatch",
    # Last, so a whole line names its code: a refused setting, or a line read in two parts.
    REFUSED: "goal_refused",
    "cell-runtime setup failed": "policy_setup_failed",  # every goal fails the same way
}
ERROR_CODES = {**ENGINE_ERRORS, **POLICY_ERRORS}
# Every variable the policy reads (adapters/aic); a test keeps this list complete.
POLICY_ENV = (
    "CELLRT_FAULT",
    "CELLRT_MANIFEST",
    "CELLRT_MAX_TOKENS",
    "CELLRT_PLANNER",
    "CELLRT_REASONING",
    "CELLRT_SESSION",
    "CELLRT_SETTING",
    "CELLRT_STORE",
)
IMAGE = "ghcr.io/intrinsic-dev/aic/aic_eval:latest"
CONTAINER = "aic_eval"
SETTINGS = ("A", "B", "C")
HELD_SIGNALS = ("SIGHUP", "SIGTERM")
_TAG = re.compile(r"[a-z0-9][a-z0-9_.-]{0,63}")
_TASK_ID = re.compile(r"[a-z0-9_]{1,64}")
_COMMIT = re.compile(r"[0-9a-f]{40}|unknown")
_IMAGE_ID = re.compile(r"sha256:[0-9a-f]{64}")
_UNSAFE_PATH = re.compile(r"[\s'\"`$;|&<>()*?!{}\[\]#]|[\x00-\x1f\x7f]")
_DBX = ["env", "DBX_CONTAINER_MANAGER=docker", "distrobox"]


class Runner(Protocol):
    """What an engine run needs from the machine."""

    def call(self, argv: list[str], *, timeout_s: float) -> int: ...
    def output(self, argv: list[str], *, timeout_s: float) -> str | None: ...
    def start(self, session: str, command: str) -> None: ...
    def alive(self, session: str) -> bool: ...
    def stop(self, session: str) -> None: ...
    def read_bytes(self, path: Path, offset: int, size: int) -> bytes: ...
    def is_mount(self, path: Path) -> bool: ...
    def free_bytes(self, path: Path) -> int: ...
    def now(self) -> float: ...
    def wall(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class EngineRunError(RuntimeError):
    """An engine run that ended early; `code` names why, for the ledger."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class PlanError(ValueError):
    """A plan or a path we will not run with. The message is ours, never a path."""


def check_path(path: Path) -> None:
    """Refuse a path the commands cannot carry safely: relative, with `..`, or with a space or
    a shell character. The commands quote every path, but distrobox rebuilds the command inside
    the container, so such a path is refused rather than trusted to survive."""
    if not path.is_absolute():
        raise PlanError("every path must be absolute")
    if ".." in path.parts:
        raise PlanError("a path holds '..'")
    if _UNSAFE_PATH.search(str(path)):
        raise PlanError("a path holds a space or a shell character")


@dataclasses.dataclass(frozen=True)
class EnginePlan:
    """One engine run: its config, the policy's inputs, and where its outputs go. Everything it
    writes lies under `results_mount`, the results disk."""

    tag: str
    config: Path
    manifest: Path
    store: Path
    results_dir: Path  # AIC_RESULTS_DIR: scoring.yaml and the bags; new for every engine run
    logs_dir: Path
    ledger: Path
    workspace: Path  # the AIC workspace where `pixi run` finds aic_model
    base_env: Path  # the VM's own env file (PYTHONPATH, the model key's loader)
    results_mount: Path  # the results disk's mount point (`~/results` on the VM)
    setting: str
    units: tuple[str, ...]  # task ids, in the config's order
    time_limit_s: float
    commit: str = "unknown"  # the checkout's commit, for the ledger

    def __post_init__(self) -> None:
        if not _TAG.fullmatch(self.tag):
            raise PlanError("tag must be 1-64 lowercase letters, digits, '_', '.' or '-'")
        if self.setting not in SETTINGS:
            raise PlanError(f"setting must be one of: {', '.join(SETTINGS)}")
        if not self.units or not all(_TASK_ID.fullmatch(u) for u in self.units):
            raise PlanError("units must be one or more task ids")
        if not self.time_limit_s > 0:
            raise PlanError("time_limit_s must be positive")
        if not _COMMIT.fullmatch(self.commit):
            raise PlanError("commit must be 40 hex digits or 'unknown'")
        written = (self.store, self.results_dir, self.logs_dir, self.ledger)
        read = (self.config, self.manifest, self.workspace, self.base_env, self.results_mount)
        for path in (*written, *read):
            check_path(path)
        # No `..` part (check_path), so a literal prefix is the real one.
        if not all(p.is_relative_to(self.results_mount) for p in written):
            raise PlanError("the store, results, logs and ledger must be on the results disk")

    @property
    def sim_log(self) -> Path:
        return self.logs_dir / f"{self.tag}.sim.log"

    @property
    def policy_log(self) -> Path:
        return self.logs_dir / f"{self.tag}.policy.log"

    @property
    def env_file(self) -> Path:
        return self.logs_dir / f"{self.tag}.env"

    def trials_bound_s(self) -> float:
        return len(self.units) * (self.time_limit_s / RTF + TRIAL_OVERHEAD_S)


@dataclasses.dataclass(frozen=True)
class EngineResult:
    tag: str
    exit: str  # "ok" or the reason code
    started_wall: float
    ended_wall: float


class LogScan:
    """Finds the first of `patterns` in a growing log, reading only what was appended since the
    last call, at most `MAX_CHUNKS_PER_POLL` chunks a call; a pattern split across two reads is
    still found. When one window holds several patterns, the first in `patterns` wins."""

    def __init__(self, path: Path, patterns: Sequence[str]) -> None:
        if not patterns or not all(patterns):
            raise ValueError("a scan needs one or more non-empty patterns")
        self.path = path
        self.patterns = tuple(p.encode("utf-8") for p in patterns)
        self.offset = 0
        self._tail = b""
        self._keep = max(len(p) for p in self.patterns) - 1

    def find(self, source: _ReadsBytes) -> str | None:
        for _ in range(MAX_CHUNKS_PER_POLL):
            chunk = source.read_bytes(self.path, self.offset, CHUNK_BYTES)
            if not chunk:
                return None
            self.offset += len(chunk)
            window = self._tail + chunk
            for pattern in self.patterns:
                if pattern in window:
                    return pattern.decode("utf-8")
            self._tail = window[-self._keep :] if self._keep else b""
            if len(chunk) < CHUNK_BYTES:
                return None
        return None


class _ReadsBytes(Protocol):
    def read_bytes(self, path: Path, offset: int, size: int) -> bytes: ...


def run_engine(plan: EnginePlan, runner: Runner) -> EngineResult:
    """Run `plan` once; the result's `exit` is "ok" or why it ended early. An unexpected error
    is raised after the teardown and the ledger row (exit "runner_error"); so is an interrupt
    (exit "interrupted")."""
    started = runner.wall()
    config_sha = _file_sha256(plan.config)
    code = "runner_error"
    image = "unknown"
    try:
        _guard(plan, runner)
        _stop_all(runner)
        _ensure_container(runner)
        image = _image_id(runner)
        _write_env(plan)
        for log in (plan.sim_log, plan.policy_log):
            log.unlink(missing_ok=True)  # never read an earlier run's ready line
        errors = (
            LogScan(plan.sim_log, tuple(ENGINE_ERRORS)),
            LogScan(plan.policy_log, tuple(POLICY_ERRORS)),
        )
        runner.start("sim", _sim_command(plan))
        sim_ready = LogScan(plan.sim_log, (SIM_READY,))
        _wait(runner, sim_ready, SIM_READY_S, ("sim",), "sim_not_ready", errors)
        runner.start("policy", _policy_command(plan))
        policy_ready = LogScan(plan.policy_log, (POLICY_READY,))
        _wait(runner, policy_ready, POLICY_READY_S, ("policy", "sim"), "policy_not_ready", errors)
        done = LogScan(plan.sim_log, (ALL_DONE,))
        bound = plan.trials_bound_s()
        _wait(runner, done, bound, ("sim", "policy"), "trials_not_processed", errors)
        code = "ok"
    except EngineRunError as ex:
        code = ex.code
    except (KeyboardInterrupt, SystemExit):
        code = "interrupted"
        raise
    finally:
        with _signals_held():
            try:
                _stop_all(runner)
            finally:
                ended = runner.wall()
                # The ledger lives on the results disk: with the disk unmounted, a row would
                # land on the boot disk and hide under the mount. The caller has the code.
                if code != "results_not_mounted":
                    _append_ledger(plan, started, ended, code, config_sha, image)
    return EngineResult(tag=plan.tag, exit=code, started_wall=started, ended_wall=ended)


def _guard(plan: EnginePlan, runner: Runner) -> None:
    if not runner.is_mount(plan.results_mount):
        raise EngineRunError("results_not_mounted")
    if runner.free_bytes(plan.results_mount) < MIN_FREE_BYTES:
        raise EngineRunError("results_disk_full")
    if plan.results_dir.exists():
        raise EngineRunError("results_dir_exists")


def _wait(
    runner: Runner,
    scan: LogScan,
    bound_s: float,
    sessions: tuple[str, ...],
    code: str,
    errors: Sequence[LogScan],
) -> None:
    """Until `scan` finds its line: an error line ends the run with its code, one of `sessions`
    exiting ends it as `<session>_exited`, and the bound ends it as `code`."""
    deadline = runner.now() + bound_s
    while True:
        _raise_on_error(runner, errors)
        if scan.find(runner) is not None:
            return
        dead = next((s for s in sessions if not runner.alive(s)), None)
        if dead is not None:
            _raise_on_error(runner, errors)
            if scan.find(runner) is not None:  # its last line came just before it exited
                return
            raise EngineRunError(f"{dead}_exited")
        if runner.now() >= deadline:
            raise EngineRunError(code)
        runner.sleep(POLL_S)


def _raise_on_error(runner: Runner, errors: Sequence[LogScan]) -> None:
    for scan in errors:
        line = scan.find(runner)
        if line is not None:
            raise EngineRunError(ERROR_CODES[line])


@contextlib.contextmanager
def _signals_held() -> Iterator[None]:
    """Ignore SIGHUP and SIGTERM while the teardown runs, so a second signal cannot leave the sim
    on the GPU; the handlers come back after. Python sets handlers in the main thread only."""
    if threading.current_thread() is not threading.main_thread():
        yield
        return
    held: dict[int, object] = {}
    for name in HELD_SIGNALS:
        number = getattr(signal, name, None)
        if number is not None:
            held[number] = signal.signal(number, signal.SIG_IGN)
    try:
        yield
    finally:
        for number, handler in held.items():
            if handler is not None:  # None: a handler Python did not install; leave SIG_IGN
                signal.signal(number, handler)  # type: ignore[arg-type]


def _stop_all(runner: Runner) -> None:
    for session in ("policy", "sim"):
        runner.stop(session)
    runner.call([*_DBX, "stop", "-r", "--yes", CONTAINER], timeout_s=CALL_TIMEOUT_S)


def _inspect(*args: str) -> list[str]:
    return ["sudo", "docker", "inspect", "--type", "container", *args, CONTAINER]


def _ensure_container(runner: Runner) -> None:
    if runner.call(_inspect(), timeout_s=CALL_TIMEOUT_S) == 0:
        return
    create = [*_DBX, "create", "-r", "--nvidia", "--yes", "-i", IMAGE, CONTAINER]
    if runner.call(create, timeout_s=CREATE_TIMEOUT_S) != 0:
        raise EngineRunError("container_create_failed")


def _image_id(runner: Runner) -> str:
    """The id of the image the container runs, or "unknown"; never the command's text."""
    out = runner.output(_inspect("--format", "{{.Image}}"), timeout_s=CALL_TIMEOUT_S)
    image = "" if out is None else out.strip()
    return image if _IMAGE_ID.fullmatch(image) else "unknown"


def _write_env(plan: EnginePlan) -> None:
    plan.logs_dir.mkdir(parents=True, exist_ok=True)
    values = {
        "CELLRT_SETTING": plan.setting,
        "CELLRT_MANIFEST": str(plan.manifest),
        "CELLRT_STORE": str(plan.store),
        "CELLRT_SESSION": plan.tag,
    }
    lines = [f"export {k}={shlex.quote(v)}\n" for k, v in values.items()]
    lines.append("unset " + " ".join(n for n in POLICY_ENV if n not in values) + "\n")
    plan.env_file.write_text("".join(lines), encoding="utf-8", newline="\n")


def _sim_command(plan: EnginePlan) -> str:
    q = shlex.quote
    inner = (
        f"export AIC_RESULTS_DIR={q(str(plan.results_dir))}; /entrypoint.sh"
        " gazebo_gui:=false launch_rviz:=false ground_truth:=true start_aic_engine:=true"
        f" aic_engine_config_file:={q(str(plan.config))}"
    )
    return (
        f"DBX_CONTAINER_MANAGER=docker distrobox enter -r {CONTAINER} -- bash -c {q(inner)}"
        f" > {q(str(plan.sim_log))} 2>&1"
    )


def _policy_command(plan: EnginePlan) -> str:
    q = shlex.quote
    return (
        f"cd {q(str(plan.workspace))} && . {q(str(plan.base_env))} && . {q(str(plan.env_file))}"
        ' && export PATH="$HOME/.pixi/bin:$PATH" && pixi run --frozen ros2 run aic_model'
        " aic_model --ros-args -p use_sim_time:=true -p policy:=adapters.aic.RuntimePolicy"
        f" > {q(str(plan.policy_log))} 2>&1"
    )


def _file_sha256(path: Path) -> str:
    """The SHA-256 of a config we wrote, or a fixed word when it cannot be read within bound."""
    try:
        with path.open("rb") as source:
            raw = source.read(MAX_CONFIG_BYTES + 1)
    except OSError:
        return "unreadable"
    if len(raw) > MAX_CONFIG_BYTES:
        return "over_bound"
    return hashlib.sha256(raw).hexdigest()


def _append_ledger(
    plan: EnginePlan, started: float, ended: float, code: str, config_sha: str, image: str
) -> None:
    row = {
        "tag": plan.tag,
        "setting": plan.setting,
        "units": list(plan.units),
        "started_wall": round(started, 3),
        "ended_wall": round(ended, 3),
        "hours": round(max(ended - started, 0.0) / 3600.0, 6),
        "exit": code,
        "commit": plan.commit,
        "image": image,
        "config_sha256": config_sha,
    }
    plan.ledger.parent.mkdir(parents=True, exist_ok=True)
    with plan.ledger.open("a", encoding="utf-8", newline="\n") as out:
        out.write(json.dumps(row, sort_keys=True) + "\n")


BAG_DUMP_TIMEOUT_S = 600.0
STORE_SUFFIXES = ("", "-wal", "-shm")


def dump_bags(
    plan: EnginePlan, runner: Runner, *, script: Path, ros_setup: Path, out_dir: Path
) -> dict[str, str]:
    """Dump each trial's bag (`bag_<trial id>_<time>` under the results dir, aic_engine.cpp:
    1304-1315) to `<out_dir>/<trial id>.json` with `bag_dump.py`, inside the eval container, then
    stop the container. Returns each trial's result: "ok", "no_bag", "two_bags", "unsafe_path"
    or "dump_failed". Raises PlanError for a script, setup or output path `check_path` refuses."""
    for path in (script, ros_setup, out_dir):
        check_path(path)
    out_dir.mkdir(parents=True, exist_ok=True)
    results: dict[str, str] = {}
    try:
        for n in range(1, len(plan.units) + 1):
            trial_id = f"trial_{n}"
            bags = sorted(plan.results_dir.glob(f"bag_{trial_id}_*"))
            if len(bags) != 1:
                results[trial_id] = "no_bag" if not bags else "two_bags"
                continue
            try:
                check_path(bags[0])  # the engine names it; the command still carries it
            except PlanError:
                results[trial_id] = "unsafe_path"
                continue
            q = shlex.quote
            inner = (
                f". {q(str(ros_setup))} && python3 {q(str(script))}"
                f" {q(str(bags[0]))} {q(str(out_dir / f'{trial_id}.json'))}"
            )
            argv = [*_DBX, "enter", "-r", CONTAINER, "--", "bash", "-c", inner]
            ok = runner.call(argv, timeout_s=BAG_DUMP_TIMEOUT_S) == 0
            results[trial_id] = "ok" if ok else "dump_failed"
    finally:
        runner.call([*_DBX, "stop", "-r", "--yes", CONTAINER], timeout_s=CALL_TIMEOUT_S)
    return results


def snapshot_store(store: Path, dest_dir: Path) -> Path:
    """Copy the store's files (the database, its WAL and shared memory) to `dest_dir`, after the
    run's teardown; reports read only the copy (CLAUDE.md: never open the original). Files an
    earlier copy left there go first: SQLite would replay a stale `-wal` into the new copy."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    for suffix in STORE_SUFFIXES:
        (dest_dir / (store.name + suffix)).unlink(missing_ok=True)
    for suffix in STORE_SUFFIXES:
        source = store.with_name(store.name + suffix)
        if source.exists():
            shutil.copy2(source, dest_dir / source.name)
    return dest_dir / store.name


class TmuxRunner:
    """The VM's runner: tmux sessions for the sim and the policy, bounded subprocess calls. A
    session is named exactly (`=name`): tmux would otherwise take any session whose name starts
    with it."""

    def call(self, argv: list[str], *, timeout_s: float) -> int:
        try:
            done = subprocess.run(argv, capture_output=True, timeout=timeout_s, check=False)
        except (OSError, subprocess.TimeoutExpired):
            return -1
        return done.returncode

    def output(self, argv: list[str], *, timeout_s: float) -> str | None:
        """The command's stdout when it succeeds within `timeout_s` and prints at most
        `MAX_OUTPUT_BYTES`; otherwise None. The callers run fixed commands that print one line."""
        try:
            done = subprocess.run(
                argv,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=timeout_s,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if done.returncode != 0 or len(done.stdout) > MAX_OUTPUT_BYTES:
            return None
        return done.stdout.decode("utf-8", errors="replace")

    def start(self, session: str, command: str) -> None:
        argv = ["tmux", "new-session", "-d", "-s", session, command]
        if self.call(argv, timeout_s=CALL_TIMEOUT_S) != 0:
            raise EngineRunError(f"{session}_not_started")

    def alive(self, session: str) -> bool:
        argv = ["tmux", "has-session", "-t", f"={session}"]
        return self.call(argv, timeout_s=CALL_TIMEOUT_S) == 0

    def stop(self, session: str) -> None:
        self.call(["tmux", "kill-session", "-t", f"={session}"], timeout_s=CALL_TIMEOUT_S)

    def read_bytes(self, path: Path, offset: int, size: int) -> bytes:
        try:
            with path.open("rb") as log:
                log.seek(offset)
                return log.read(size)
        except OSError:
            return b""

    def is_mount(self, path: Path) -> bool:
        return os.path.ismount(path)

    def free_bytes(self, path: Path) -> int:
        try:
            return shutil.disk_usage(path).free
        except OSError:
            return 0

    def now(self) -> float:
        return time.monotonic()

    def wall(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


# --- the tracer (#78) --------------------------------------------------------------------------


def exit_on_hangup() -> None:
    """Turn SIGHUP (a dropped SSH session) and SIGTERM into SystemExit, so `run_engine` still
    tears down and writes its ledger row (ADR 028). Only the signals the platform has."""
    for name in HELD_SIGNALS:
        number = getattr(signal, name, None)
        if number is not None:
            signal.signal(number, _raise_exit)


def _raise_exit(signum: int, _frame: object) -> None:
    raise SystemExit(128 + signum)


def arm_guard(runner: Runner, minutes: int) -> bool:
    """Set the session's guard power-off (ADR 028, D8): the VM shuts down after `minutes`
    whatever the session does. False if it could not be set."""
    if not 0 < minutes <= 24 * 60:
        raise PlanError("the guard must be 1 to 1440 minutes")
    argv = ["sudo", "shutdown", "-h", f"+{minutes}"]
    return runner.call(argv, timeout_s=CALL_TIMEOUT_S) == 0


def _commit(runner: Runner, repo: Path) -> str:
    """The checkout's commit, or "unknown"; never the command's output otherwise."""
    out = runner.output(["git", "-C", str(repo), "rev-parse", "HEAD"], timeout_s=CALL_TIMEOUT_S)
    commit = "" if out is None else out.strip()
    return commit if _COMMIT.fullmatch(commit) else "unknown"


def tracer(argv: Sequence[str], runner: Runner) -> int:
    import yaml

    from adapters.aic.manifest import ManifestError, load_manifest
    from tools.sim.scenes import (
        EngineConfigError,
        build_manifest,
        engine_config,
        load_template,
        read_bounded,
        scene_from_trial,
        write_yaml,
    )

    parser = argparse.ArgumentParser(prog="python -m tools.sim.batch tracer")
    parser.add_argument("--sample", type=Path, required=True, help="aic_engine's sample config")
    parser.add_argument("--template", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True, help="a new absolute directory")
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--base-env", type=Path, required=True)
    parser.add_argument("--results-mount", type=Path, default=Path.home() / "results")
    parser.add_argument("--setting", default="A", choices=SETTINGS)
    parser.add_argument("--ros-setup", type=Path, default=Path("/opt/ros/kilted/setup.bash"))
    args = parser.parse_args(argv)

    out: Path = args.out
    if out.exists() and any(out.iterdir()):  # one run per directory: no mixed bags or stores
        print("tracer: --out must be a new or empty directory", file=sys.stderr)
        return 2
    if not runner.is_mount(args.results_mount):  # before anything is written under it
        print("tracer: the results disk is not mounted", file=sys.stderr)
        return 2
    try:
        sample = yaml.safe_load(read_bounded(args.sample))
        scene = scene_from_trial("nominal_sfp", "nominal", sample["trials"]["trial_1"])
        plan = EnginePlan(  # every path checked before anything is written
            tag=f"tracer-{args.setting.lower()}",
            config=out / "engine.yaml",
            manifest=out / "tracer-v1.json",
            store=out / "store.sqlite",
            results_dir=out / "aic",
            logs_dir=out / "logs",
            ledger=out / "ledger.jsonl",
            workspace=args.workspace,
            base_env=args.base_env,
            results_mount=args.results_mount,
            setting=args.setting,
            units=(scene.task_id(0),),
            time_limit_s=float(scene.trial["task"]["time_limit"]),
            commit=_commit(runner, Path(__file__).resolve().parents[2]),
        )
        script = Path(__file__).resolve().parent / "in_container" / "bag_dump.py"
        for path in (script, args.ros_setup):
            check_path(path)
        config = engine_config([(scene, 0)], load_template(args.template))
        manifest = build_manifest("tracer-v1", [scene], repeats=1)
    except (PlanError, EngineConfigError) as ex:  # our own messages
        print(f"tracer: {ex}", file=sys.stderr)
        return 2
    except Exception as ex:  # a file we could not read or parse: its text is not ours to print
        print(f"tracer: the inputs could not be read ({type(ex).__name__})", file=sys.stderr)
        return 2
    if not arm_guard(runner, TRACER_GUARD_MIN):
        print("tracer: the guard power-off could not be set", file=sys.stderr)
        return 2
    out.mkdir(parents=True, exist_ok=True)
    plan.manifest.write_text(
        json.dumps(manifest, sort_keys=True, indent=1), encoding="utf-8", newline="\n"
    )
    try:
        load_manifest(plan.manifest)  # the policy will read it: refuse it here first
    except ManifestError as ex:
        print(f"tracer: {ex}", file=sys.stderr)
        return 2
    write_yaml(plan.config, config)
    result = run_engine(plan, runner)
    print(f"{result.tag}: {result.exit}")
    dumps = dump_bags(plan, runner, script=script, ros_setup=args.ros_setup, out_dir=out / "bags")
    print(f"bags: {dumps}")
    runner.sleep(SNAPSHOT_GRACE_S)  # the policy was killed in the teardown; let it exit
    copy = snapshot_store(plan.store, out / "snapshot")
    print(f"store copy: {copy}")
    return 0 if result.exit == "ok" and set(dumps.values()) == {"ok"} else 1


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args[:1] == ["tracer"]:
        exit_on_hangup()
        return tracer(args[1:], TmuxRunner())
    print("usage: python -m tools.sim.batch tracer --help", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
