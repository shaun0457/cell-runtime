"""A Phase 2 session (`tools/sim/queue.py`, #81; ADR 026, 028, 030), run on a fake VM: each
engine run writes what the real one leaves on the results disk (the ledger row, the store's
events, `scoring.yaml` when the engine finished, the sim log's trial lines), and copying its
outputs writes the bag dumps and the store copy. Each test scripts the units' outcomes and the
engine runs' exits it needs."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
import yaml

import tools.sim.queue as queue
from adapters.aic.manifest import load_manifest
from cellrt.runtime.events import (
    SCHEMA_VERSION,
    Event,
    MonitorVerdict,
    RunFinished,
    RunStarted,
    SkillStarted,
    WorldReady,
)
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task
from tools.sim import batch
from tools.sim.batch import EnginePlan, EngineResult, snapshot_store
from tools.sim.queue import (
    Record,
    Session,
    SessionError,
    SplitQueue,
    attempts,
    check_families,
    last_named_trial,
    load_queue,
    main_session,
    next_tag,
    read_session,
    run_session,
)
from tools.sim.report import EngineRun
from tools.sim.scenes import (
    build_manifest,
    load_template,
    scene_from_trial,
    scenes_of,
    write_yaml,
)

FIXTURES = Path(__file__).parent / "fixtures" / "sim"
TEMPLATE = Path(__file__).parent.parent / "eval" / "sim" / "engine_template.yaml"
SAMPLE = yaml.safe_load((FIXTURES / "sample_config.yaml").read_text("utf-8"))
COMMIT = "a" * 40
ORIGIN = 100.0
TARGET = "nic_card_mount_0/sfp_port_0"

Outcome = str  # "inserted", "failure", "false_alarm" or "unfinished"


# --- a session ---------------------------------------------------------------------------------


def _session(
    tmp_path: Path,
    *,
    name: str = "pilot-v1",
    families: dict[str, str],
    repeats: dict[str, int],
    split: SplitQueue,
    selected: frozenset[str] | None = None,
    commit: str = COMMIT,
) -> Session:
    scenes = [scene_from_trial(s, f, SAMPLE["trials"]["trial_1"]) for s, f in families.items()]
    manifest_path = tmp_path / f"{name}.json"
    if not manifest_path.exists():
        data = build_manifest(name, scenes, repeats=repeats)
        manifest_path.write_text(json.dumps(data), "utf-8")
    manifest = load_manifest(manifest_path)
    results = tmp_path / "results"
    return Session(
        name="s1",
        root=results / "s1",
        results_mount=results,
        manifest_path=manifest_path,
        manifest=manifest,
        scenes=scenes_of(manifest.model_dump()),
        queue=split,
        families=selected,
        template=load_template(TEMPLATE),
        workspace=tmp_path / "ws",
        base_env=tmp_path / "env.sh",
        commit=commit,
    )


def _pilot(tmp_path: Path, scenes: int = 3, **kw: Any) -> Session:
    """`scenes` scenes run twice with A, and a `reversed` one run last."""
    ids = [f"p{i}" for i in range(scenes)]
    families = {s: "inrange_sfp" for s in ids} | {"rev": "reversed"}
    split = SplitQueue(
        a=tuple([f"{s}_r0" for s in ids] + [f"{s}_r1" for s in ids]),
        order=(*ids, "rev"),
        last=("rev_r0",),
    )
    return _session(
        tmp_path, families=families, repeats={**dict.fromkeys(ids, 2), "rev": 1}, split=split, **kw
    )


def _tune(tmp_path: Path, a: list[str], b: list[str], per_run: int, **kw: Any) -> Session:
    families = {"t0": "tilt_sfp", "t1": "tilt_sfp", "t2": "beyond_sfp"}
    split = SplitQueue(a=tuple(a), b=tuple(b), per_run=per_run)
    return _session(
        tmp_path, name="tune-v1", families=families, repeats=dict.fromkeys(families, 2),
        split=split, **kw,
    )  # fmt: skip


# --- a fake VM ---------------------------------------------------------------------------------


@dataclass
class Machine:
    """The runner calls a session makes itself: the mount, the guard, the commit."""

    mounted: bool = True
    guard_rc: int = 0
    commit_out: str | None = COMMIT + "\n"
    status_out: str | None = ""  # `git status --porcelain` of the policy's code: clean
    container: bool = True  # the eval container is there (`docker inspect` succeeds)
    wall_at: float = 0.0  # the wall clock the loop's start notes read
    running: frozenset[str] = frozenset()  # tmux sessions still up
    calls: list[list[str]] = field(default_factory=list)

    def call(self, argv: list[str], *, timeout_s: float) -> int:
        assert timeout_s > 0
        self.calls.append(list(argv))
        if argv[:3] == ["sudo", "shutdown", "-h"]:
            return self.guard_rc
        return 1 if "inspect" in argv and not self.container else 0

    def output(self, argv: list[str], *, timeout_s: float) -> str | None:
        assert timeout_s > 0
        if "status" in argv:
            assert argv[-4:] == list(queue.CHECKOUT_PATHS)
            return self.status_out
        return self.commit_out

    def is_mount(self, path: Path) -> bool:
        return self.mounted

    def start(self, session: str, command: str) -> None:
        raise AssertionError("a session starts no process itself")

    def alive(self, session: str) -> bool:
        return session in self.running

    def stop(self, session: str) -> None:
        raise AssertionError("unused")

    def read_bytes(self, path: Path, offset: int, size: int) -> bytes:
        raise AssertionError("unused")

    def free_bytes(self, path: Path) -> int:
        raise AssertionError("unused")

    def now(self) -> float:
        return 0.0

    def wall(self) -> float:
        return self.wall_at

    def sleep(self, seconds: float) -> None:
        raise AssertionError("unused")


class HardKill(Exception):
    """The runner's process gone before `run_engine` wrote its row (SIGKILL, a VM stop)."""


@dataclass
class FakeVm:
    """Each engine run as the VM leaves it. `outcome(task_id, setting)` scripts a reached unit;
    `exits` scripts the engine runs in turn, (exit, the trial its log named last); a run past
    the script ends "ok". `interrupt_at` raises KeyboardInterrupt after that run's row, as
    `run_engine` does; `kill_at` raises HardKill before it, with the store and the log written."""

    outcome: Callable[[str, str], Outcome] = lambda task_id, setting: "inserted"
    exits: list[tuple[str, int | None]] = field(default_factory=list)
    interrupt_at: int | None = None
    kill_at: int | None = None
    copy_hours: float = 0.0  # each copy's, as VmExecutor records it
    plans: list[EnginePlan] = field(default_factory=list)
    copied: list[str] = field(default_factory=list)
    said: list[str] = field(default_factory=list)  # what the session said, in order
    clock: float = 1000.0

    def run(self, plan: EnginePlan) -> EngineResult:
        self.plans.append(plan)
        n_run = len(self.plans)
        code, named = self.exits[n_run - 1] if n_run <= len(self.exits) else ("ok", None)
        started = self.clock
        manifest = load_manifest(plan.manifest)
        reached = len(plan.units) if code == "ok" else (named or 0)
        plan.logs_dir.mkdir(parents=True, exist_ok=True)
        plan.results_dir.mkdir(parents=True, exist_ok=True)
        lines, scores = [], {"total": 0.0}
        with EventStore(plan.store) as store:
            for n, task_id in enumerate(plan.units[:reached], start=1):
                lines.append(f"\x1b[1;33m  Trial {n}/{len(plan.units)}: trial_{n}\x1b[0m\n")
                outcome = self.outcome(task_id, plan.setting)
                if code != "ok" and n == reached:
                    outcome = "unfinished"  # the trial the engine stopped in
                place = manifest.lookup(task_id)
                attempt = store.next_attempt(place.scenario_id, plan.setting, place.seed)
                for event in _events(place.scenario_id, task_id, place.seed, plan.setting,
                                     attempt, outcome, started + 10.0 * n):  # fmt: skip
                    store.append(event)
                scores[f"trial_{n}"] = _score(outcome)
                (plan.results_dir / f"bag_trial_{n}_0.json").write_text(
                    json.dumps(_dump(outcome)), "utf-8"
                )
        plan.sim_log.write_text("".join(lines), "utf-8")
        if self.kill_at == n_run:
            raise HardKill
        if code == "ok":
            (plan.results_dir / "scoring.yaml").write_text(yaml.safe_dump(scores), "utf-8")
        ended = started + 1000.0
        self.clock = ended + 10.0
        if code != "results_not_mounted":  # run_engine writes every other row
            sha = hashlib.sha256(plan.config.read_bytes()).hexdigest()
            batch.append_ledger(plan, started, ended, code, sha, "sha256:" + "b" * 64)
        if self.interrupt_at == n_run:
            raise KeyboardInterrupt
        return EngineResult(tag=plan.tag, exit=code, started_wall=started, ended_wall=ended)

    def copy_outputs(self, plan: EnginePlan) -> None:
        self.copied.append(plan.tag)
        run_dir = plan.config.parent
        (run_dir / "bags").mkdir(exist_ok=True)
        for bag in plan.results_dir.glob("bag_trial_*_0.json"):
            trial_id = bag.name.removeprefix("bag_").removesuffix("_0.json")
            (run_dir / "bags" / f"{trial_id}.json").write_bytes(bag.read_bytes())
        snapshot_store(plan.store, run_dir / "snapshot")
        queue.write_outputs(run_dir, {}, self.copy_hours)


def _events(
    scenario: str, task_id: str, seed: int, setting: str, attempt: int, outcome: str, wall: float
) -> list[Event]:
    start = RunStarted(
        scenario_id=scenario,
        setting=setting,
        seed=seed,
        attempt=attempt,
        task=Task(id=task_id, time_limit=180.0),
        config_hash="h",
        schema_version=SCHEMA_VERSION,
    )
    payloads: list[tuple[float, Any]] = [
        (0.0, start),
        (0.0, WorldReady(sim_origin_s=ORIGIN, port=None, plug=None, gripper=None)),
    ]
    if outcome in ("failure", "false_alarm"):
        payloads.append((19.0, SkillStarted(skill="insert", attempt=1)))
        payloads.append(
            (20.0, MonitorVerdict(skill="insert", failure_class="INSERT_STUCK", rule="r"))
        )
    if outcome != "unfinished":
        success = outcome == "inserted"
        payloads.append(
            (
                25.0,
                RunFinished(
                    outcome="success" if success else "failed", reason="r", steps=1, duration=25.0
                ),
            )
        )
    run_id = f"{scenario}/{setting}/{seed}/a{attempt}"
    return [
        Event(run_id=run_id, seq=n, ts=ts, wall_ts=wall + ts, payload=p)
        for n, (ts, p) in enumerate(payloads, start=1)
    ]


def _inserted(outcome: str) -> bool:
    return outcome in ("inserted", "false_alarm")


def _score(outcome: str) -> dict[str, Any]:
    categories = {"contacts": {"score": 0}, "insertion force": {"score": 0}}
    return {
        "tier_1": {"score": 1},
        "tier_2": {"score": 10.0, "categories": categories},
        "tier_3": {"score": 75 if _inserted(outcome) else 0},
    }


def _dump(outcome: str) -> dict[str, Any]:
    insertions: list[dict[str, Any]] = []
    pairs: list[list[int]] = []
    if _inserted(outcome):
        recv = int((ORIGIN + 5.0) * 1e9)
        insertions.append({"recv_ns": recv, "port": f"/{TARGET}"})
        pairs = [[recv - 20_000_000, recv - 20_000_000], [recv + 20_000_000, recv + 20_000_000]]
    return {"version": 1, "insertions": insertions, "contacts": [], "pairs": pairs}


def _runs(vm: FakeVm) -> list[tuple[str, tuple[str, ...]]]:
    return [(p.setting, p.units) for p in vm.plans]


def _go(session: Session, vm: FakeVm, machine: Machine | None = None, **kw: Any) -> int:
    return run_session(session, machine or Machine(), vm, say=vm.said.append, **kw)


# --- the order ---------------------------------------------------------------------------------


def test_the_pilot_runs_a_then_b_on_the_qualifying_scenes_then_last(tmp_path: Path) -> None:
    failing = {("p0_r0", "A"), ("p0_r1", "A"), ("p2_r1", "A")}  # p0 twice, p2 once, p1 never
    vm = FakeVm(outcome=lambda u, s: "failure" if (u, s) in failing else "inserted")
    session = _pilot(tmp_path)

    assert _go(session, vm) == 0

    assert _runs(vm) == [
        ("A", ("p0_r0", "p1_r0", "p2_r0", "p0_r1", "p1_r1", "p2_r1")),
        ("B", ("p0_r0", "p2_r0")),  # both A trials of p0 qualify, one of p2
        ("A", ("rev_r0",)),
    ]
    assert vm.said[-1] == "session s1: the queue is empty"
    assert [p.tag for p in vm.plans] == ["s1-001", "s1-002", "s1-003"]


def test_the_b_pass_ranks_by_qualifying_a_trials_then_order_and_stops_at_its_cap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    failing = {("p0_r0", "A"), ("p1_r0", "A"), ("p1_r1", "A"), ("p2_r0", "A")}
    vm = FakeVm(outcome=lambda u, s: "failure" if (u, s) in failing else "inserted")
    monkeypatch.setattr(queue, "MAX_PILOT_B", 2)

    assert _go(_pilot(tmp_path), vm) == 0

    assert _runs(vm)[1] == ("B", ("p1_r0", "p0_r0"))  # p1 (two) first, then p0 by order; p2 cut


def test_a_scene_whose_a_verdicts_were_false_alarms_gets_no_b_run(tmp_path: Path) -> None:
    vm = FakeVm(outcome=lambda u, s: "false_alarm" if u.startswith("p0") else "inserted")

    assert _go(_pilot(tmp_path), vm) == 0

    assert [s for s, _ in _runs(vm)] == ["A", "A"]  # no B run at all, then `last`


def test_tuning_alternates_a_and_b_and_skips_unselected_families(tmp_path: Path) -> None:
    units = ["t0_r0", "t2_r0", "t1_r0", "t0_r1", "t2_r1", "t1_r1"]
    session = _tune(tmp_path, units, list(reversed(units)), 2, selected=frozenset({"tilt_sfp"}))
    vm = FakeVm()

    assert _go(session, vm) == 0

    assert _runs(vm) == [
        ("A", ("t0_r0", "t1_r0")),
        ("B", ("t1_r1", "t0_r1")),
        ("A", ("t0_r1", "t1_r1")),
        ("B", ("t1_r0", "t0_r0")),
    ]


def test_when_one_setting_has_no_units_left_the_others_runs_follow(tmp_path: Path) -> None:
    units = ["t0_r0", "t1_r0", "t2_r0", "t0_r1", "t1_r1", "t2_r1"]
    session = _tune(tmp_path, units[:2], units, 2, selected=frozenset({"tilt_sfp", "beyond_sfp"}))
    vm = FakeVm()

    assert _go(session, vm) == 0

    assert [s for s, _ in _runs(vm)] == ["A", "B", "B", "B"]


# --- attempts ----------------------------------------------------------------------------------


def test_an_ok_run_charges_every_unit_and_a_unit_out_of_attempts_is_left(tmp_path: Path) -> None:
    vm = FakeVm(outcome=lambda u, s: "unfinished" if u == "p0_r0" else "inserted")
    session = _pilot(tmp_path, scenes=1)

    assert _go(session, vm) == 0

    held = [units for _, units in _runs(vm) if "p0_r0" in units]
    assert len(held) == queue.MAX_ATTEMPTS
    assert attempts(read_session(session))[("p0_r0", "A")] == queue.MAX_ATTEMPTS


def test_a_run_that_ended_badly_charges_only_the_trial_its_log_named_last(tmp_path: Path) -> None:
    session = _pilot(tmp_path, scenes=3)
    vm = FakeVm(exits=[("trials_not_processed", 2)])

    assert _go(session, vm) == 0

    first, second = _runs(vm)[:2]
    assert second == first  # trial 1 lost its scoring.yaml and reruns with the rest
    charged = attempts(read_session(session)[:1])
    assert charged == {("p1_r0", "A"): 1}


def test_two_runs_in_a_row_that_fail_before_any_trial_stop_the_session(tmp_path: Path) -> None:
    vm = FakeVm(exits=[("sim_not_ready", None), ("sim_not_ready", None)])

    assert _go(_pilot(tmp_path), vm) == 1

    assert len(vm.plans) == 2
    assert vm.said[-1] == "session: 2 engine runs in a row failed before any trial"


def test_one_run_failing_before_any_trial_is_rerun(tmp_path: Path) -> None:
    vm = FakeVm(exits=[("sim_not_ready", None), ("ok", None), ("sim_not_ready", None)])

    assert _go(_pilot(tmp_path), vm) == 0

    assert len(vm.plans) == 4  # the count of failures in a row went back to 0 after the ok run


@pytest.mark.parametrize("code", ["task_mismatch", "policy_setup_failed", "engine_parse_failed"])
def test_a_code_no_rerun_can_fix_stops_the_session_at_once(tmp_path: Path, code: str) -> None:
    vm = FakeVm(exits=[(code, 1)])

    assert _go(_pilot(tmp_path), vm) == 1

    assert len(vm.plans) == 1 and vm.copied == ["s1-001"]  # its outputs are still copied
    assert vm.said[-1] == f"session: stopped at {code}; no rerun can fix it"


@pytest.mark.parametrize("code", sorted(queue.DISK_GUARDS))
def test_a_disk_guard_stops_the_session_without_copying(tmp_path: Path, code: str) -> None:
    vm = FakeVm(exits=[(code, None)])

    assert _go(_pilot(tmp_path), vm) == 1

    assert vm.copied == []


# --- cost and guards ---------------------------------------------------------------------------


def test_the_session_stops_before_a_run_that_could_pass_its_ceiling(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(queue.CEILINGS, "pilot", (0.5, 60))  # one 6-trial run's worst case: $1
    vm = FakeVm()

    assert _go(_pilot(tmp_path), vm) == 1

    assert vm.plans == []
    assert vm.said[-1] == ("session: the session's ceiling would be passed: the queue stops here")


def test_the_session_stops_before_a_run_that_could_pass_the_phases_ceiling(tmp_path: Path) -> None:
    tracer = tmp_path / "results" / "tracer1"
    tracer.mkdir(parents=True)
    row = {"tag": "tracer-a", "units": ["x"], "config_sha256": "c", "hours": 27.0}
    (tracer / "ledger.jsonl").write_text(json.dumps(row) + "\nnot ours\n", "utf-8")
    vm = FakeVm()

    assert _go(_pilot(tmp_path), vm) == 1

    assert vm.said[-1] == ("session: the phase's ceiling would be passed: the queue stops here")


def test_a_ledger_row_without_valid_hours_stops_the_phase_sum(tmp_path: Path) -> None:
    tracer = tmp_path / "results" / "tracer1"
    tracer.mkdir(parents=True)
    row = {"tag": "tracer-a", "units": ["x"], "config_sha256": "c", "hours": "1"}
    (tracer / "ledger.jsonl").write_text(json.dumps(row) + "\n", "utf-8")

    with pytest.raises(SessionError, match="no valid hours"):
        queue.phase_hours(tmp_path / "results")


def test_a_ledger_past_its_bound_stops_the_phase_sum_rather_than_undercount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracer = tmp_path / "results" / "tracer1"
    tracer.mkdir(parents=True)
    row = json.dumps({"tag": "tracer-a", "units": ["x"], "config_sha256": "c", "hours": 1.0})
    (tracer / "ledger.jsonl").write_text(row + "\n", "utf-8")
    monkeypatch.setattr(queue, "MAX_LEDGER_BYTES", len(row))

    with pytest.raises(SessionError, match="a ledger on the results disk: over its bound"):
        queue.phase_hours(tmp_path / "results")


def test_the_guard_power_off_is_set_first_and_a_session_without_it_is_refused(
    tmp_path: Path,
) -> None:
    vm = FakeVm()

    assert _go(_pilot(tmp_path), vm, Machine(guard_rc=1)) == 2

    assert vm.plans == []


def test_the_guard_is_the_splits(tmp_path: Path) -> None:
    machine = Machine()

    assert _go(_pilot(tmp_path), FakeVm(), machine, power_off_when_done=True) == 0

    assert machine.calls[0] == ["sudo", "shutdown", "-h", "+298"]
    assert machine.calls[-1] == ["sudo", "shutdown", "-h", "now"]


def test_a_resumed_session_gets_only_what_is_left_of_its_guard(tmp_path: Path) -> None:
    session = _pilot(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        _go(session, FakeVm(interrupt_at=1))  # 1000 s of engine run: 17 minutes
    machine = Machine()

    assert _go(session, FakeVm(clock=5000.0), machine) == 0

    assert machine.calls[0] == ["sudo", "shutdown", "-h", "+281"]  # 298 - ceil(16.7)


def test_a_session_whose_guard_time_is_spent_sets_one_minute_and_stops(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _pilot(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        _go(session, FakeVm(interrupt_at=1))
    monkeypatch.setitem(queue.CEILINGS, "pilot", (6.0, 17))
    machine, vm = Machine(), FakeVm(clock=5000.0)

    assert _go(session, vm, machine) == 1

    assert machine.calls == [["sudo", "shutdown", "-h", "+1"]] and vm.plans == []
    assert vm.said[-1] == "session: the session's guard time is spent: the queue stops here"


def test_the_copies_hours_count_against_the_sessions_ceiling(tmp_path: Path) -> None:
    session = _pilot(tmp_path)
    vm = FakeVm(copy_hours=6.0)  # US$5.65 spent: any next run's worst case passes US$6

    assert _go(session, vm) == 1

    assert len(vm.plans) == 1
    assert vm.said[-1] == "session: the session's ceiling would be passed: the queue stops here"
    (record,) = read_session(session)
    assert record.hours == pytest.approx(1000.0 / 3600.0 + 6.0)


def test_a_run_pays_for_the_containers_creation_only_when_it_is_not_there(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _pilot(tmp_path)
    plan = session.plan("s1-001", "A", ("p0_r0",))
    assert queue.worst_case_h(plan) - queue.worst_case_h(plan, create=False) == pytest.approx(
        batch.CREATE_TIMEOUT_S / 3600.0
    )
    monkeypatch.setattr(queue, "CREATE_TIMEOUT_S", 1e6)  # a creation no ceiling allows
    missing, there = FakeVm(), FakeVm()

    assert _go(session, missing, Machine(container=False)) == 1
    assert missing.plans == []
    assert (
        missing.said[-1] == "session: the session's ceiling would be passed: the queue stops here"
    )
    assert _go(session, there, Machine(container=True)) == 0
    assert there.plans


def test_the_phases_hours_hold_every_runs_row_and_copy(tmp_path: Path) -> None:
    session = _pilot(tmp_path)
    assert _go(session, FakeVm(copy_hours=0.5)) == 0
    runs = len(read_session(session))

    assert queue.phase_hours(tmp_path / "results") == pytest.approx(runs * (1000.0 / 3600 + 0.5))


def test_a_line_in_a_sessions_ledger_that_is_no_row_stops_the_phase_sum(tmp_path: Path) -> None:
    session = _pilot(tmp_path)
    assert _go(session, FakeVm()) == 0
    ledger = session.runs_dir / "s1-001" / "ledger.jsonl"
    ledger.write_text(ledger.read_text("utf-8") + '{"tag": "s1-0\n', "utf-8")  # a cut row

    with pytest.raises(SessionError, match="holds a line that is no row"):
        queue.phase_hours(tmp_path / "results")


def test_the_phases_files_are_bounded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = _pilot(tmp_path)
    assert _go(session, FakeVm()) == 0
    monkeypatch.setattr(queue, "MAX_LEDGERS", 1)  # one run: its ledger and its outputs.json

    with pytest.raises(SessionError, match="too many ledgers"):
        queue.phase_hours(tmp_path / "results")


def test_two_runs_in_a_row_that_finish_no_unit_stop_the_session(tmp_path: Path) -> None:
    vm = FakeVm(exits=[("trials_not_processed", 5)] * 3)  # every run cut in its fifth trial

    assert _go(_pilot(tmp_path), vm) == 1

    assert len(vm.plans) == 2
    assert vm.said[-1] == "session: 2 engine runs in a row finished no unit"


def test_a_unit_that_crashes_every_run_gets_its_third_attempt_in_the_next_invocation(
    tmp_path: Path,
) -> None:
    session = _pilot(tmp_path)
    first = FakeVm(exits=[("trials_not_processed", 1)] * 2)  # each run stops in p0_r0
    assert _go(session, first) == 1
    assert first.said[-1] == "session: 2 engine runs in a row finished no unit"
    second = FakeVm(exits=[("trials_not_processed", 1)], clock=first.clock)

    assert _go(session, second) == 0

    assert second.plans[0].units[0] == "p0_r0"  # its third attempt
    assert all("p0_r0" not in plan.units for plan in second.plans[1:])  # then out of attempts


def test_an_unmounted_results_disk_is_refused(tmp_path: Path) -> None:
    vm = FakeVm()

    assert _go(_pilot(tmp_path), vm, Machine(mounted=False)) == 2

    assert vm.plans == [] and not (tmp_path / "results").exists()


def test_one_invocation_runs_at_most_its_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(queue, "MAX_ENGINE_RUNS", 1)
    vm = FakeVm()

    assert _go(_pilot(tmp_path), vm) == 1

    assert len(vm.plans) == 1
    assert vm.said[-1] == "session: over 1 engine runs in one invocation"


# --- resuming ----------------------------------------------------------------------------------


def test_a_session_cut_short_resumes_and_copies_what_was_not_copied(tmp_path: Path) -> None:
    session = _pilot(tmp_path)
    cut = FakeVm(interrupt_at=1)
    with pytest.raises(KeyboardInterrupt):
        _go(session, cut)

    vm = FakeVm(clock=cut.clock)
    assert _go(session, vm) == 0

    assert vm.copied == ["s1-001", "s1-002"]  # 001 copied on resume, before the next run
    assert [p.tag for p in vm.plans] == ["s1-002"]
    assert _runs(vm) == [("A", ("rev_r0",))]  # A counted once copied; B has nothing; `last`


def test_a_run_killed_before_its_row_is_recorded_on_resume_and_charges_its_named_trial(
    tmp_path: Path,
) -> None:
    session = _pilot(tmp_path)
    with pytest.raises(HardKill):  # trial 1 finished, trial 2 was running
        _go(session, FakeVm(exits=[("x", 2)], kill_at=1), Machine(wall_at=999.0))
    run = session.runs_dir / "s1-001"
    assert not (run / "ledger.jsonl").exists()
    written = [*session.root.glob("store.sqlite*"), run / "logs" / "s1-001.sim.log"]
    for path in written:  # the last write: after its events (1010-1035), before the resume
        os.utime(path, (1040.0, 1040.0))
    vm = FakeVm(clock=2000.0)

    assert _go(session, vm, Machine(wall_at=1990.0)) == 0

    row = json.loads((run / "ledger.jsonl").read_text("utf-8"))
    assert (row["exit"], row["started_wall"], row["ended_wall"]) == ("killed", 999.0, 1040.0)
    assert row["hours"] == round(41.0 / 3600.0, 6)
    assert vm.said[0] == "s1-001: no ledger row; recorded as killed"
    assert vm.copied[:2] == ["s1-001", "s1-002"]  # its bags and store first, then the next run
    a_units = session.queue.a
    assert _runs(vm) == [("A", a_units), ("A", ("rev_r0",))]  # no scoring.yaml: all of A rerun
    charged = attempts(read_session(session))
    assert charged[("p1_r0", "A")] == 2 and charged[("p0_r0", "A")] == 1  # trial 2 was charged


def test_a_killed_runs_end_is_the_last_write_to_the_store_or_its_logs(tmp_path: Path) -> None:
    session = _pilot(tmp_path)
    nothing = session.plan("s1-001", "A", ["p0_r0"])
    nothing.config.parent.mkdir(parents=True)
    write_yaml(nothing.config, session.render(nothing))
    queue.note_start(nothing, 500.0004)  # rounded down to the ms

    assert queue.recover_killed(session, 1000.0) == ["s1-001"]

    row = json.loads(nothing.ledger.read_text("utf-8"))
    assert (row["ended_wall"], row["hours"], row["units"]) == (500.0, 0.0, ["p0_r0"])

    later = session.plan("s1-002", "B", ["p1_r0"])
    later.logs_dir.mkdir(parents=True)
    write_yaml(later.config, session.render(later))
    queue.note_start(later, 600.0)
    wal = session.root / "store.sqlite-wal"
    for path, mtime in [(session.store, 650.0), (wal, 720.0002), (later.sim_log, 640.0),
                        (later.policy_log, 700.0)]:  # fmt: skip
        path.write_bytes(b"")
        os.utime(path, (mtime, mtime))
    later.ledger.write_bytes(b"")  # cut before its first byte: still no row

    assert queue.recover_killed(session, 1000.0) == ["s1-002"]  # s1-001 has its row now

    row = json.loads(later.ledger.read_text("utf-8"))
    assert (row["setting"], row["ended_wall"]) == ("B", 720.001)  # rounded up to the ms


def test_a_killed_runs_window_must_lie_after_the_run_before_it_and_before_now(
    tmp_path: Path,
) -> None:
    session = _pilot(tmp_path)
    ok = session.plan("s1-001", "A", ["p0_r0"])
    ok.config.parent.mkdir(parents=True)
    write_yaml(ok.config, session.render(ok))
    sha = hashlib.sha256(ok.config.read_bytes()).hexdigest()
    batch.append_ledger(ok, 100.0, 200.0, "ok", sha, "unknown")
    killed = session.plan("s1-002", "A", ["p1_r0"])
    killed.config.parent.mkdir(parents=True)
    write_yaml(killed.config, session.render(killed))

    queue.note_start(killed, 150.0)  # inside the run before it
    with pytest.raises(SessionError, match="does not fit between its neighbours' times"):
        queue.recover_killed(session, 1000.0)
    queue.note_start(killed, 300.0)
    with pytest.raises(SessionError, match="does not fit"):  # now is before it: the clock went back
        queue.recover_killed(session, 250.0)

    assert not killed.ledger.exists()
    assert queue.recover_killed(session, 1000.0) == ["s1-002"]


def test_a_run_directory_with_neither_a_row_nor_a_note_stops_the_session(tmp_path: Path) -> None:
    session = _pilot(tmp_path)
    (session.runs_dir / "s1-001").mkdir(parents=True)
    vm = FakeVm()

    assert _go(session, vm) == 1

    assert vm.plans == []
    assert vm.said[-1] == (
        "session: an engine run has no ledger row and never started its engine: move it out of "
        "runs/"
    )


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        ({"commit": "c" * 40}, "an engine run of the session names another commit"),
        ({"tag": "s1-009"}, "not one this runner wrote"),
        ({"units": ["nope_r0"]}, "not one this runner wrote"),
        ({"units": []}, "not one this runner wrote"),
        ({"units": ["p0_r0", "p0_r0"]}, "not one this runner wrote"),
        ({"units": ["p0_r0"]}, "does not match its engine config"),
        ({"setting": "Z"}, "not one this runner wrote"),
        ({"started_wall": True}, "not one this runner wrote"),
        ({"started_wall": 10**400}, "not one this runner wrote"),
        ({"started_wall": -1.0}, "not one this runner wrote"),
        (None, "not one this runner wrote"),
    ],
    ids=["commit", "tag", "unit", "no units", "unit twice", "other units", "setting", "bool",
         "huge", "negative", "not json"],
)  # fmt: skip
def test_a_note_it_did_not_write_is_refused_and_no_row_is_written(
    tmp_path: Path, edit: dict[str, Any] | None, message: str
) -> None:
    session = _pilot(tmp_path)
    with pytest.raises(HardKill):
        _go(session, FakeVm(exits=[("x", 1)], kill_at=1))
    note = session.runs_dir / "s1-001" / queue.STARTED
    if edit is None:
        note.write_text("{not json", "utf-8")
    else:
        note.write_text(json.dumps(json.loads(note.read_text("utf-8")) | edit), "utf-8")
    vm = FakeVm(clock=5000.0)

    assert _go(session, vm) == 1

    assert vm.plans == [] and message in vm.said[-1]
    assert not (session.runs_dir / "s1-001" / "ledger.jsonl").exists()


def test_a_note_past_its_bound_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session = _pilot(tmp_path)
    with pytest.raises(HardKill):
        _go(session, FakeVm(exits=[("x", 1)], kill_at=1))
    monkeypatch.setattr(queue, "MAX_NOTE_BYTES", 8)

    with pytest.raises(SessionError, match="not one this runner wrote"):
        queue.recover_killed(session, 1e10)


def test_a_session_whose_lock_is_held_is_refused_and_the_lock_is_released_after_a_run(
    tmp_path: Path,
) -> None:
    session = _pilot(tmp_path)
    session.root.mkdir(parents=True)
    (session.root / queue.LOCK).write_text("", "utf-8")
    vm = FakeVm()

    assert _go(session, vm) == 2

    assert vm.plans == [] and "another invocation holds session.lock" in vm.said[-1]
    (session.root / queue.LOCK).unlink()
    with pytest.raises(HardKill):  # an exception still releases it; a SIGKILL would not
        _go(session, FakeVm(kill_at=1))
    assert not (session.root / queue.LOCK).exists()


@pytest.mark.parametrize("left_up", ["sim", "policy"])
def test_a_sim_or_policy_still_running_refuses_the_session_before_any_recovery(
    tmp_path: Path, left_up: str
) -> None:
    session = _pilot(tmp_path)
    with pytest.raises(HardKill):
        _go(session, FakeVm(exits=[("x", 1)], kill_at=1))
    vm = FakeVm(clock=5000.0)

    assert _go(session, vm, Machine(wall_at=1e10, running=frozenset({left_up}))) == 2

    assert vm.plans == [] and "still running" in vm.said[-1]
    assert not (session.runs_dir / "s1-001" / "ledger.jsonl").exists()


def test_a_run_of_another_commit_stops_the_session(tmp_path: Path) -> None:
    first = _pilot(tmp_path)
    assert _go(first, FakeVm(exits=[("task_mismatch", 1)])) == 1
    vm = FakeVm(clock=5000.0)

    assert _go(_pilot(tmp_path, commit="c" * 40), vm) == 1

    assert vm.plans == []
    assert vm.said[-1] == "session: an engine run of the session names another commit"


def test_the_next_tag_follows_the_highest_one(tmp_path: Path) -> None:
    session = _pilot(tmp_path)

    def record(tag: str) -> Record:
        run = EngineRun(tmp_path, tag, "A", 0.0, 1.0, 0.0, "ok", COMMIT, (), None)
        return Record(run, None)

    assert next_tag(session, []) == "s1-001"
    assert next_tag(session, [record("s1-001"), record("s1-003")]) == "s1-004"
    with pytest.raises(SessionError, match="not one of this session's"):
        next_tag(session, [record("other-001")])
    assert next_tag(session, [record("s1-998")]) == "s1-999"
    with pytest.raises(SessionError, match="999 engine runs"):
        next_tag(session, [record("s1-999")])


def test_a_session_with_too_many_run_directories_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    session = _pilot(tmp_path)
    for n in range(3):
        (session.runs_dir / f"s1-{n:03d}").mkdir(parents=True)
    monkeypatch.setattr(queue, "MAX_ENGINE_RUNS", 1)  # a session holds at most twice that

    with pytest.raises(SessionError, match="too many engine runs"):
        read_session(session)
    with pytest.raises(SessionError, match="too many engine runs"):
        queue.recover_killed(session, 1e10)


# --- the sim log -------------------------------------------------------------------------------


def test_the_last_named_trial_is_found_across_chunks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "sim.log"
    log.write_bytes(b"x" * 10 + b"Trial 1/6: trial_1\n" + b"y" * 7 + b"Trial 2/6: trial_2\n")
    monkeypatch.setattr(queue, "LOG_CHUNK_BYTES", 8)  # every line split across reads

    assert last_named_trial(log) == "trial_2"
    assert last_named_trial(tmp_path / "missing.log") is None


def test_a_log_past_its_bound_names_no_trial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    log = tmp_path / "sim.log"
    log.write_bytes(b"Trial 1/6: trial_1\n" + b"z" * 100)
    monkeypatch.setattr(queue, "MAX_LOG_SCAN_BYTES", 64)

    assert last_named_trial(log) is None


# --- the inputs --------------------------------------------------------------------------------


def _inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    scenes = [scene_from_trial("p0", "inrange_sfp", SAMPLE["trials"]["trial_1"])]
    sim = tmp_path / "sim"
    sim.mkdir()
    manifest = sim / "pilot-v1.json"
    manifest.write_text(json.dumps(build_manifest("pilot-v1", scenes, repeats=1)), "utf-8")
    q = sim / "queue.json"
    entry = {"order": ["p0"], "A": ["p0_r0"], "last": []}
    q.write_text(json.dumps({"version": 1, "max_trials_per_run": 6, "splits": {"pilot-v1": entry}}))
    template = sim / "engine_template.yaml"
    template.write_bytes(TEMPLATE.read_bytes())
    sums = "".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}\n" for p in (manifest, q, template)
    )
    (sim / "SHA256SUMS").write_text(sums, "utf-8")
    return manifest, q, template


def _argv(tmp_path: Path, manifest: Path, q: Path, template: Path, *extra: str) -> list[str]:
    return [
        "--session", "s1", "--manifest", str(manifest), "--queue", str(q),
        "--template", str(template), "--workspace", str(tmp_path / "ws"),
        "--base-env", str(tmp_path / "env.sh"), "--results-mount", str(tmp_path / "results"),
        *extra,
    ]  # fmt: skip


def test_main_runs_a_session_from_the_pre_registered_files(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest, q, template = _inputs(tmp_path)
    vm = FakeVm()

    assert main_session(_argv(tmp_path, manifest, q, template), Machine(), vm, tmp_path) == 0

    assert _runs(vm) == [("A", ("p0_r0",))] and vm.plans[0].commit == COMMIT
    assert "s1-001 (A, 1 units): ok" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda m, q, t: m.write_text(m.read_text("utf-8") + " ", "utf-8"), "SHA256SUMS names"),
        (lambda m, q, t: (m.parent / "SHA256SUMS").write_text("x\n", "utf-8"), "not a sums file"),
        (lambda m, q, t: None, "--families: the pilot runs every family"),
    ],
    ids=["edited manifest", "bad sums", "families on the pilot"],
)
def test_main_refuses_inputs_it_will_not_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], edit: Any, message: str
) -> None:
    manifest, q, template = _inputs(tmp_path)
    edit(manifest, q, template)
    extra = ("--families", "inrange_sfp") if "families" in message else ()
    vm = FakeVm()

    assert (
        main_session(_argv(tmp_path, manifest, q, template, *extra), Machine(), vm, tmp_path) == 2
    )

    assert vm.plans == [] and message in capsys.readouterr().err


@pytest.mark.parametrize(
    ("bound", "message"),
    [
        ("MAX_SUMS_BYTES", "SHA256SUMS: over its bound"),
        ("MAX_INPUT_BYTES", "an input named in SHA256SUMS: over its bound"),
    ],
)
def test_main_refuses_a_sums_file_or_input_past_its_bound(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
    bound: str,
    message: str,
) -> None:
    manifest, q, template = _inputs(tmp_path)
    monkeypatch.setattr(queue, bound, 64)
    vm = FakeVm()

    assert main_session(_argv(tmp_path, manifest, q, template), Machine(), vm, tmp_path) == 2

    assert vm.plans == [] and message in capsys.readouterr().err


@pytest.mark.parametrize(
    "status",
    [
        " M cellrt/eval/referee.py\n",
        "?? adapters/aic/x.py\n",
        " M tools/sim/queue.py\n",
        " M uv.lock\n",
        None,
    ],
)
def test_main_refuses_a_checkout_whose_policy_code_is_not_its_commits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], status: str | None
) -> None:
    manifest, q, template = _inputs(tmp_path)
    vm = FakeVm()

    code = main_session(
        _argv(tmp_path, manifest, q, template), Machine(status_out=status), vm, tmp_path
    )

    assert code == 2 and vm.plans == []
    assert "cellrt/, adapters/, tools/ or uv.lock is not its commit's" in capsys.readouterr().err


def test_a_queue_naming_a_unit_the_manifest_lacks_is_refused(tmp_path: Path) -> None:
    session = _pilot(tmp_path)
    check_queue = queue.check_queue

    check_queue(session.manifest, session.queue)
    for bad in (
        dataclasses.replace(session.queue, a=("p0_r9",)),
        dataclasses.replace(session.queue, order=("nope",)),
    ):
        with pytest.raises(SessionError, match="a unit the manifest does not hold"):
            check_queue(session.manifest, bad)


def test_a_queue_past_its_bound_or_not_a_regular_file_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "queue.json"
    path.write_text(json.dumps({"version": 1, "max_trials_per_run": 6, "splits": {}}), "utf-8")
    monkeypatch.setattr(queue, "MAX_QUEUE_BYTES", 8)

    with pytest.raises(SessionError, match="queue: over its bound"):
        load_queue(path, "p")
    with pytest.raises(SessionError, match="queue: not a regular file"):
        load_queue(tmp_path, "p")


def test_main_refuses_a_checkout_whose_commit_it_cannot_read(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    manifest, q, template = _inputs(tmp_path)

    code = main_session(
        _argv(tmp_path, manifest, q, template), Machine(commit_out=None), FakeVm(), tmp_path
    )

    assert code == 2
    assert "commit could not be read" in capsys.readouterr().err


def test_tuning_and_held_out_need_the_checkpoints_families(tmp_path: Path) -> None:
    session = _tune(tmp_path, ["t0_r0"], ["t0_r0"], 1)

    with pytest.raises(SessionError, match="name the families"):
        check_families(session.manifest, session.queue, None)
    with pytest.raises(SessionError, match="does not hold"):
        check_families(session.manifest, session.queue, ["nominal_sc"])
    check_families(session.manifest, session.queue, ["tilt_sfp"])


@pytest.mark.parametrize(
    ("data", "message"),
    [
        ({"version": 1, "max_trials_per_run": 7, "splits": {"p": {"A": []}}}, "1 to 6"),
        ({"version": 1, "max_trials_per_run": 6, "splits": {}}, "not a queue with this split"),
        ({"version": 1, "max_trials_per_run": 6, "splits": {"p": {"A": [1]}}}, "not a queue"),
    ],
    ids=["per run", "no split", "not a task id"],
)
def test_a_queue_it_will_not_run_is_refused(tmp_path: Path, data: Any, message: str) -> None:
    path = tmp_path / "queue.json"
    path.write_text(json.dumps(data), "utf-8")

    with pytest.raises(SessionError, match=message):
        load_queue(path, "p")


def test_the_committed_queue_loads_for_every_split() -> None:
    path = Path(__file__).parent.parent / "eval" / "sim" / "queue.json"

    pilot = load_queue(path, "pilot-v1")

    assert pilot.pilot and pilot.last and len(pilot.a) == 44
    assert not load_queue(path, "heldout-v1").pilot
