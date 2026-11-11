"""The tracer's reconciliation row (`tools/sim/report.py`, #78): one row per trial of an engine
config, joining our run (from a read-only store), the scorer's `scoring.yaml` and the bag's
insertion events. `fixtures/sim/scoring_f1clean.yaml` is a real scorer file, from the F1 rerun
of the sample config."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from aic_fakes import FakeNode

import tools.sim.report as report
from adapters.aic.run import run_goal
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task
from tools.sim.report import (
    MAX_YAML_BYTES,
    ReportInputError,
    TrialRow,
    format_rows,
    trial_rows,
)
from tools.sim.scenes import build_manifest, engine_config, load_template, scene_from_trial

FIXTURES = Path(__file__).parent / "fixtures" / "sim"
TEMPLATE = Path(__file__).parent.parent / "eval" / "sim" / "engine_template.yaml"
TARGET = "nic_card_mount_0/sfp_port_0"


def _setup(tmp_path: Path, *, inserted: str | None, run: bool = True) -> dict[str, Path]:
    sample = yaml.safe_load((FIXTURES / "sample_config.yaml").read_text("utf-8"))
    scene = scene_from_trial("nominal_sfp", "nominal", sample["trials"]["trial_1"])
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps(build_manifest("tracer-v1", [scene], repeats=1)), "utf-8")
    config = tmp_path / "engine.yaml"
    config.write_text(yaml.safe_dump(engine_config([(scene, 0)], load_template(TEMPLATE))), "utf-8")
    store = tmp_path / "store.sqlite"
    with EventStore(store) as s:
        if run:
            task = Task(id="nominal_sfp_r0", time_limit=180.0)
            run_goal(
                FakeNode(task=task),
                task,
                s,
                scenario_id="tracer-v1/nominal_sfp",
                seed=0,
                scene_digest=scene.digest,
            )
    bags = tmp_path / "bags"
    bags.mkdir()
    if inserted is not None:
        dump = {"version": 1, "insertions": [{"recv_ns": 5, "port": inserted}]}
        dump |= {"contacts": [], "pairs": []}
        (bags / "trial_1.json").write_text(json.dumps(dump), "utf-8")
    return {"store": store, "config": config, "manifest": manifest, "bags": bags}


def _rows(paths: dict[str, Path]) -> list[TrialRow]:
    return trial_rows(
        store=paths["store"],
        engine_config=paths["config"],
        manifest=paths["manifest"],
        scoring=FIXTURES / "scoring_f1clean.yaml",
        bags=paths["bags"],
        setting="A",
    )


def test_a_run_the_scorer_saw_inserted_agrees(tmp_path: Path) -> None:
    (row,) = _rows(_setup(tmp_path, inserted=TARGET))

    assert row.trial_id == "trial_1" and row.task_id == "nominal_sfp_r0"
    assert row.run_id == "tracer-v1/nominal_sfp/A/0/a1"
    assert (row.outcome, row.verdicts) == ("success", 0)
    assert (row.inserted, row.target) == (TARGET, TARGET)
    assert row.tier3 == 75
    assert row.total is not None and 93 < row.total < 94  # tiers 1 + 2 + 3 of trial_1
    assert row.agrees is True


def test_a_success_the_scorer_did_not_see_disagrees(tmp_path: Path) -> None:
    (row,) = _rows(_setup(tmp_path, inserted=None))

    assert row.inserted is None
    assert row.agrees is False


def test_an_insertion_into_another_port_does_not_count(tmp_path: Path) -> None:
    (row,) = _rows(_setup(tmp_path, inserted="nic_card_mount_1/sfp_port_0"))

    assert row.inserted == "nic_card_mount_1/sfp_port_0"
    assert row.agrees is False


def test_a_port_name_with_a_leading_slash_is_read_as_the_scorer_reads_it(tmp_path: Path) -> None:
    # The cable plugin publishes the touch topic's namespace, which starts with "/"
    # (CablePlugin.cc:268-269, :324); the scorer drops empty parts (ScoringTier2.cc:822-832).
    (row,) = _rows(_setup(tmp_path, inserted=f"/{TARGET}"))

    assert row.into_target is True
    assert row.agrees is True


def test_the_last_insertion_event_is_the_one_the_scorer_kept(tmp_path: Path) -> None:
    paths = _setup(tmp_path, inserted=TARGET)
    events = [{"recv_ns": 5, "port": "/nic_card_mount_1/sfp_port_0"}]
    events.append({"recv_ns": 9, "port": f"/{TARGET}"})
    dump = {"version": 1, "insertions": events, "contacts": [], "pairs": []}
    (paths["bags"] / "trial_1.json").write_text(json.dumps(dump), "utf-8")

    (row,) = _rows(paths)

    assert row.inserted == f"/{TARGET}" and row.agrees is True


def test_a_bag_dump_over_its_bound_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _setup(tmp_path, inserted=TARGET)
    monkeypatch.setattr(report, "MAX_BAG_JSON_BYTES", 16)

    with pytest.raises(ReportInputError, match="bag dump: over 16 bytes"):
        _rows(paths)


def test_a_trial_without_a_run_has_no_verdict_to_compare(tmp_path: Path) -> None:
    (row,) = _rows(_setup(tmp_path, inserted=TARGET, run=False))

    assert row.run_id is None and row.outcome is None
    assert row.agrees is None


def test_the_store_is_read_without_writing_to_it(tmp_path: Path) -> None:
    paths = _setup(tmp_path, inserted=TARGET)
    before = paths["store"].read_bytes()

    _rows(paths)

    assert paths["store"].read_bytes() == before


def test_a_trial_id_that_could_name_another_file_is_refused(tmp_path: Path) -> None:
    paths = _setup(tmp_path, inserted=TARGET)
    config = yaml.safe_load(paths["config"].read_text("utf-8"))
    config["trials"] = {"../../outside": config["trials"]["trial_1"]}
    paths["config"].write_text(yaml.safe_dump(config), "utf-8")

    with pytest.raises(ReportInputError, match="trial id"):
        _rows(paths)


def test_an_input_over_its_bound_is_refused(tmp_path: Path) -> None:
    paths = _setup(tmp_path, inserted=TARGET)
    paths["config"].write_bytes(b"#" * (MAX_YAML_BYTES + 1))

    with pytest.raises(ReportInputError, match="over"):
        _rows(paths)


def test_rows_print_as_a_table_with_a_dash_for_what_is_missing(tmp_path: Path) -> None:
    rows = _rows(_setup(tmp_path, inserted=None))

    text = format_rows(rows)

    header, rule, line = text.splitlines()
    assert header.startswith("| trial ") and set(rule) <= {"|", "-", " "}
    assert "trial_1" in line and "nominal_sfp_r0" in line and "| - |" in line
