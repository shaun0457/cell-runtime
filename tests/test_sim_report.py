"""The Phase 2 tables (`tools/sim/report.py`, #82): which trial of a unit counts (ADR 028), the
reconciliation with the scorer (ADR 029), and the discovery, A/B and confusion tables (ADR 026,
ADR 030). Each engine run is a directory laid out as `tools/sim/batch.py` writes it; the
store copies hold synthetic runs, so each test fixes the verdicts and the insertion it needs."""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import json
import math
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

import tools.sim.report as report
from adapters.aic.manifest import load_manifest
from cellrt.eval.calibration import ForceVerdict, Measured
from cellrt.runtime.events import (
    SCHEMA_VERSION,
    Event,
    MonitorVerdict,
    PoseRecord,
    RecoveryStarted,
    RunFinished,
    RunStarted,
    SignalSampled,
    SkillStarted,
    WorldReady,
)
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task
from tools.sim.report import ReportInputError, Trial, collect, load_runs, main
from tools.sim.scenes import (
    Scene,
    build_manifest,
    engine_config,
    load_template,
    scene_from_trial,
    write_yaml,
)

FIXTURES = Path(__file__).parent / "fixtures" / "sim"
TEMPLATE = Path(__file__).parent.parent / "eval" / "sim" / "engine_template.yaml"
SAMPLE = yaml.safe_load((FIXTURES / "sample_config.yaml").read_text("utf-8"))
NAME = "pilot-v1"
TARGET = "nic_card_mount_0/sfp_port_0"
ORIGIN = 100.0  # the runs' sim time at run time 0
COMMIT = "a" * 40
REF_COMMIT = "b" * 40  # the reference (tuning) session's
BASE_TRIAL = scene_from_trial("base", "base", SAMPLE["trials"]["trial_1"]).trial


def _tilted_trial(degrees: float) -> dict[str, Any]:
    trial = copy.deepcopy(BASE_TRIAL)
    trial["scene"]["task_board"]["nic_rail_0"]["entity_pose"]["roll"] = math.radians(degrees)
    return trial


def _pose(position: tuple[float, float, float], turn_deg: float = 0.0) -> PoseRecord:
    """A pose turned by `turn_deg` about the base's x axis."""
    half = math.radians(turn_deg) / 2
    return PoseRecord(position=position, orientation=(math.cos(half), math.sin(half), 0.0, 0.0))


def _ready(port_turn_deg: float = 0.0, *, port: bool = True) -> WorldReady:
    """A readback whose port is turned `port_turn_deg` from the nominal one's."""
    return WorldReady(
        sim_origin_s=ORIGIN,
        port=_pose((0.5, 0.1, 0.2), port_turn_deg) if port else None,
        plug=_pose((0.4, 0.0, 0.3)),
        gripper=_pose((0.4, 0.0, 0.35)),
    )


@dataclass(frozen=True)
class Unit:
    """One trial of an engine run, and what the test wants its attempt and referee to be."""

    scene: int
    seed: int = 0
    attempt: int = 1
    run: bool = True  # an attempt reached the store
    finished: bool = True
    outcome: str = "success"
    verdict_at: float | None = None
    recovery_at: float | None = None
    insert_at: float | None = 5.0  # the insertion's run time; None: no insertion event
    port: str = f"/{TARGET}"  # the insertion event's port, as the cable plugin names it
    mapped: bool = True  # the bag has wrench pairs around the insertion
    contacts: int = 0
    dump: bool = True
    score: bool = True
    ready: WorldReady | None = None  # the run's readback; None: no pose read
    samples: tuple[float, ...] = ()  # forces sampled every 0.05 s in approach, 100 mm up, still


class Session:
    """A results disk: a manifest and engine-run directories that share one growing store.
    `kinds` gives scene i's family and trial; by default every scene is the sample's trial_1,
    alternately as `inrange_sfp` and `tilt_sfp`."""

    def __init__(
        self,
        root: Path,
        scenes: int = 6,
        name: str = NAME,
        kinds: Sequence[tuple[str, dict[str, Any]]] | None = None,
    ) -> None:
        root.mkdir(parents=True, exist_ok=True)
        self.root = root
        self.name = name
        if kinds is None:
            kinds = [
                ("inrange_sfp" if i % 2 == 0 else "tilt_sfp", BASE_TRIAL) for i in range(scenes)
            ]
        self.scenes = [
            Scene(scene_id=f"s{i}", family=family, trial=trial)
            for i, (family, trial) in enumerate(kinds)
        ]
        self.manifest = root / f"{name}.json"
        self.manifest.write_text(json.dumps(build_manifest(name, self.scenes, repeats=3)), "utf-8")
        self.events: list[Event] = []
        self.clock = 1000.0
        self.count = 0

    def run(
        self,
        setting: str,
        units: Sequence[Unit],
        *,
        scoring: bool = True,
        store: bool = True,
        commit: str = COMMIT,
        exit: str = "ok",
        extra: Sequence[Unit] = (),
    ) -> Path:
        """An engine run of `units`; `extra` are attempts in its window that its config lacks."""
        self.count += 1
        tag = f"run{self.count}"
        path = self.root / tag
        (path / "aic").mkdir(parents=True)
        (path / "bags").mkdir()
        started, ended = self.clock, self.clock + 1000.0
        self.clock = ended + 10.0
        config = engine_config(
            [(self.scenes[u.scene], u.seed) for u in units], load_template(TEMPLATE)
        )
        write_yaml(path / "engine.yaml", config)
        units_ids = [f"s{u.scene}_r{u.seed}" for u in units]
        _ledger(
            path,
            tag=tag,
            setting=setting,
            units=units_ids,
            started=started,
            ended=ended,
            commit=commit,
            exit=exit,
        )
        scores: dict[str, Any] = {"total": 0.0}
        for n, unit in enumerate([*units, *extra], start=1):
            if unit.run:
                self.events += _events(unit, setting, started + 10.0 * n, self.name)
            if n > len(units):
                continue
            if unit.score:
                scores[f"trial_{n}"] = _score(unit)
            if unit.dump:
                (path / "bags" / f"trial_{n}.json").write_text(json.dumps(_dump(unit)), "utf-8")
        if scoring:
            (path / "aic" / "scoring.yaml").write_text(yaml.safe_dump(scores), "utf-8")
        if store:
            (path / "snapshot").mkdir()
            with EventStore(path / "snapshot" / "store.sqlite") as copy:
                for event in self.events:
                    copy.append(event)
        return path

    def trials(self, *paths: Path) -> list[Trial]:
        return collect(load_manifest(self.manifest), load_runs(list(paths)))


def _ledger(path: Path, **row: Any) -> None:
    sha = hashlib.sha256((path / "engine.yaml").read_bytes()).hexdigest()
    full = {
        "tag": row["tag"],
        "setting": row["setting"],
        "units": row["units"],
        "started_wall": row["started"],
        "ended_wall": row["ended"],
        "hours": round((row["ended"] - row["started"]) / 3600.0, 6),
        "exit": row["exit"],
        "commit": row["commit"],
        "image": "sha256:x",
        "config_sha256": sha,
    }
    (path / "ledger.jsonl").write_text(json.dumps(full, sort_keys=True) + "\n", "utf-8")


def _signal(force: float) -> SignalSampled:
    return SignalSampled(
        step=0,
        height_mm=100.0,
        depth_mm=0.0,
        xy_offset_mm=(0.0, 0.0),
        xy_error_mm=0.0,
        raw_force_n=(0.0, 0.0, force),
        tare_offset_n=(0.0, 0.0, 0.0),
        force_n=force,
        torque_nm=0.0,
        wrench_valid=True,
    )


def _events(unit: Unit, setting: str, wall: float, name: str = NAME) -> list[Event]:
    body: list[tuple[float, Any]] = []
    if unit.samples:
        body.append((0.0, SkillStarted(skill="approach", attempt=1)))
        body += [(0.05 * k, _signal(force)) for k, force in enumerate(unit.samples, start=1)]
    if unit.verdict_at is not None:
        body.append((unit.verdict_at - 1.0, SkillStarted(skill="insert", attempt=1)))
        body.append(
            (
                unit.verdict_at,
                MonitorVerdict(skill="insert", failure_class="INSERT_STUCK", rule="r"),
            )
        )
    if unit.recovery_at is not None:
        body.append(
            (
                unit.recovery_at,
                RecoveryStarted(
                    skill="insert",
                    failure_class="INSERT_STUCK",
                    action="retract_realign",
                    count_for_class=1,
                    count_total=1,
                ),
            )
        )
    end = max([ts for ts, _ in body] + [unit.insert_at or 0.0]) + 5.0
    start = RunStarted(
        scenario_id=f"{name}/s{unit.scene}",
        setting=setting,
        seed=unit.seed,
        attempt=unit.attempt,
        task=Task(id=f"s{unit.scene}_r{unit.seed}", time_limit=180.0),
        config_hash="h",
        schema_version=SCHEMA_VERSION,
    )
    ready = unit.ready or WorldReady(sim_origin_s=ORIGIN, port=None, plug=None, gripper=None)
    payloads: list[tuple[float, Any]] = [(0.0, start), (0.0, ready), *body]
    if unit.finished:
        payloads.append((end, RunFinished(outcome=unit.outcome, reason="r", steps=1, duration=end)))  # type: ignore[arg-type]
    run_id = f"{name}/s{unit.scene}/{setting}/{unit.seed}/a{unit.attempt}"
    return [
        Event(run_id=run_id, seq=n, ts=ts, wall_ts=wall + ts, payload=p)
        for n, (ts, p) in enumerate(payloads, start=1)
    ]


def _score(unit: Unit) -> dict[str, Any]:
    inserted = unit.insert_at is not None and unit.port.strip("/") == TARGET
    categories = {
        "contacts": {"score": -24 if unit.contacts else 0},
        "insertion force": {"score": 0},
    }
    return {
        "tier_1": {"score": 1},
        "tier_2": {"score": 10.0, "categories": categories},
        "tier_3": {"score": 75 if inserted else 0},
    }


def _dump(unit: Unit) -> dict[str, Any]:
    insertions: list[dict[str, Any]] = []
    pairs: list[list[int]] = []
    if unit.insert_at is not None:
        recv = int((ORIGIN + unit.insert_at) * 1e9)  # the bag's clock is the sim's, here
        insertions.append({"recv_ns": recv, "port": unit.port})
        if unit.mapped:
            pairs = [[recv - 20_000_000, recv - 20_000_000], [recv + 20_000_000, recv + 20_000_000]]
    contacts = [{"recv_ns": 1, "count": 1} for _ in range(unit.contacts)]
    return {"version": 1, "insertions": insertions, "contacts": contacts, "pairs": pairs}


def _lines(table: str) -> list[str]:
    return table.split("\n\n")[0].splitlines()[2:]


def _edit_store(path: Path, sql: str, *params: object) -> None:
    """Corrupt a store copy, as a copy not written by EventStore could be."""
    db = sqlite3.connect(path / "snapshot" / "store.sqlite")
    try:
        with db:
            db.execute("DROP TRIGGER events_no_update")  # the store's own append-only guard
            db.execute(sql, params)
    finally:
        db.close()


# --- which trial counts ----------------------------------------------------------------------


def test_a_trial_the_scorer_saw_inserted_is_compensated_and_agrees(tmp_path: Path) -> None:
    s = Session(tmp_path)
    (t,) = s.trials(s.run("A", [Unit(0)]))

    assert (t.task_id, t.setting, t.tag, t.trial_id) == ("s0_r0", "A", "run1", "trial_1")
    assert t.judged is not None and t.judged.run_id == f"{NAME}/s0/A/0/a1"
    assert t.trial_class == "compensated"
    (line,) = _lines(report.reconciliation_table([t]))
    assert "| target | yes |" in line and "| mapped | compensated |" in line
    assert "| 86.0 |" in line  # tiers 1 + 2 + 3


def test_an_unmapped_insertion_with_no_verdict_is_not_judged_as_a_whole(tmp_path: Path) -> None:
    s = Session(tmp_path)
    (t,) = s.trials(s.run("A", [Unit(0, mapped=False)]))

    (line,) = _lines(report.reconciliation_table([t]))

    assert "| unmapped | compensated |" in line
    assert "- judged as a whole (no time mapping): none" in report.confusion_table([t])


def test_a_success_the_scorer_did_not_see_is_a_false_success(tmp_path: Path) -> None:
    s = Session(tmp_path)
    (t,) = s.trials(s.run("A", [Unit(0, insert_at=None)]))

    assert t.trial_class == "false_success"
    (line,) = _lines(report.reconciliation_table([t]))
    assert "| none | no |" in line


@pytest.mark.parametrize("port", [TARGET, f"/{TARGET}"])
def test_a_port_name_is_read_as_the_scorer_reads_it(tmp_path: Path, port: str) -> None:
    s = Session(tmp_path)
    (t,) = s.trials(s.run("A", [Unit(0, port=port)]))

    assert t.judged is not None and t.judged.inserted


def test_a_wrong_port_insertion_is_named_and_not_inserted(tmp_path: Path) -> None:
    s = Session(tmp_path)
    (t,) = s.trials(s.run("A", [Unit(0, port="/nic_card_mount_1/sfp_port_0")]))

    assert t.judged is not None and not t.judged.inserted and t.judged.wrong_port
    assert t.trial_class == "false_success"
    (line,) = _lines(report.reconciliation_table([t]))
    assert "| wrong port: nic_card_mount_1/sfp_port_0 | no |" in line


@pytest.mark.parametrize(
    ("unit", "scoring"),
    [
        (Unit(0, dump=False), True),
        (Unit(0, score=False), True),
        (Unit(0), False),
        (Unit(0, finished=False), True),
        (Unit(0, run=False), True),
    ],
    ids=["no bag dump", "no scoring entry", "no scoring file", "unfinished", "no attempt"],
)
def test_a_trial_without_a_finished_run_and_referee_data_is_infrastructure(
    tmp_path: Path, unit: Unit, scoring: bool
) -> None:
    s = Session(tmp_path)
    (t,) = s.trials(s.run("A", [unit], scoring=scoring))

    assert t.judged is None and t.trial_class == "infrastructure"
    (line,) = _lines(report.reconciliation_table([t]))
    assert line.endswith("| infrastructure (1 runs) |")


def test_an_engine_run_without_a_store_copy_leaves_its_units_infrastructure(tmp_path: Path) -> None:
    s = Session(tmp_path)
    (t,) = s.trials(s.run("A", [Unit(0, run=False)], store=False))

    assert t.trial_class == "infrastructure"


def test_a_rerun_counts_the_first_engine_run_with_referee_data(tmp_path: Path) -> None:
    s = Session(tmp_path)
    first = s.run("A", [Unit(0, dump=False)])
    second = s.run("A", [Unit(0, attempt=2)])

    (t,) = s.trials(second, first)  # in start order, whatever order they are given in

    assert (t.tag, t.engine_runs) == ("run2", 2)
    assert t.judged is not None and t.judged.run_id.endswith("/a2")


@pytest.mark.parametrize("given", ["in start order", "reversed"])
def test_a_counted_trial_is_not_replaced_by_a_later_one(tmp_path: Path, given: str) -> None:
    s = Session(tmp_path)
    first = s.run("A", [Unit(0)])
    second = s.run("A", [Unit(0, attempt=2, insert_at=None)])

    (t,) = s.trials(*((first, second) if given == "in start order" else (second, first)))

    assert (t.tag, t.trial_class) == ("run1", "compensated")


@pytest.mark.parametrize(("end", "joined"), [(10.0, True), (9.999, False)])
def test_an_attempt_started_at_the_windows_end_is_joined(
    tmp_path: Path, end: float, joined: bool
) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)])  # its attempt's run_started is 10 s after the run's start
    row = json.loads((path / "ledger.jsonl").read_text("utf-8"))
    _ledger(path, **{**row, "started": row["started_wall"], "ended": row["started_wall"] + end})

    (t,) = s.trials(path)

    assert (t.judged is not None) is joined


def test_a_session_with_an_engine_run_left_out_is_refused(tmp_path: Path) -> None:
    s = Session(tmp_path)
    first = s.run("A", [Unit(0)])
    s.run("B", [Unit(1)])
    last = s.run("A", [Unit(2)])

    with pytest.raises(ReportInputError, match="outside every engine run given"):
        s.trials(first, last)


def test_an_attempt_before_the_first_engine_run_given_is_another_sessions(tmp_path: Path) -> None:
    s = Session(tmp_path)
    s.run("A", [Unit(0)])
    later = s.run("A", [Unit(1)])

    (t,) = s.trials(later)

    assert t.task_id == "s1_r0" and t.judged is not None


def test_an_attempt_of_another_engine_run_is_not_joined(tmp_path: Path) -> None:
    # The second run's store copy holds the first run's attempt too, outside its window.
    s = Session(tmp_path)
    first = s.run("A", [Unit(0, dump=False)])
    second = s.run("A", [Unit(0, run=False)])

    (t,) = s.trials(first, second)

    assert (t.trial_class, t.engine_runs) == ("infrastructure", 2)


def test_two_attempts_at_a_unit_in_one_engine_run_are_refused(tmp_path: Path) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)], extra=[Unit(0, attempt=2)])

    with pytest.raises(ReportInputError, match="two attempts"):
        s.trials(path)


def test_settings_are_counted_apart(tmp_path: Path) -> None:
    s = Session(tmp_path)
    trials = s.trials(s.run("A", [Unit(0)]), s.run("B", [Unit(0)]))

    assert [(t.task_id, t.setting) for t in trials] == [("s0_r0", "A"), ("s0_r0", "B")]
    assert all(t.judged is not None for t in trials)


# --- refusals --------------------------------------------------------------------------------


def test_engine_runs_of_two_commits_are_refused(tmp_path: Path) -> None:
    s = Session(tmp_path)
    paths = [s.run("A", [Unit(0)]), s.run("B", [Unit(0)], commit="b" * 40)]

    with pytest.raises(ReportInputError, match="more than one commit"):
        load_runs(paths)


def test_an_engine_run_of_another_commit_than_asked_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)])

    code = main(["--manifest", str(s.manifest), "--run", str(path), "--commit", "b" * 40])

    assert code == 1
    assert capsys.readouterr().err.strip() == (
        "report: ledger: an engine run names another commit than --commit"
    )


@pytest.mark.parametrize("commit", ["unknown", "A" * 40, "a" * 39])
def test_a_commit_asked_for_must_be_40_hex_digits(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], commit: str
) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)], commit="unknown")

    assert main(["--manifest", str(s.manifest), "--run", str(path), "--commit", commit]) == 1
    assert capsys.readouterr().err.strip() == "report: --commit: not a commit's 40 hex digits"


def test_an_engine_config_other_than_the_hashed_one_is_refused(tmp_path: Path) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)])
    (path / "engine.yaml").write_bytes((path / "engine.yaml").read_bytes() + b"\n# edited\n")

    with pytest.raises(ReportInputError, match="not the file its ledger row hashed"):
        load_runs([path])


def test_a_ledger_whose_units_are_not_the_configs_is_refused(tmp_path: Path) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)])
    row = json.loads((path / "ledger.jsonl").read_text("utf-8"))
    row["units"] = ["s1_r0"]
    (path / "ledger.jsonl").write_text(json.dumps(row) + "\n", "utf-8")

    with pytest.raises(ReportInputError, match="units"):
        load_runs([path])


@pytest.mark.parametrize(
    ("edit", "message"),
    [
        (lambda row: "", "not one row"),
        (lambda row: json.dumps(row) + "\n" + json.dumps(row), "not one row"),
        (lambda row: "[1]", "not a ledger row"),
        (lambda row: "{", "not a ledger row"),
        (lambda row: json.dumps({**row, "setting": "D"}), "not a ledger row"),
        (lambda row: json.dumps({**row, "hours": True}), "not a ledger row"),
        (lambda row: json.dumps({**row, "hours": 10**400}), "not a ledger row"),  # no float
        (
            lambda row: json.dumps({k: v for k, v in row.items() if k != "commit"}),
            "not a ledger row",
        ),
        (
            lambda row: json.dumps({**row, "ended_wall": row["started_wall"] - 1}),
            "not a ledger row",
        ),
        (lambda row: json.dumps({**row, "tag": "run|1"}), "not a ledger row"),
        (lambda row: json.dumps({**row, "exit": "ok\n| x"}), "not a ledger row"),
        (lambda row: json.dumps({**row, "units": ["s0_r0", "s0_r0"]}), "a unit twice"),
    ],
    ids=[
        "empty",
        "two rows",
        "not a map",
        "not JSON",
        "setting",
        "bool",
        "overflow",
        "missing",
        "times",
        "tag",
        "exit",
        "unit twice",
    ],
)
def test_a_ledger_that_is_not_one_row_is_refused(tmp_path: Path, edit: Any, message: str) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)])
    row = json.loads((path / "ledger.jsonl").read_text("utf-8"))
    (path / "ledger.jsonl").write_text(edit(row), "utf-8")

    with pytest.raises(ReportInputError, match=message):
        load_runs([path])


def test_two_engine_runs_with_one_tag_are_refused(tmp_path: Path) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)])

    with pytest.raises(ReportInputError, match="share a tag"):
        load_runs([path, path])


def test_more_engine_runs_than_the_bound_are_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = Session(tmp_path)
    paths = [s.run("A", [Unit(0)]), s.run("A", [Unit(1)])]
    monkeypatch.setattr(report, "MAX_RUNS", 1)

    with pytest.raises(ReportInputError, match="over 1 engine runs"):
        load_runs(paths)


@pytest.mark.parametrize(
    ("bound", "message"),
    [("MAX_BAG_JSON_BYTES", "bag dump: over 16 bytes"), ("MAX_LEDGER_BYTES", "ledger: over 16")],
)
def test_an_input_over_its_bound_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bound: str, message: str
) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)])
    monkeypatch.setattr(report, bound, 16)

    with pytest.raises(ReportInputError, match=message):
        s.trials(path)


@pytest.mark.parametrize(
    ("name", "message"),
    [("engine.yaml", "engine config: over"), ("aic/scoring.yaml", r"scoring\.yaml: over")],
)
def test_a_yaml_file_over_its_bound_is_refused(tmp_path: Path, name: str, message: str) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)])
    (path / name).write_bytes(b"#" * (report.MAX_YAML_BYTES + 1))

    with pytest.raises(ReportInputError, match=message):
        load_runs([path])


def test_an_input_that_is_not_a_regular_file_is_refused(tmp_path: Path) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)])
    (path / "aic" / "scoring.yaml").unlink()
    (path / "aic" / "scoring.yaml").mkdir()

    with pytest.raises(ReportInputError, match=r"scoring\.yaml: not a regular file"):
        load_runs([path])


def test_a_scoring_file_with_a_date_that_is_no_date_is_refused(tmp_path: Path) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)])
    (path / "aic" / "scoring.yaml").write_text("trial_1: 2026-02-30\n", "utf-8")  # ValueError

    with pytest.raises(ReportInputError, match=r"scoring\.yaml: not YAML"):
        load_runs([path])


def test_a_trial_id_that_could_name_another_file_is_refused(tmp_path: Path) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)])
    config = yaml.safe_load((path / "engine.yaml").read_text("utf-8"))
    config["trials"] = {"../../outside": config["trials"]["trial_1"]}
    write_yaml(path / "engine.yaml", config)
    row = json.loads((path / "ledger.jsonl").read_text("utf-8"))
    _ledger(path, **{**row, "started": row["started_wall"], "ended": row["ended_wall"]})

    with pytest.raises(ReportInputError, match="trial id"):
        load_runs([path])


def test_a_scoring_file_that_is_not_a_map_is_refused(tmp_path: Path) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)])
    (path / "aic" / "scoring.yaml").write_text("- 1\n", "utf-8")

    with pytest.raises(ReportInputError, match=r"scoring\.yaml: not a scorer file"):
        load_runs([path])


def test_a_bag_dump_that_is_not_one_is_refused(tmp_path: Path) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)])
    (path / "bags" / "trial_1.json").write_text("{", "utf-8")

    with pytest.raises(ReportInputError, match="bag dump: not a bag dump"):
        s.trials(path)


@pytest.mark.parametrize(
    ("event_type", "payload"),
    [
        ("world_ready", '{"type": "world_ready", "sim_origin_s": "secret text"}'),
        ("run_finished", '{"type": "secret text"}'),
    ],
    ids=["a field", "a type"],
)
def test_a_store_event_the_schema_does_not_hold_is_refused_without_quoting_it(
    tmp_path: Path, event_type: str, payload: str
) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)])
    _edit_store(path, "UPDATE events SET payload = ? WHERE type = ?", payload, event_type)

    with pytest.raises(ReportInputError, match="schema does not hold") as refused:
        s.trials(path)

    assert "secret" not in str(refused.value)


def test_a_finished_run_out_of_order_is_refused(tmp_path: Path) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)])
    _edit_store(path, "UPDATE events SET seq = 99 WHERE type = 'world_ready'")  # after the end

    with pytest.raises(ReportInputError, match="out of order"):
        s.trials(path)


def test_a_store_copy_past_its_bound_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)])
    size = (path / "snapshot" / "store.sqlite").stat().st_size
    monkeypatch.setattr(report, "MAX_STORE_COPY_BYTES", size - 1)

    with pytest.raises(ReportInputError, match="store copy: over"):
        s.trials(path)


def test_a_store_copy_whose_wal_takes_it_past_its_bound_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)])
    copy = path / "snapshot" / "store.sqlite"
    monkeypatch.setattr(report, "MAX_STORE_COPY_BYTES", copy.stat().st_size + 9)
    s.trials(path)  # alone, within the bound
    (path / "snapshot" / "store.sqlite-wal").write_bytes(b"\0" * 10)

    with pytest.raises(ReportInputError, match="store copy: over"):
        s.trials(path)


def test_a_store_copy_whose_wal_cannot_be_read_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)])
    real = Path.stat

    def stat(self: Path, *args: Any, **kwargs: Any) -> Any:
        if self.name.endswith("-wal"):
            raise PermissionError("denied")
        return real(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", stat)

    with pytest.raises(ReportInputError, match="store copy: unreadable"):
        s.trials(path)


def test_a_store_copy_that_is_not_a_regular_file_is_refused(tmp_path: Path) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)])
    copy = path / "snapshot" / "store.sqlite"
    copy.unlink()
    copy.mkdir()

    with pytest.raises(ReportInputError, match="store copy: not a regular file"):
        s.trials(path)


def test_the_store_copy_is_read_without_writing_to_it(tmp_path: Path) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)])
    before = (path / "snapshot" / "store.sqlite").read_bytes()

    s.trials(path)

    assert (path / "snapshot" / "store.sqlite").read_bytes() == before


# --- the tables ------------------------------------------------------------------------------


def test_a_scene_whose_two_a_trials_differ_is_mixed(tmp_path: Path) -> None:
    s = Session(tmp_path)
    units = [Unit(0), Unit(0, seed=1, insert_at=None), Unit(1), Unit(1, seed=1), Unit(2)]
    trials = s.trials(s.run("A", units))

    rows = _lines(report.discovery_table(trials))

    assert rows == [
        "| s0 | inrange_sfp | compensated, false_success | yes |",
        "| s1 | tilt_sfp | compensated, compensated | no |",
        "| s2 | inrange_sfp | compensated | - |",
    ]


def _a_against_b(tmp_path: Path, scenes: int) -> list[Trial]:
    s = Session(tmp_path, scenes=scenes)
    a = [Unit(i, outcome="failed", verdict_at=20.0, insert_at=None) for i in range(scenes)]
    b = [Unit(i, verdict_at=20.0, recovery_at=21.0, insert_at=30.0) for i in range(scenes)]
    return s.trials(s.run("A", a), s.run("B", b))


def test_b_beats_a_when_six_scenes_all_go_its_way(tmp_path: Path) -> None:
    trials = _a_against_b(tmp_path, 6)

    table = report.ab_table(trials, held_out=True)

    pooled = [line for line in _lines(table) if line.startswith("| all |")]
    assert pooled == [
        "| all | A | 6 | 0 | [0.00, 0.39] | 0 | 0 | 0 | 0 | 0 | 25.0 | 0 |",
        "| all | B | 6 | 6 | [0.61, 1.00] | 6 | 0 | 6 | 0 | 0 | 35.0 | 0 |",
    ]
    assert table.endswith("scenes: B better 6, A better 0, tied 0; sign test p = 0.0312: B beats A")


def test_five_scenes_are_too_few_for_a_claim(tmp_path: Path) -> None:
    # The intervals part at n = 5 ([0, 0.43] and [0.57, 1]), but 5 scenes give p = 0.0625.
    assert report.ab_table(_a_against_b(tmp_path, 5), held_out=True).endswith(
        "sign test p = 0.0625: no detectable gain at this n"
    )


def test_a_claim_needs_both_settings(tmp_path: Path) -> None:
    s = Session(tmp_path)
    trials = s.trials(s.run("A", [Unit(0)]))

    assert report.ab_table(trials, held_out=True).endswith(
        "claim: needs counted trials in both A and B"
    )


def test_a_manifest_other_than_the_held_out_gets_no_claim(tmp_path: Path) -> None:
    table = report.ab_table(_a_against_b(tmp_path, 6), held_out=False)

    assert table.endswith(report.NO_CLAIM_HERE) and "sign test" not in table


def test_infrastructure_is_counted_apart_from_the_rates(tmp_path: Path) -> None:
    s = Session(tmp_path)
    trials = s.trials(s.run("A", [Unit(0), Unit(1, dump=False)]))

    table = report.ab_table(trials, held_out=False)
    pooled = [line for line in _lines(table) if line.startswith("| all |")]
    assert pooled == ["| all | A | 1 | 1 | [0.21, 1.00] | 1 | 0 | 0 | 0 | 0 | 10.0 | 1 |"]


def test_the_confusion_table_counts_every_verdict_and_names_the_fallbacks(tmp_path: Path) -> None:
    s = Session(tmp_path)
    units = [
        Unit(0, outcome="failed", verdict_at=20.0, insert_at=5.0),  # inserted long before
        Unit(1, outcome="failed", verdict_at=20.0, insert_at=5.0, mapped=False),  # whole trial
        Unit(2, outcome="failed", verdict_at=5.1, insert_at=5.0),  # a tie within tau
        Unit(3, outcome="failed", verdict_at=20.0, insert_at=None),  # a real failure, A stopped
    ]
    trials = s.trials(s.run("A", units))

    table = report.confusion_table(trials)

    assert _lines(table) == ["| A | INSERT_STUCK | 4 | 3 | 1 | 0 | 0 | 1 | 1 |"]
    assert "- false alarms: 3; without the 1 trials judged as a whole: 2" in table
    assert "- judged as a whole (no time mapping): s1_r0 (A)" in table


def test_a_pipe_in_a_port_name_cannot_split_a_row(tmp_path: Path) -> None:
    s = Session(tmp_path)
    (t,) = s.trials(s.run("A", [Unit(0, port="/nic|x/sfp_port_0")]))

    (line,) = _lines(report.reconciliation_table([t]))

    assert "| wrong port: nic\\|x/sfp_port_0 |" in line
    assert line.replace("\\|", "").count("|") == len(report.RECONCILIATION) + 1


def test_a_control_character_in_a_port_name_does_not_reach_the_terminal(tmp_path: Path) -> None:
    s = Session(tmp_path)
    (t,) = s.trials(s.run("A", [Unit(0, port="/nic\x1b[2J\x07\x9b/sfp_port_0")]))

    (line,) = _lines(report.reconciliation_table([t]))

    assert "| wrong port: nic?[2J??/sfp_port_0 |" in line


def test_the_session_line_names_the_runs_that_did_not_end_ok(tmp_path: Path) -> None:
    s = Session(tmp_path)
    runs = load_runs([s.run("A", [Unit(0)]), s.run("A", [Unit(1)], exit="trials_not_processed")])

    line = report.session_line(runs)

    assert line == (
        f"engine runs: 2; commit: {COMMIT}; VM hours: 0.56; not ok: run2 (trials_not_processed)"
    )


def test_main_prints_the_six_tables(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)])

    assert main(["--manifest", str(s.manifest), "--run", str(path)]) == 0

    out = capsys.readouterr().out
    headings = ("Discovery", "Applied", "Reconciliation", "A/B", "Confusion", "Calibration")
    for heading in headings:
        assert f"## {heading}" in out
    assert report.NO_CLAIM_HERE in out  # the pilot's manifest


# --- calibration -------------------------------------------------------------------------------


def test_a_counted_trial_carries_its_calibration_series(tmp_path: Path) -> None:
    s = Session(tmp_path)

    (t,) = s.trials(s.run("A", [Unit(0, samples=(0.5,) * 11)]))

    assert t.port_type == "sfp" and t.calibration is not None
    assert t.calibration.free_space_n == (0.5,) * 11  # every fresh sample of approach
    # still from the 2nd sample, so at rest from the 6th: 100 mm above the SFP entrance
    assert t.calibration.rest_approach_n == (0.5,) * 6
    assert t.calibration.periods_s == pytest.approx((0.05,) * 10)


def test_rest_is_read_above_the_port_types_entrance(tmp_path: Path) -> None:
    unknown = copy.deepcopy(BASE_TRIAL)
    unknown["task"]["port_type"] = "lc"
    s = Session(tmp_path, kinds=[("inrange_sfp", BASE_TRIAL), ("inrange_lc", unknown)])
    manifest = load_manifest(s.manifest)
    by_scene = {manifest.units[task_id].scene: task_id for task_id in sorted(manifest.units)}

    assert report._free_above_mm(manifest, by_scene["s0"]) == pytest.approx(
        45.8 + report.REST_CLEARANCE_MM
    )
    assert report._free_above_mm(manifest, by_scene["s1"]) == math.inf  # no profile: nowhere


def test_the_calibration_table_pools_the_counted_trials_per_port_type(tmp_path: Path) -> None:
    s = Session(tmp_path)
    (t,) = s.trials(s.run("A", [Unit(0)]))
    measured = Measured(
        samples=11,
        periods_s=(0.05,) * 10,
        not_fresh=1,
        dropped=2,
        free_space_n=(0.5,) * 10,
        rest_approach_n=(0.2,) * 3,
        rest_align_n=(0.3,) * 10,
        force_verdicts=(
            ForceVerdict("INSERT_STUCK", 0.5),
            ForceVerdict("INSERT_STUCK", 0.7),
            ForceVerdict("FORCE_LIMIT", 0.6, over_s=0.52),
            ForceVerdict("FORCE_LIMIT", None, repeat=True, over_s=0.57),
            ForceVerdict("FORCE_LIMIT", None),
        ),
    )
    trials = [
        dataclasses.replace(t, calibration=measured),
        dataclasses.replace(t, setting="B", calibration=measured, port_type="sc"),
        dataclasses.replace(t, setting="C", judged=None),  # infrastructure: not counted
    ]

    table = report.calibration_table(trials)

    period = "mean 50.0, sd 0.0, p95 50.0, max 50.0"
    free = "mean 0.50, sd 0.00, p95 0.50, max 0.50"
    align = "mean 0.30, sd 0.00, p95 0.30, max 0.30"
    stuck = "rise mean 0.60 s, max 0.70 s"
    limit = "rise mean 0.60 s, max 0.60 s; over the limit at it mean 0.52 s, max 0.52 s"
    one = (
        f"| 1 | 11 | {period} | 1 | 2 | {free} | undefined | {align} |"
        f" 2 verdicts, 0 repeats, 0 untimed; {stuck} | 3 verdicts, 1 repeats, 1 untimed; {limit} |"
    )
    assert _lines(table) == [
        f"| sc {one}",
        f"| sfp {one}",
        f"| all | 2 | 22 | {period} | 2 | 4 | {free} | undefined | {align} |"
        f" 4 verdicts, 0 repeats, 0 untimed; {stuck} | 6 verdicts, 2 repeats, 2 untimed; {limit} |",
    ]
    assert "INSERT_STUCK from the first of the consecutive fresh samples above 10 N" in table
    assert "added to the Monitor's sum over 20 N" in table


def test_a_class_without_verdicts_is_none(tmp_path: Path) -> None:
    s = Session(tmp_path)
    (t,) = s.trials(s.run("A", [Unit(0)]))

    (row,) = [r for r in _lines(report.calibration_table([t])) if r.startswith("| all |")]

    assert row.endswith("| none | none |")


def _reference(
    root: Path, runs: int = 1, ready: WorldReady | None = None
) -> tuple[Session, list[Path]]:
    """A tuning session whose nominal SFP trials read `ready`, by default every pose."""
    ref = Session(root, name="tune-v1", kinds=[("nominal_sfp", BASE_TRIAL)])
    ready = ready or _ready()
    return ref, [
        ref.run("A", [Unit(0, seed=k, ready=ready)], commit=REF_COMMIT) for k in range(runs)
    ]


def test_main_claims_on_the_held_out_manifest_only_with_its_commit_and_references(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    s = Session(tmp_path / "heldout", name="heldout-v1")
    paths = [str(p) for p in (s.run("A", [Unit(0)]), s.run("B", [Unit(0)]))]
    argv = ["--manifest", str(s.manifest), "--run", paths[0], "--run", paths[1]]
    ref, (ref_path,) = _reference(tmp_path / "tune")

    assert main(argv) == 1
    assert capsys.readouterr().err.strip() == (
        "report: --commit: the held-out manifest needs its commit named"
    )
    assert main([*argv, "--commit", COMMIT]) == 1
    assert capsys.readouterr().err.strip() == (
        "report: --reference-manifest: the held-out manifest holds no nominal scene; name the"
        " tuning session's"
    )
    references = ["--reference-manifest", str(ref.manifest), "--reference-run", str(ref_path)]
    assert main([*argv, "--commit", COMMIT, *references]) == 0
    assert "claim (pooled, ADR 030)" in capsys.readouterr().out


def test_a_tilt_that_did_not_take_effect_is_left_out_of_every_rate(tmp_path: Path) -> None:
    kinds = [
        ("nominal_sfp", BASE_TRIAL),
        ("tilt_sfp", _tilted_trial(2.0)),
        ("tilt_sfp", _tilted_trial(4.0)),
    ]
    s = Session(tmp_path, kinds=kinds)
    units = [
        Unit(0, ready=_ready()),
        Unit(1, ready=_ready(2.0)),
        Unit(2, ready=_ready(), outcome="failed", verdict_at=20.0, insert_at=None),  # not tilted
    ]
    path = s.run("A", units)
    manifest, trials = load_manifest(s.manifest), s.trials(path)

    refs = report.references(manifest, trials)
    applied = report.applied_scenes(manifest, trials, refs)

    assert refs["sfp"].run_id == f"{NAME}/s0/A/0/a1"
    assert {k: a.status for k, a in applied.items()} == {
        "s0": "not checked",
        "s1": "applied",
        "s2": "not applied",
    }
    table = report.applied_table(applied, refs)
    assert _lines(table) == [
        "| s0 | nominal_sfp | - | - | - | - | 0/1 | not checked |",
        "| s1 | tilt_sfp | tilt | 2.00 deg | 2.00 deg | 0.25 deg | 1/1 | applied |",
        "| s2 | tilt_sfp | tilt | 4.00 deg | 0.00 deg | 0.25 deg | 1/1 | not applied |",
    ]
    out = report.render(load_runs([path]), trials, held_out=False, applied=applied, refs=refs)
    assert f"- reference per port type: sfp: {NAME}/s0/A/0/a1" in out
    assert "- left out of every rate (not applied): s2" in out
    (pooled,) = [line for line in out.splitlines() if line.startswith("| all | A |")]
    assert pooled.startswith("| all | A | 2 | 2 |")  # s2's failed trial is not among them
    assert "| A | INSERT_STUCK |" not in out  # nor among the verdicts
    assert "| s2 | tilt_sfp | natural_failure_known |" in out  # the discovery table lists it


def test_a_scene_without_a_reference_is_kept_and_named(tmp_path: Path) -> None:
    s = Session(tmp_path)  # tilt_sfp scenes, no nominal one
    trials = s.trials(s.run("A", [Unit(0), Unit(1, ready=_ready())]))
    manifest = load_manifest(s.manifest)

    applied = report.applied_scenes(manifest, trials, report.references(manifest, trials))

    assert {k: a.status for k, a in applied.items()} == {"s0": "not checked", "s1": "no reference"}
    assert "- no reference: no counted nominal trial" in report.applied_table(applied, {})


def test_a_nominal_trial_that_did_not_read_every_pose_is_no_reference(tmp_path: Path) -> None:
    s = Session(tmp_path, kinds=[("nominal_sfp", BASE_TRIAL), ("tilt_sfp", _tilted_trial(2.0))])
    trials = s.trials(s.run("A", [Unit(0, ready=_ready(port=False)), Unit(1, ready=_ready(2.0))]))

    assert report.references(load_manifest(s.manifest), trials) == {}


def test_a_scene_whose_trials_read_no_pose_has_no_readback(tmp_path: Path) -> None:
    s = Session(tmp_path, kinds=[("nominal_sfp", BASE_TRIAL), ("tilt_sfp", _tilted_trial(2.0))])
    trials = s.trials(s.run("A", [Unit(0, ready=_ready()), Unit(1, ready=_ready(port=False))]))
    manifest = load_manifest(s.manifest)

    applied = report.applied_scenes(manifest, trials, report.references(manifest, trials))

    assert applied["s1"].status == "no readback" and applied["s1"].read == 0
    assert report.applied_table(applied, {}).endswith(
        "- kept in every rate, unchecked (no readback: no counted trial read the poses its check"
        " needs): s1"
    )


def test_a_nominal_trial_whose_readback_gives_no_reading_is_passed_over(tmp_path: Path) -> None:
    s = Session(tmp_path, kinds=[("nominal_sfp", BASE_TRIAL)])
    flat = _ready().model_copy(
        update={"port": PoseRecord(position=(0.5, 0.1, 0.2), orientation=(0.0, 0.0, 0.0, 0.0))}
    )
    trials = s.trials(s.run("A", [Unit(0, ready=flat), Unit(0, seed=1, ready=_ready())]))

    refs = report.references(load_manifest(s.manifest), trials)

    assert refs["sfp"].run_id == f"{NAME}/s0/A/1/a1"  # seed 0's port has a zero quaternion


def test_the_reference_session_need_not_be_given_whole(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    s = Session(tmp_path / "pilot", kinds=[("tilt_sfp", _tilted_trial(2.0))])
    path = s.run("A", [Unit(0, ready=_ready(2.0))])
    ref, ref_paths = _reference(tmp_path / "tune", runs=3)
    gapped = ["--reference-run", str(ref_paths[0]), "--reference-run", str(ref_paths[2])]

    argv = ["--manifest", str(s.manifest), "--run", str(path), "--reference-manifest"]
    assert main([*argv, str(ref.manifest), *gapped]) == 0

    out = capsys.readouterr().out
    assert "| s0 | tilt_sfp | tilt | 2.00 deg | 2.00 deg | 0.25 deg | 1/1 | applied |" in out
    assert f"- reference per port type: sfp: tune-v1/s0/A/0/a1 (commit {REF_COMMIT})\n" in out
    assert "- nominal spread, sfp: 1 other trials; port 0.00 deg, 0.00 mm" in out


@pytest.mark.parametrize(
    ("turn_deg", "wider"), [(0.1, ""), (0.3, "; wider than a tolerance")], ids=["within", "wider"]
)
def test_the_sessions_own_reference_wins_with_its_commit_and_spread(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], turn_deg: float, wider: str
) -> None:
    kinds = [("nominal_sfp", BASE_TRIAL), ("tilt_sfp", _tilted_trial(2.0))]
    s = Session(tmp_path / "pilot", kinds=kinds)
    units = [Unit(0, ready=_ready()), Unit(0, seed=1, ready=_ready(turn_deg))]
    path = s.run("A", [*units, Unit(1, ready=_ready(2.0))])
    ref, (ref_path,) = _reference(tmp_path / "tune")
    argv = ["--manifest", str(s.manifest), "--run", str(path)]
    references = ["--reference-manifest", str(ref.manifest), "--reference-run", str(ref_path)]

    assert main([*argv, *references]) == 0

    out = capsys.readouterr().out
    assert f"- reference per port type: sfp: {NAME}/s0/A/0/a1 (commit {COMMIT})\n" in out
    assert (
        f"- nominal spread, sfp: 1 other trials; port {turn_deg:.2f} deg, 0.00 mm in the board's"
        f" frame; plug 0.00 mm, 0.000 rad in the gripper's frame{wider}\n- left out"
    ) in out


@pytest.mark.parametrize(
    ("trial", "ready", "refused"),
    [
        (_tilted_trial(2.0), _ready(), False),
        (_tilted_trial(2.0), _ready(port=False), True),  # the reference read no port: none
        (BASE_TRIAL, _ready(), True),  # a tilt scene that tilts nothing
    ],
    ids=["checked", "no reference", "changes no value"],
)
def test_a_held_out_scene_that_could_not_be_checked_is_refused(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
    trial: dict[str, Any],
    ready: WorldReady,
    refused: bool,
) -> None:
    s = Session(tmp_path / "heldout", name="heldout-v1", kinds=[("tilt_sfp", trial)])
    path = s.run("A", [Unit(0, ready=_ready(2.0))])
    ref, (ref_path,) = _reference(tmp_path / "tune", ready=ready)
    argv = ["--manifest", str(s.manifest), "--run", str(path), "--commit", COMMIT]
    references = ["--reference-manifest", str(ref.manifest), "--reference-run", str(ref_path)]

    assert main([*argv, *references]) == (1 if refused else 0)

    err = capsys.readouterr().err.strip()
    assert err == (
        (
            "report: --reference-run: a held-out scene of a checked family could not be checked"
            " against a reference (1 scenes)"
        )
        if refused
        else ""
    )


def test_a_held_out_scene_with_no_counted_trial_is_not_refused() -> None:
    applied = {"s0": report.Applied("s0", "tilt_sfp", report.NO_REFERENCE, counted=0)}

    report.check_held_out(applied)  # nothing to leave out of a rate


@pytest.mark.parametrize(
    ("port_type", "read"),
    [
        ("sfp", "sfp"),
        ("sc", "sc"),
        ("SFP", None),
        ("sfp\x1b[2J", None),
        ("x" * 41, None),
        (3, None),
    ],
)
def test_a_port_type_is_read_only_as_a_short_lowercase_name(
    port_type: object, read: str | None
) -> None:
    assert report._port_type({"task": {"port_type": port_type}}) == read


def test_a_control_character_in_a_reference_does_not_reach_the_terminal() -> None:
    ref = report.Reference(BASE_TRIAL, _ready(), "run\x1b[2J\x9b", COMMIT)

    table = report.applied_table({}, {"sfp": ref})

    assert f"- reference per port type: sfp: run?[2J? (commit {COMMIT})" in table
    assert not report._CONTROL.search(table.replace("\n", ""))


@pytest.mark.parametrize("given", ["--reference-manifest", "--reference-run"])
def test_the_reference_flags_go_together(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], given: str
) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)])

    assert main(["--manifest", str(s.manifest), "--run", str(path), given, str(path)]) == 1
    assert capsys.readouterr().err.strip() == (
        "report: --reference-manifest and --reference-run go together"
    )


def test_main_reports_a_refusal_by_its_fixed_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)])
    (path / "bags" / "trial_1.json").write_text('{"version": 2}', "utf-8")

    assert main(["--manifest", str(s.manifest), "--run", str(path)]) == 1
    assert capsys.readouterr().err.strip() == "report: bag dump: not a bag dump"
