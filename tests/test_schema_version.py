"""The event schema version: new runs record it, and stores written before it existed stay
readable, so a payload change does not orphan recorded runs.

The old rows below were copied as stored from a Phase 0 store (before `config_parts`,
`xy_offset_mm` and `schema_version` existed).
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest
from pydantic import ValidationError

from cellrt.eval.metrics import compute_metrics, summarize_run
from cellrt.eval.runner import run_unit
from cellrt.eval.scenarios import Scenario
from cellrt.replay import replay_lines
from cellrt.runtime.events import (
    PAYLOAD_ADAPTER,
    SCHEMA_VERSION,
    PlannerDecided,
    RunStarted,
    SignalSampled,
    parse_stored,
)
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task

OLD_RUN_ID = "yaw_mismatch/C/0/a1"
OLD_RUN_STARTED = (
    '{"type":"run_started","scenario_id":"yaw_mismatch","setting":"C","seed":0,"attempt":1,'
    '"task":{"id":"insert-sfp-0","cable_type":"sfp","cable_name":"cable_0","plug_type":"sfp",'
    '"plug_name":"plug","port_type":"sfp","port_name":"port_0",'
    '"target_module_name":"nic_card_0","time_limit":60.0},"config_hash":"d5f8268b7252c504"}'
)
OLD_SIGNAL = (
    '{"type":"signal_sampled","step":1,"height_mm":49.0,"depth_mm":0.0,"xy_error_mm":2.654394,'
    '"raw_force_n":[0.0,0.0,6.108],"tare_offset_n":[0.0,0.0,6.0],"force_n":0.108,'
    '"torque_nm":0.0,"wrench_valid":true}'
)
OLD_RUN_FINISHED = (
    '{"type":"run_finished","outcome":"success","reason":"task complete","steps":5,'
    '"duration":10.05}'
)


def _write_old_run(db: Path) -> None:
    """Insert the old rows as they were stored, bypassing today's models."""
    rows = [
        (OLD_RUN_ID, 1, 0.0, 0.0, "run_started", OLD_RUN_STARTED),
        (OLD_RUN_ID, 2, 0.05, 0.0, "signal_sampled", OLD_SIGNAL),
        (OLD_RUN_ID, 3, 10.05, 0.0, "run_finished", OLD_RUN_FINISHED),
    ]
    with sqlite3.connect(db) as conn:
        conn.executemany("INSERT INTO events VALUES (?, ?, ?, ?, ?, ?)", rows)


def test_a_new_run_records_the_current_schema_version(
    store: EventStore, baseline: Scenario
) -> None:
    done = run_unit(baseline, "A", 0, store)
    started = store.events(done.run_id)[0].payload

    assert isinstance(started, RunStarted)
    assert started.schema_version == SCHEMA_VERSION == 12


# The JSON schema of every payload, hashed, per schema version. A payload that changes shape
# changes the hash: bump SCHEMA_VERSION and record the new hash under it, so two stores with
# the same version always have the same shape. 2: #21 (`schema_version` itself); 3: #23
# (`signal_sampled.force_over_limit_s`); 4: #24 (`run_started.geometry_source`); 5: #25
# (`hold_checked`); 6: #26 (`signal_sampled.wrench_dropped`); 7: #45 (`planner_responded`); 8: #47
# (`planner_timed_out`); 9: #56 (`planner_preempted`); 10: #57
# (`planner_decided.waited_sim_s`); 11: task plans, ADR 023 (`task_planned`,
# `task_plan_rejected`); 12: a model task planner's trace, ADR 023 (`task_plan_responded`).
# Descriptions (docstrings) are left out; a pydantic upgrade that only changes how the schema
# is written needs the hash re-recorded, not a bump.
PAYLOAD_SHAPES = {
    3: "b20ca3e30bfafeb4",
    4: "492633644cf97f43",
    5: "57dffe4ea4ad7676",
    6: "42793e4db84efc68",
    7: "80b2ac2afc65dd29",
    8: "c6ab31fcb5be31c8",
    9: "32fa6aa6b3219d84",
    10: "0d6439e812ec5b65",
    11: "e8e6f75cabf4e1b1",
    12: "3fd5fa028bfd7b4f",
}


def _shape(schema: object) -> object:
    """The schema without descriptions: a payload's docstring is not part of its shape."""
    if isinstance(schema, dict):
        return {
            k: _shape(v)
            for k, v in schema.items()
            if not (k == "description" and isinstance(v, str))  # not a field named so
        }
    if isinstance(schema, list):
        return [_shape(v) for v in schema]
    return schema


def test_the_payload_shape_matches_the_schema_version() -> None:
    schema = json.dumps(_shape(PAYLOAD_ADAPTER.json_schema()), sort_keys=True)
    shape = hashlib.sha256(schema.encode()).hexdigest()[:16]

    assert PAYLOAD_SHAPES.get(SCHEMA_VERSION) == shape


def test_a_decision_stored_before_schema_10_reads_as_taking_no_sim_time() -> None:
    row = {"type": "planner_decided", "skill": "insert_scripted", "decision": {"kind": "retry"}}

    decided = PAYLOAD_ADAPTER.validate_python(row)

    assert isinstance(decided, PlannerDecided)
    assert decided.waited_sim_s == 0.0


def test_a_run_started_stored_before_versioning_reads_as_version_1() -> None:
    started = parse_stored("run_started", OLD_RUN_STARTED)

    assert isinstance(started, RunStarted)
    assert started.schema_version == 1
    assert started.config_parts == {}


def test_an_old_signal_sample_without_later_optional_fields_still_parses() -> None:
    sample = parse_stored("signal_sampled", OLD_SIGNAL)

    assert isinstance(sample, SignalSampled)
    assert sample.xy_offset_mm is None and sample.force_n == 0.108


def test_replay_and_metrics_read_a_store_that_mixes_old_and_new_runs(
    tmp_path: Path, baseline: Scenario
) -> None:
    db = tmp_path / "mixed.sqlite"
    with EventStore(db) as store:
        new_run = run_unit(baseline, "A", 0, store).run_id
    _write_old_run(db)

    with EventStore(db) as store:
        old = store.events(OLD_RUN_ID)
        new = store.events(new_run)
        old_lines = list(replay_lines(old))
        new_lines = list(replay_lines(new))
        table = compute_metrics([old, new], k=1)

    assert [e.payload.schema_version for e in (old[0], new[0])] == [1, SCHEMA_VERSION]
    assert "yaw_mismatch" in old_lines[0] and "run_finished" in old_lines[-1]
    assert "run_finished" in new_lines[-1]
    assert summarize_run(old) is not None and summarize_run(new) is not None
    assert {row.setting for row in table.rows} == {"A", "C"}


def test_a_writer_must_state_the_schema_version() -> None:
    with pytest.raises(ValidationError, match="schema_version"):
        RunStarted(scenario_id="s", setting="A", seed=0, attempt=1, task=Task(), config_hash="h")
