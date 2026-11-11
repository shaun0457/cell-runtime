"""The Phase 2 tables (`tools/sim/report.py`, #82): which trial of a unit counts (ADR 028), the
reconciliation with the scorer (ADR 029), and the discovery, A/B and confusion tables (ADR 026,
ADR 030). Each engine run is a directory laid out as `tools/sim/batch.py` writes it; the
store copies hold synthetic runs, so each test fixes the verdicts and the insertion it needs."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
import yaml

import tools.sim.report as report
from adapters.aic.manifest import load_manifest
from cellrt.runtime.events import (
    SCHEMA_VERSION,
    Event,
    MonitorVerdict,
    RecoveryStarted,
    RunFinished,
    RunStarted,
    SkillStarted,
    WorldReady,
)
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task
from tools.sim.report import ReportInputError, Trial, collect, load_runs, main
from tools.sim.scenes import (
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


class Session:
    """A results disk: a manifest and engine-run directories that share one growing store."""

    def __init__(self, root: Path, scenes: int = 6, name: str = NAME) -> None:
        self.root = root
        self.name = name
        self.scenes = [
            scene_from_trial(
                f"s{i}", "inrange_sfp" if i % 2 == 0 else "tilt_sfp", SAMPLE["trials"]["trial_1"]
            )
            for i in range(scenes)
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


def _events(unit: Unit, setting: str, wall: float, name: str = NAME) -> list[Event]:
    body: list[tuple[float, Any]] = []
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
    payloads: list[tuple[float, Any]] = [
        (0.0, start),
        (0.0, WorldReady(sim_origin_s=ORIGIN, port=None, plug=None, gripper=None)),
        *body,
    ]
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


def test_the_session_line_names_the_runs_that_did_not_end_ok(tmp_path: Path) -> None:
    s = Session(tmp_path)
    runs = load_runs([s.run("A", [Unit(0)]), s.run("A", [Unit(1)], exit="trials_not_processed")])

    line = report.session_line(runs)

    assert line == (
        f"engine runs: 2; commit: {COMMIT}; VM hours: 0.56; not ok: run2 (trials_not_processed)"
    )


def test_main_prints_the_four_tables(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)])

    assert main(["--manifest", str(s.manifest), "--run", str(path)]) == 0

    out = capsys.readouterr().out
    for heading in ("## Discovery", "## Reconciliation", "## A/B", "## Confusion"):
        assert heading in out
    assert report.NO_CLAIM_HERE in out  # the pilot's manifest


def test_main_claims_on_the_held_out_manifest_only_with_its_commit(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    s = Session(tmp_path, name="heldout-v1")
    paths = [str(p) for p in (s.run("A", [Unit(0)]), s.run("B", [Unit(0)]))]
    argv = ["--manifest", str(s.manifest), "--run", paths[0], "--run", paths[1]]

    assert main(argv) == 1
    assert capsys.readouterr().err.strip() == (
        "report: --commit: the held-out manifest needs its commit named"
    )
    assert main([*argv, "--commit", COMMIT]) == 0
    assert "claim (pooled, ADR 030)" in capsys.readouterr().out


def test_main_reports_a_refusal_by_its_fixed_message(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    s = Session(tmp_path)
    path = s.run("A", [Unit(0)])
    (path / "bags" / "trial_1.json").write_text('{"version": 2}', "utf-8")

    assert main(["--manifest", str(s.manifest), "--run", str(path)]) == 1
    assert capsys.readouterr().err.strip() == "report: bag dump: not a bag dump"
