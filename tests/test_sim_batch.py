"""One engine run on the VM (`tools/sim/batch.py`, ADR 028), driven through a fake `Runner`:
time moves only when the runner sleeps, and each session's log grows on a script. Every wait
has a bound that a test hits, every engine error line ends the run, and every exit path stops
the policy and the sim and writes its ledger row."""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
import yaml

import tools.sim.batch as batch
from tools.sim.batch import (
    ALL_DONE,
    BAG_DUMP_TIMEOUT_S,
    CALL_TIMEOUT_S,
    CHUNK_BYTES,
    CREATE_TIMEOUT_S,
    ENGINE_ERRORS,
    MAX_CHUNKS_PER_POLL,
    MAX_OUTPUT_BYTES,
    MIN_FREE_BYTES,
    POLICY_ENV,
    POLICY_ERRORS,
    POLICY_READY,
    POLICY_READY_S,
    POLL_S,
    RTF,
    SIM_READY,
    SIM_READY_S,
    TRACER_GUARD_MIN,
    TRIAL_OVERHEAD_S,
    EnginePlan,
    LogScan,
    PlanError,
    TmuxRunner,
    arm_guard,
    dump_bags,
    exit_on_hangup,
    run_engine,
    snapshot_store,
    tracer,
)

REPO = Path(__file__).parent.parent
IMAGE_ID = "sha256:" + "b" * 64


@dataclass
class FakeRunner:
    """Sessions whose logs grow on a script: (seconds after the session started, text)."""

    log_of: dict[str, Path]
    script: dict[str, list[tuple[float, str]]] = field(default_factory=dict)
    exits_after: dict[str, float] = field(default_factory=dict)  # the session ends by itself
    container_exists: bool = True
    create_rc: int = 0
    fail_start: bool = False
    mounted: bool = True
    free: int = MIN_FREE_BYTES
    sleep_raises: type[BaseException] | None = None
    guard_rc: int = 0
    image_out: str | None = IMAGE_ID + "\n"
    commit_out: str | None = "a" * 40 + "\n"
    t: float = 0.0
    calls: list[list[str]] = field(default_factory=list)
    timeouts: list[tuple[list[str], float]] = field(default_factory=list)
    asked: list[list[str]] = field(default_factory=list)
    started: dict[str, float] = field(default_factory=dict)
    commands: dict[str, str] = field(default_factory=dict)
    log_existed: dict[str, bool] = field(default_factory=dict)
    stopped: list[str] = field(default_factory=list)
    down: set[str] = field(default_factory=set)

    def call(self, argv: list[str], *, timeout_s: float) -> int:
        assert timeout_s > 0
        self.calls.append(list(argv))
        self.timeouts.append((list(argv), timeout_s))
        if argv[:3] == ["sudo", "docker", "inspect"]:
            return 0 if self.container_exists else 1
        if argv[:3] == ["sudo", "shutdown", "-h"]:
            return self.guard_rc
        if "create" in argv:
            return self.create_rc
        return 0

    def output(self, argv: list[str], *, timeout_s: float) -> str | None:
        assert timeout_s > 0
        self.asked.append(list(argv))
        if "rev-parse" in argv:
            return self.commit_out
        if "{{.Image}}" in argv:
            return self.image_out
        return None

    def start(self, session: str, command: str) -> None:
        if self.fail_start:
            raise RuntimeError("tmux is not installed")
        self.log_existed[session] = self.log_of[session].exists()
        self.started[session] = self.t
        self.commands[session] = command
        self.down.discard(session)

    def alive(self, session: str) -> bool:
        if session not in self.started or session in self.down:
            return False
        ends = self.exits_after.get(session)
        return ends is None or self.t - self.started[session] < ends

    def stop(self, session: str) -> None:
        self.stopped.append(session)
        self.down.add(session)

    def read_bytes(self, path: Path, offset: int, size: int) -> bytes:
        text = ""
        for session, log in self.log_of.items():
            if log == path and session in self.started:
                age = self.t - self.started[session]
                text = "".join(s for at, s in self.script.get(session, []) if at <= age)
        return text.encode("utf-8")[offset : offset + size]

    def is_mount(self, path: Path) -> bool:
        return self.mounted

    def free_bytes(self, path: Path) -> int:
        return self.free

    def now(self) -> float:
        return self.t

    def wall(self) -> float:
        return 1_000_000.0 + self.t

    def sleep(self, seconds: float) -> None:
        assert seconds > 0
        if self.sleep_raises is not None:
            raise self.sleep_raises()
        self.t += seconds


def plan(tmp_path: Path, n_units: int = 2, **change: object) -> EnginePlan:
    fields: dict[str, object] = {
        "tag": "tracer-a",
        "config": tmp_path / "engine.yaml",
        "manifest": tmp_path / "manifest.json",
        "store": tmp_path / "store.sqlite",
        "results_dir": tmp_path / "aic",
        "logs_dir": tmp_path / "logs",
        "ledger": tmp_path / "ledger.jsonl",
        "workspace": tmp_path / "ws_aic" / "src" / "aic",
        "base_env": tmp_path / "cellrt.env",
        "results_mount": tmp_path,
        "setting": "A",
        "units": tuple(f"nominal_sfp_r{k}" for k in range(n_units)),
        "time_limit_s": 180.0,
    }
    return EnginePlan(**{**fields, **change})  # type: ignore[arg-type]


def runner_for(p: EnginePlan, **kwargs: object) -> FakeRunner:
    return FakeRunner(log_of={"sim": p.sim_log, "policy": p.policy_log}, **kwargs)  # type: ignore[arg-type]


HAPPY = {
    "sim": [(40.0, f"[engine] {SIM_READY}\n"), (700.0, f"... {ALL_DONE} ...\n")],
    "policy": [(10.0, f"[aic_model] {POLICY_READY}: RuntimePolicy\n")],
}


def ledger_rows(p: EnginePlan) -> list[dict[str, object]]:
    return [json.loads(line) for line in p.ledger.read_text("utf-8").splitlines()]


def test_an_engine_run_starts_the_sim_then_the_policy_and_waits_for_every_trial(
    tmp_path: Path,
) -> None:
    p = plan(tmp_path)
    runner = runner_for(p, script=HAPPY)

    result = run_engine(p, runner)

    assert result.exit == "ok"
    assert list(runner.started) == ["sim", "policy"]
    # The commands run in a POSIX shell on the VM: read them as that shell splits them.
    sim = shlex.split(runner.commands["sim"])
    assert sim[-3:] == [">", str(p.sim_log), "2>&1"]
    inner = shlex.split(sim[sim.index("-c") + 1])
    assert f"AIC_RESULTS_DIR={p.results_dir};" in inner
    assert f"aic_engine_config_file:={p.config}" in inner
    assert {"ground_truth:=true", "start_aic_engine:=true", "gazebo_gui:=false"} <= set(inner)
    policy = shlex.split(runner.commands["policy"])
    assert str(p.env_file) in policy and "policy:=adapters.aic.RuntimePolicy" in policy
    assert policy[-3:] == [">", str(p.policy_log), "2>&1"]


def test_the_policy_reads_its_setting_manifest_and_store_from_the_env_file(
    tmp_path: Path,
) -> None:
    p = plan(tmp_path)
    run_engine(p, runner_for(p, script=HAPPY))

    lines = [shlex.split(line) for line in p.env_file.read_text("utf-8").splitlines()]
    assert ["export", "CELLRT_SETTING=A"] in lines
    assert ["export", f"CELLRT_MANIFEST={p.manifest}"] in lines
    assert ["export", f"CELLRT_STORE={p.store}"] in lines
    assert not any("NVIDIA" in word for line in lines for word in line)  # never a key in a file


def test_the_env_file_unsets_every_policy_variable_the_plan_does_not_set(tmp_path: Path) -> None:
    p = plan(tmp_path)
    run_engine(p, runner_for(p, script=HAPPY))

    lines = [shlex.split(line) for line in p.env_file.read_text("utf-8").splitlines()]
    exported = {line[1].split("=", 1)[0] for line in lines if line[0] == "export"}
    (unset,) = [line[1:] for line in lines if line[0] == "unset"]
    assert {"CELLRT_FAULT", "CELLRT_PLANNER"} <= set(unset)
    assert exported | set(unset) == set(POLICY_ENV)
    assert not exported & set(unset)


def test_the_env_list_names_every_variable_the_policy_reads() -> None:
    read = set()
    for source in (REPO / "adapters" / "aic").glob("*.py"):
        read |= set(re.findall(r"\"(CELLRT_[A-Z_]+)\"", source.read_text("utf-8")))

    assert read == set(POLICY_ENV)


def test_every_run_writes_one_ledger_row_with_its_hours(tmp_path: Path) -> None:
    p = plan(tmp_path)
    runner = runner_for(p, script=HAPPY)
    run_engine(p, runner)
    run_engine(p, runner_for(p))  # a second run that fails: a second row

    first, second = ledger_rows(p)
    assert first["tag"] == "tracer-a" and first["setting"] == "A"
    assert first["units"] == ["nominal_sfp_r0", "nominal_sfp_r1"]
    assert first["exit"] == "ok"
    assert first["hours"] == pytest.approx(runner.t / 3600.0, abs=1e-4)
    assert second["exit"] == "sim_not_ready"


def test_the_ledger_row_names_the_commit_and_the_engine_configs_hash(tmp_path: Path) -> None:
    p = plan(tmp_path, commit="a" * 40)
    p.config.write_bytes(b"trials: {}\n")
    run_engine(p, runner_for(p, script=HAPPY))
    p.config.unlink()
    run_engine(p, runner_for(p, script=HAPPY))

    first, second = ledger_rows(p)
    assert first["commit"] == "a" * 40
    assert first["config_sha256"] == hashlib.sha256(b"trials: {}\n").hexdigest()
    assert second["config_sha256"] == "unreadable"


@pytest.mark.parametrize(
    ("out", "recorded"),
    [
        (IMAGE_ID + "\n", IMAGE_ID),
        ("Error: No such object: aic_eval\n", "unknown"),  # never the command's text
        (None, "unknown"),
    ],
)
def test_the_ledger_row_names_the_image_the_container_runs(
    tmp_path: Path, out: str | None, recorded: str
) -> None:
    p = plan(tmp_path)
    runner = runner_for(p, script=HAPPY, image_out=out)

    run_engine(p, runner)

    assert ledger_rows(p)[-1]["image"] == recorded
    (asked,) = runner.asked
    assert asked[:5] == ["sudo", "docker", "inspect", "--type", "container"]


def test_a_refused_run_records_no_image(tmp_path: Path) -> None:
    p = plan(tmp_path)
    run_engine(p, runner_for(p, script=HAPPY, free=0))

    assert ledger_rows(p)[-1]["image"] == "unknown"


def _assert_torn_down(runner: FakeRunner) -> None:
    assert {"policy", "sim"} <= set(runner.stopped[-2:])
    assert runner.calls[-1][-4:] == ["stop", "-r", "--yes", "aic_eval"]


def test_a_sim_that_never_gets_ready_ends_the_run_at_its_bound(tmp_path: Path) -> None:
    p = plan(tmp_path)
    runner = runner_for(p)

    assert run_engine(p, runner).exit == "sim_not_ready"

    assert SIM_READY_S <= runner.t < SIM_READY_S + POLL_S + 1e-9
    assert "policy" not in runner.started
    _assert_torn_down(runner)


def test_a_policy_that_never_gets_ready_ends_the_run_at_its_bound(tmp_path: Path) -> None:
    p = plan(tmp_path)
    runner = runner_for(p, script={"sim": HAPPY["sim"]})

    assert run_engine(p, runner).exit == "policy_not_ready"

    waited = runner.t - runner.started["policy"]
    assert POLICY_READY_S <= waited < POLICY_READY_S + POLL_S + 1e-9
    _assert_torn_down(runner)


def test_trials_that_never_finish_end_the_run_at_their_bound(tmp_path: Path) -> None:
    p = plan(tmp_path, n_units=3)
    bound = 3 * (180.0 / RTF + TRIAL_OVERHEAD_S)
    assert p.trials_bound_s() == pytest.approx(bound)
    script = {"sim": [(40.0, SIM_READY)], "policy": HAPPY["policy"]}
    runner = runner_for(p, script=script)

    assert run_engine(p, runner).exit == "trials_not_processed"

    waited = runner.t - runner.started["policy"] - 10.0  # the policy got ready after 10 s
    assert bound - POLL_S <= waited < bound + POLL_S + 1e-9
    _assert_torn_down(runner)


@pytest.mark.parametrize(("line", "code"), sorted(ENGINE_ERRORS.items()))
@pytest.mark.parametrize("at", [30.0, 300.0])  # before the sim is ready, and mid-run
def test_an_engine_error_line_ends_the_run_at_once(
    tmp_path: Path, line: str, code: str, at: float
) -> None:
    p = plan(tmp_path)
    sim = [(40.0, SIM_READY), (at, f"[aic_engine] {line} 'trial_1'\n")]
    runner = runner_for(p, script={"sim": sim, "policy": HAPPY["policy"]})

    assert run_engine(p, runner).exit == code

    assert runner.t < at + POLL_S + 1e-9
    _assert_torn_down(runner)


@pytest.mark.parametrize(("session", "code"), [("sim", "sim_exited"), ("policy", "policy_exited")])
def test_a_session_that_exits_before_it_is_ready_ends_the_run_at_once(
    tmp_path: Path, session: str, code: str
) -> None:
    p = plan(tmp_path)
    script = {"sim": HAPPY["sim"]} if session == "policy" else {}
    runner = runner_for(p, script=script, exits_after={session: 12.0})

    assert run_engine(p, runner).exit == code

    assert runner.t - runner.started[session] < 12.0 + POLL_S + 1e-9
    _assert_torn_down(runner)


@pytest.mark.parametrize("session", ["sim", "policy"])
def test_a_session_that_exits_mid_run_ends_the_run(tmp_path: Path, session: str) -> None:
    p = plan(tmp_path)
    script = {"sim": [(40.0, SIM_READY)], "policy": HAPPY["policy"]}
    runner = runner_for(p, script=script, exits_after={session: 300.0})

    assert run_engine(p, runner).exit == f"{session}_exited"
    assert runner.t - runner.started[session] < 300.0 + POLL_S + 1e-9
    _assert_torn_down(runner)


@pytest.mark.parametrize(("line", "code"), sorted(POLICY_ERRORS.items()))
def test_a_goal_the_policy_refuses_ends_the_run_with_its_code(
    tmp_path: Path, line: str, code: str
) -> None:
    # RuntimePolicy logs "cell-runtime goal refused: <fixed message>" (ADR 027); the engine
    # would go on to the next trial, so the runner stops the run itself (ADR 028).
    p = plan(tmp_path)
    policy = [*HAPPY["policy"], (60.0, f"[ERROR] [aic_model]: {line}\n")]
    runner = runner_for(p, script={"sim": [(40.0, SIM_READY)], "policy": policy})

    assert run_engine(p, runner).exit == code

    assert runner.t - runner.started["policy"] < 60.0 + POLL_S + 1e-9
    assert ledger_rows(p)[-1]["exit"] == code
    _assert_torn_down(runner)


def test_a_goal_that_merely_fails_does_not_end_the_run(tmp_path: Path) -> None:
    p = plan(tmp_path)
    policy = [*HAPPY["policy"], (60.0, "[ERROR] cell-runtime goal failed: TimeoutError\n")]
    runner = runner_for(p, script={"sim": HAPPY["sim"], "policy": policy})

    assert run_engine(p, runner).exit == "ok"  # a failed goal is a run's outcome, not ours


def test_the_teardown_ignores_a_second_signal_and_restores_the_handlers(tmp_path: Path) -> None:
    seen: list[object] = []

    class _Watching(FakeRunner):
        def stop(self, session: str) -> None:
            seen.append(signal.getsignal(signal.SIGTERM))
            super().stop(session)

    p = plan(tmp_path)
    before = signal.getsignal(signal.SIGTERM)
    run_engine(p, _Watching(log_of={"sim": p.sim_log, "policy": p.policy_log}, script=HAPPY))

    assert seen[:2] == [before, before]  # the clean-up before the run: signals as they were
    assert seen[-2:] == [signal.SIG_IGN, signal.SIG_IGN]  # the teardown
    assert signal.getsignal(signal.SIGTERM) == before


def test_a_last_line_written_as_the_session_exits_still_counts(tmp_path: Path) -> None:
    class _LastWords(FakeRunner):
        def alive(self, session: str) -> bool:
            up = super().alive(session)
            if not up and session == "sim":  # the engine flushed its last line, then exited
                self.script["sim"].append((0.0, ALL_DONE))
            return up

    p = plan(tmp_path)
    script = {"sim": [(40.0, SIM_READY)], "policy": HAPPY["policy"]}
    runner = _LastWords(
        log_of={"sim": p.sim_log, "policy": p.policy_log},
        script=script,
        exits_after={"sim": 300.0},
    )

    assert run_engine(p, runner).exit == "ok"


def test_logs_an_earlier_run_left_under_the_tag_are_removed_before_the_sim_starts(
    tmp_path: Path,
) -> None:
    p = plan(tmp_path)
    p.logs_dir.mkdir()
    p.sim_log.write_text(f"{SIM_READY}\n{ALL_DONE}\n", "utf-8")
    p.policy_log.write_text(f"{POLICY_READY}\n", "utf-8")
    runner = runner_for(p, script=HAPPY)

    run_engine(p, runner)

    assert runner.log_existed == {"sim": False, "policy": False}


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ({"mounted": False}, "results_not_mounted"),
        ({"free": MIN_FREE_BYTES - 1}, "results_disk_full"),
    ],
)
def test_a_run_is_refused_unless_the_results_disk_is_mounted_with_room(
    tmp_path: Path, change: dict[str, object], code: str
) -> None:
    p = plan(tmp_path)
    runner = runner_for(p, script=HAPPY, **change)

    assert run_engine(p, runner).exit == code

    assert runner.started == {}
    if code == "results_not_mounted":  # the ledger is on that disk: no row under the mount
        assert not p.ledger.exists()
    else:
        assert ledger_rows(p)[-1]["exit"] == code
    _assert_torn_down(runner)


def test_a_results_directory_from_an_earlier_run_is_refused(tmp_path: Path) -> None:
    p = plan(tmp_path)
    p.results_dir.mkdir()
    runner = runner_for(p, script=HAPPY)

    assert run_engine(p, runner).exit == "results_dir_exists"
    assert runner.started == {}


def test_a_missing_container_is_created_and_a_failed_creation_ends_the_run(
    tmp_path: Path,
) -> None:
    p = plan(tmp_path)
    created = runner_for(p, script=HAPPY, container_exists=False)
    assert run_engine(p, created).exit == "ok"
    assert any("create" in c for c in created.calls)

    failed = runner_for(p, script=HAPPY, container_exists=False, create_rc=1)
    assert run_engine(p, failed).exit == "container_create_failed"
    assert failed.started == {}
    _assert_torn_down(failed)


def test_a_runner_that_raises_still_tears_down_and_writes_its_row(tmp_path: Path) -> None:
    p = plan(tmp_path)
    runner = runner_for(p, fail_start=True)

    with pytest.raises(RuntimeError):
        run_engine(p, runner)

    _assert_torn_down(runner)
    assert ledger_rows(p)[-1]["exit"] == "runner_error"


@pytest.mark.parametrize("interrupt", [SystemExit, KeyboardInterrupt])
def test_an_interrupt_still_tears_down_and_writes_its_row(
    tmp_path: Path, interrupt: type[BaseException]
) -> None:
    p = plan(tmp_path)
    runner = runner_for(p, sleep_raises=interrupt)

    with pytest.raises(interrupt):
        run_engine(p, runner)

    _assert_torn_down(runner)
    assert ledger_rows(p)[-1]["exit"] == "interrupted"


def test_a_hangup_or_a_terminate_becomes_an_ordinary_exit(monkeypatch: pytest.MonkeyPatch) -> None:
    installed: dict[int, object] = {}
    monkeypatch.setattr(
        signal, "signal", lambda number, handler: installed.update({number: handler})
    )

    exit_on_hangup()

    handler = installed[signal.SIGTERM]
    with pytest.raises(SystemExit) as ended:
        handler(signal.SIGTERM, None)  # type: ignore[operator]
    assert ended.value.code == 128 + signal.SIGTERM
    if hasattr(signal, "SIGHUP"):  # POSIX only; the VM has it
        assert signal.SIGHUP in installed


@pytest.mark.parametrize(
    "change",
    [
        {"tag": "../up"},
        {"tag": ""},
        {"setting": "D"},
        {"units": ()},
        {"units": ("Bad-Id",)},
        {"time_limit_s": 0.0},
        {"config": Path("relative.yaml")},
        {"commit": "abc"},
    ],
)
def test_a_plan_that_cannot_run_is_refused(tmp_path: Path, change: dict[str, object]) -> None:
    with pytest.raises(PlanError):
        plan(tmp_path, **change)


@pytest.mark.parametrize("name", ["store", "results_dir", "logs_dir", "ledger"])
def test_a_path_that_climbs_out_of_the_results_disk_is_refused(tmp_path: Path, name: str) -> None:
    # A literal prefix check would pass <mount>/../x; a `..` part is refused outright.
    with pytest.raises(PlanError, match=r"'\.\.'"):
        plan(tmp_path, **{name: tmp_path / ".." / "elsewhere"})


@pytest.mark.parametrize("name", ["with space", "semi;colon", "quote'd", "dollar$x", "new\nline"])
def test_a_path_with_a_shell_character_is_refused(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError, match="shell character"):
        plan(tmp_path, results_dir=tmp_path / name)


def test_a_plan_that_writes_outside_the_results_disk_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="results disk"):
        plan(tmp_path, results_mount=tmp_path / "disk")


def test_the_tracer_refuses_a_directory_an_earlier_run_wrote_to(tmp_path: Path) -> None:
    out = tmp_path / "out"
    out.mkdir()
    (out / "ledger.jsonl").write_text("{}\n", "utf-8")
    common = ["--sample", str(tmp_path / "s.yaml"), "--template", str(tmp_path / "t.yaml")]
    common += ["--workspace", str(tmp_path), "--base-env", str(tmp_path / "e")]
    common += ["--results-mount", str(tmp_path)]

    assert tracer([*common, "--out", str(out)], FakeRunner(log_of={})) == 2

    fresh = tmp_path / "fresh"
    unmounted = FakeRunner(log_of={}, mounted=False)
    assert tracer([*common, "--out", str(fresh)], unmounted) == 2
    assert not fresh.exists()  # an unmounted disk: nothing is written under it
    assert unmounted.calls == []  # not even the guard


def _tracer_args(tmp_path: Path, out: Path, **change: Path) -> list[str]:
    paths = {
        "--sample": REPO / "tests" / "fixtures" / "sim" / "sample_config.yaml",
        "--template": REPO / "eval" / "sim" / "engine_template.yaml",
        "--out": out,
        "--workspace": tmp_path / "ws",
        "--base-env": tmp_path / "cellrt.env",
        "--results-mount": tmp_path / "disk",
        "--ros-setup": tmp_path / "setup.bash",  # the default is a POSIX path
    }
    paths |= change
    return [word for flag, path in paths.items() for word in (flag, str(path))]


def test_the_tracer_sets_the_guard_power_off_before_it_runs_the_sample_trial(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "disk" / "tracer"
    logs = out / "logs"
    one_trial = {"sim": [(40.0, SIM_READY), (200.0, ALL_DONE)], "policy": HAPPY["policy"]}
    runner = FakeRunner(
        log_of={"sim": logs / "tracer-a.sim.log", "policy": logs / "tracer-a.policy.log"},
        script=one_trial,
    )

    assert tracer(_tracer_args(tmp_path, out), runner) == 1  # the fake engine left no bag

    assert runner.calls[0] == ["sudo", "shutdown", "-h", f"+{TRACER_GUARD_MIN}"]
    assert TRACER_GUARD_MIN == 60  # ADR 028's table
    assert "tracer-a: ok" in capsys.readouterr().out
    (row,) = [json.loads(line) for line in (out / "ledger.jsonl").read_text("utf-8").splitlines()]
    assert (row["exit"], row["commit"], row["image"]) == ("ok", "a" * 40, IMAGE_ID)
    config = yaml.safe_load((out / "engine.yaml").read_text("utf-8"))
    assert list(config["trials"]["trial_1"]["tasks"]) == ["nominal_sfp_r0"]
    manifest = json.loads((out / "tracer-v1.json").read_text("utf-8"))
    assert list(manifest["units"]) == ["nominal_sfp_r0"]


def test_without_its_guard_the_tracer_writes_nothing_and_starts_nothing(tmp_path: Path) -> None:
    out = tmp_path / "disk" / "tracer"
    runner = FakeRunner(log_of={}, guard_rc=1)

    assert tracer(_tracer_args(tmp_path, out), runner) == 2

    assert not out.exists() and runner.started == {}


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"--sample": Path("missing.yaml")}, "could not be read"),
        ({"--workspace": Path("relative")}, "absolute"),
        ({"--ros-setup": Path("/opt/with space/setup.bash").absolute()}, "shell character"),
    ],
)
def test_a_tracer_input_it_cannot_use_is_refused_before_the_guard(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], change: dict[str, Path], message: str
) -> None:
    out = tmp_path / "disk" / "tracer"
    runner = FakeRunner(log_of={})

    assert tracer(_tracer_args(tmp_path, out, **change), runner) == 2

    assert message in capsys.readouterr().err
    assert runner.calls == [] and not out.exists()


@pytest.mark.parametrize("minutes", [0, 24 * 60 + 1])
def test_a_guard_out_of_its_range_is_refused(minutes: int) -> None:
    runner = FakeRunner(log_of={})
    with pytest.raises(PlanError):
        arm_guard(runner, minutes)
    assert runner.calls == []


@pytest.mark.parametrize(
    ("out", "commit"),
    [("a" * 40 + "\n", "a" * 40), ("fatal: not a git repository\n", "unknown"), (None, "unknown")],
)
def test_the_commit_is_read_or_unknown_never_the_commands_text(
    out: str | None, commit: str
) -> None:
    assert batch.checkout_commit(FakeRunner(log_of={}, commit_out=out), REPO) == commit


# --- after the run: the bags and the store ------------------------------------------------------


def test_each_trials_bag_is_dumped_inside_the_container_and_the_container_stopped(
    tmp_path: Path,
) -> None:
    p = plan(tmp_path, n_units=3)
    (p.results_dir / "bag_trial_1_2026_11_12-10_00_00").mkdir(parents=True)
    (p.results_dir / "bag_trial_10_2026_11_12-10_00_00").mkdir()  # not trial_1's
    (p.results_dir / "bag_trial_3_a").mkdir()
    (p.results_dir / "bag_trial_3_b").mkdir()
    runner = runner_for(p)
    script, setup = tmp_path / "bag_dump.py", tmp_path / "setup.bash"

    results = dump_bags(p, runner, script=script, ros_setup=setup, out_dir=tmp_path / "bags")

    assert results == {"trial_1": "ok", "trial_2": "no_bag", "trial_3": "two_bags"}
    (dump, stop) = runner.calls
    assert dump[:7] == [
        *("env", "DBX_CONTAINER_MANAGER=docker", "distrobox", "enter", "-r", "aic_eval", "--")
    ]
    inner = shlex.split(dump[-1])
    assert inner[:2] == [".", str(setup)]
    assert inner[-3:] == [
        str(script),
        str(p.results_dir / "bag_trial_1_2026_11_12-10_00_00"),
        str(tmp_path / "bags" / "trial_1.json"),
    ]
    assert stop[-4:] == ["stop", "-r", "--yes", "aic_eval"]


def test_a_failed_dump_is_reported_and_the_container_still_stopped(tmp_path: Path) -> None:
    p = plan(tmp_path, n_units=1)
    (p.results_dir / "bag_trial_1_x").mkdir(parents=True)

    class _Failing(FakeRunner):
        def call(self, argv: list[str], *, timeout_s: float) -> int:
            super().call(argv, timeout_s=timeout_s)
            return 1 if "enter" in argv else 0

    runner = _Failing(log_of={})
    results = dump_bags(
        p, runner, script=tmp_path / "s.py", ros_setup=tmp_path / "r", out_dir=tmp_path / "b"
    )

    assert results == {"trial_1": "dump_failed"}
    assert runner.calls[-1][-4:] == ["stop", "-r", "--yes", "aic_eval"]


def test_the_dumps_container_stop_ignores_a_second_signal(tmp_path: Path) -> None:
    seen: list[object] = []

    class _Watching(FakeRunner):
        def call(self, argv: list[str], *, timeout_s: float) -> int:
            if "stop" in argv:
                seen.append(signal.getsignal(signal.SIGTERM))
            return super().call(argv, timeout_s=timeout_s)

    p = plan(tmp_path, n_units=1)
    before = signal.getsignal(signal.SIGTERM)
    dump_bags(
        p,
        _Watching(log_of={}),
        script=tmp_path / "s.py",
        ros_setup=tmp_path / "r",
        out_dir=tmp_path / "b",
    )

    assert seen == [signal.SIG_IGN]
    assert signal.getsignal(signal.SIGTERM) == before


def test_dump_paths_the_command_cannot_carry_are_refused(tmp_path: Path) -> None:
    p = plan(tmp_path, n_units=2)
    (p.results_dir / "bag_trial_1_a;b").mkdir(parents=True)
    runner = runner_for(p)
    ok = {"script": tmp_path / "s.py", "ros_setup": tmp_path / "r", "out_dir": tmp_path / "b"}

    for name in ok:
        with pytest.raises(PlanError):
            dump_bags(p, runner, **{**ok, name: tmp_path / "with space"})
    assert runner.calls == []

    assert dump_bags(p, runner, **ok) == {"trial_1": "unsafe_path", "trial_2": "no_bag"}


def test_the_long_calls_get_their_own_bounds_and_every_other_call_the_short_one(
    tmp_path: Path,
) -> None:
    p = plan(tmp_path, n_units=1)
    runner = runner_for(p, script=HAPPY, container_exists=False)
    run_engine(p, runner)
    (p.results_dir / "bag_trial_1_x").mkdir(parents=True)
    dump_bags(p, runner, script=tmp_path / "s.py", ros_setup=tmp_path / "r", out_dir=tmp_path / "b")

    bounds = {
        ("create" if "create" in a else "enter" if "enter" in a else ""): t
        for a, t in runner.timeouts
    }
    assert bounds["create"] == CREATE_TIMEOUT_S
    assert bounds["enter"] == BAG_DUMP_TIMEOUT_S
    others = {t for a, t in runner.timeouts if "create" not in a and "enter" not in a}
    assert others == {CALL_TIMEOUT_S}


def test_the_store_snapshot_copies_the_database_and_its_wal_files(tmp_path: Path) -> None:
    store = tmp_path / "live" / "store.sqlite"
    store.parent.mkdir()
    store.write_bytes(b"db")
    store.with_name("store.sqlite-wal").write_bytes(b"wal")  # no -shm: skipped

    copy = snapshot_store(store, tmp_path / "snap")

    assert copy == tmp_path / "snap" / "store.sqlite"
    assert copy.read_bytes() == b"db"
    assert (tmp_path / "snap" / "store.sqlite-wal").read_bytes() == b"wal"
    assert not (tmp_path / "snap" / "store.sqlite-shm").exists()


def test_a_snapshot_leaves_no_wal_of_an_earlier_copy_beside_the_new_one(tmp_path: Path) -> None:
    store = tmp_path / "live" / "store.sqlite"
    store.parent.mkdir()
    store.write_bytes(b"db2")  # checkpointed: no -wal this time
    snap = tmp_path / "snap"
    snap.mkdir()
    (snap / "store.sqlite").write_bytes(b"db1")
    (snap / "store.sqlite-wal").write_bytes(b"stale")
    (snap / "store.sqlite-shm").write_bytes(b"stale")

    copy = snapshot_store(store, snap)

    assert copy.read_bytes() == b"db2"
    assert sorted(f.name for f in snap.iterdir()) == ["store.sqlite"]


# --- the VM's runner ----------------------------------------------------------------------------


@dataclass
class _Run:
    """subprocess.run, scripted: what it returns or raises, and what it was asked."""

    returncode: int = 0
    stdout: bytes = b""
    raises: BaseException | None = None
    seen: list[tuple[list[str], float]] = field(default_factory=list)

    def __call__(self, argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        timeout = kwargs["timeout"]
        assert isinstance(timeout, float)
        self.seen.append((list(argv), timeout))
        if self.raises is not None:
            raise self.raises
        return subprocess.CompletedProcess(argv, self.returncode, stdout=self.stdout, stderr=b"")


def test_the_vm_runner_names_its_sessions_exactly_and_bounds_each_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run = _Run()
    monkeypatch.setattr(batch.subprocess, "run", run)
    runner = TmuxRunner()

    runner.start("sim", "true")
    assert runner.alive("sim")
    runner.stop("policy")

    assert run.seen == [
        (["tmux", "new-session", "-d", "-s", "sim", "true"], CALL_TIMEOUT_S),
        (["tmux", "has-session", "-t", "=sim"], CALL_TIMEOUT_S),  # not a prefix: "sim2"
        (["tmux", "kill-session", "-t", "=policy"], CALL_TIMEOUT_S),
    ]


@pytest.mark.parametrize(
    "error", [subprocess.TimeoutExpired(["tmux"], CALL_TIMEOUT_S), OSError("no tmux")]
)
def test_a_call_that_times_out_or_cannot_start_fails_without_raising(
    monkeypatch: pytest.MonkeyPatch, error: BaseException
) -> None:
    monkeypatch.setattr(batch.subprocess, "run", _Run(raises=error))
    runner = TmuxRunner()

    assert runner.call(["tmux"], timeout_s=CALL_TIMEOUT_S) == -1
    assert runner.output(["git"], timeout_s=CALL_TIMEOUT_S) is None
    assert runner.alive("sim") is False
    with pytest.raises(batch.EngineRunError, match="sim_not_started"):
        runner.start("sim", "true")


@pytest.mark.parametrize(
    ("returncode", "stdout", "expected"),
    [
        (0, b"abc\n", "abc\n"),
        (1, b"abc\n", None),
        (0, b"x" * MAX_OUTPUT_BYTES, "x" * MAX_OUTPUT_BYTES),
        (0, b"x" * (MAX_OUTPUT_BYTES + 1), None),  # the bound: refused, not cut
    ],
)
def test_a_commands_output_is_read_only_within_its_bound(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stdout: bytes, expected: str | None
) -> None:
    monkeypatch.setattr(batch.subprocess, "run", _Run(returncode=returncode, stdout=stdout))

    assert TmuxRunner().output(["git"], timeout_s=CALL_TIMEOUT_S) == expected


# --- reading a log a chunk at a time ------------------------------------------------------------


@dataclass
class _Bytes:
    data: bytes
    reads: int = 0

    def read_bytes(self, path: Path, offset: int, size: int) -> bytes:
        self.reads += 1
        return self.data[offset : offset + size]


def test_a_pattern_across_a_chunk_boundary_is_found(tmp_path: Path) -> None:
    data = b"x" * (CHUNK_BYTES - 5) + ALL_DONE.encode("utf-8") + b"\n"
    scan = LogScan(tmp_path / "log", (ALL_DONE,))

    assert scan.find(_Bytes(data)) == ALL_DONE


def test_a_scan_reads_only_what_was_appended_since_the_last_poll(tmp_path: Path) -> None:
    source = _Bytes(b"a" * 10)
    scan = LogScan(tmp_path / "log", (SIM_READY,))
    assert scan.find(source) is None
    source.data += b"b" * 10 + SIM_READY.encode("utf-8")

    assert scan.find(source) == SIM_READY
    assert scan.offset == len(source.data)


def test_a_scan_names_which_of_its_patterns_it_found(tmp_path: Path) -> None:
    scan = LogScan(tmp_path / "log", tuple(ENGINE_ERRORS))

    assert scan.find(_Bytes(b"... Engine Stopped with Errors\n")) == "Engine Stopped with Errors"


def test_one_poll_reads_at_most_its_bound_of_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(batch, "CHUNK_BYTES", 8)
    source = _Bytes(b"x" * 8 * MAX_CHUNKS_PER_POLL + ALL_DONE.encode("utf-8"))
    scan = LogScan(tmp_path / "log", (ALL_DONE,))

    assert scan.find(source) is None  # the bound, not the end of the log
    assert source.reads == MAX_CHUNKS_PER_POLL
    assert scan.offset == 8 * MAX_CHUNKS_PER_POLL
    assert scan.find(source) == ALL_DONE  # the next poll goes on from there
