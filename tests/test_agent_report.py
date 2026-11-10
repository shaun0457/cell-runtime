"""The agent report (#51): B, C-rule, C-fixed and C-LLM side by side. Every metric runs on
hand-built event logs with known answers; the CLI reads real stores left by `run_unit`."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cellrt.eval.agent_report import (
    AgentRunSummary,
    StoreRefused,
    parse_label,
    percentile,
    render_agent_report,
    summarize_agent_run,
    variant_rows,
)
from cellrt.eval.runner import run_unit
from cellrt.eval.scenarios import load_scenarios
from cellrt.planner.fixed_planner import FixedPlanner
from cellrt.runtime.events import (
    SCHEMA_VERSION,
    Event,
    MonitorVerdict,
    Payload,
    PlannerCalled,
    PlannerDecided,
    PlannerOutputRejected,
    PlannerPreempted,
    PlannerResponded,
    PlannerTimedOut,
    RunFinished,
    RunStarted,
    SignalSampled,
    SkillStarted,
)
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task

REPO = Path(__file__).resolve().parent.parent
PHASE3 = REPO / "eval" / "scenarios" / "phase3.yaml"


def _log(
    *steps: tuple[float, Payload],
    seed: int = 0,
    scenario: str = "damaged_port",
    setting: str = "C",
    attempt: int = 1,
    config_hash: str = "h",
    config_parts: dict[str, str] | None = None,
) -> list[Event]:
    run_id = f"{scenario}/{setting}/{seed}/a{attempt}"
    started = RunStarted(
        scenario_id=scenario,
        setting=setting,
        seed=seed,
        attempt=attempt,
        task=Task(),
        config_hash=config_hash,
        config_parts=config_parts or {},
        schema_version=SCHEMA_VERSION,
    )
    return [
        Event(run_id=run_id, seq=i, ts=ts, wall_ts=0.0, payload=p)
        for i, (ts, p) in enumerate([(0.0, started), *steps], start=1)
    ]


def _finish(outcome: str, steps: int, at: float) -> tuple[float, RunFinished]:
    return at, RunFinished(outcome=outcome, reason="r", steps=steps, duration=at)  # type: ignore[arg-type]


def _signal(step: int) -> SignalSampled:
    return SignalSampled(
        step=step,
        height_mm=0.0,
        depth_mm=0.0,
        xy_offset_mm=(0.0, 0.0),
        xy_error_mm=0.0,
        raw_force_n=(0.0, 0.0, 1.0),
        tare_offset_n=(0.0, 0.0, 0.0),
        force_n=1.0,
        torque_nm=0.0,
        wrench_valid=True,
    )


def _started(skill: str) -> SkillStarted:
    return SkillStarted(skill=skill, attempt=1)


def _verdict(failure_class: str) -> MonitorVerdict:
    return MonitorVerdict(skill="insert_scripted", failure_class=failure_class, rule="r")


def _called(call: int) -> PlannerCalled:
    return PlannerCalled(skill="insert_scripted", failure_class="UNKNOWN", call=call)


def _decided(kind: str) -> PlannerDecided:
    return PlannerDecided(skill="insert_scripted", decision={"kind": kind})


def _rejected(*errors: str) -> PlannerOutputRejected:
    return PlannerOutputRejected(skill="insert_scripted", raw="", errors=list(errors))


def _responded(latency_s: float, tokens: tuple[int | None, int | None]) -> PlannerResponded:
    return PlannerResponded(
        skill="insert_scripted",
        provider="nim",
        model="m",
        prompt="[]",
        response="{}",
        prompt_tokens=tokens[0],
        completion_tokens=tokens[1],
        latency_s=latency_s,
    )


# --- one run --------------------------------------------------------------------------------


def test_time_and_steps_after_the_first_unknown_count_as_wasted_on_a_failed_run() -> None:
    # Steps are skill steps, as in run_finished.steps: the skills started after the verdict.
    events = _log(
        (0.0, _started("approach")),
        (1.0, _started("insert_scripted")),
        (1.0, _signal(20)),
        (4.0, _verdict("INSERT_STUCK")),  # a known class first: not the start of the waste
        (4.0, _started("retract")),
        (4.5, _started("insert_scripted")),
        (5.0, _signal(100)),
        (5.0, _verdict("UNKNOWN")),
        (5.0, _started("insert_act")),
        (8.0, _verdict("UNKNOWN")),  # a second UNKNOWN does not move the start
        (8.0, _started("insert_act")),
        _finish("failed", steps=6, at=12.0),
    )

    summary = summarize_agent_run(events)

    assert summary is not None
    assert (summary.wasted_s, summary.wasted_steps) == (7.0, 2)


def test_nothing_is_wasted_on_a_success_or_without_an_unknown() -> None:
    won = _log((5.0, _signal(50)), (5.0, _verdict("UNKNOWN")), _finish("success", 120, 12.0))
    lost_known = _log((5.0, _signal(50)), (5.0, _verdict("POSE_ERROR")), _finish("failed", 9, 9.0))

    for events in (won, lost_known):
        summary = summarize_agent_run(events)
        assert summary is not None
        assert (summary.wasted_s, summary.wasted_steps) == (None, None)


def test_an_unknown_with_no_skill_after_it_wastes_time_but_no_step() -> None:
    events = _log(
        (0.0, _started("insert_scripted")),
        (0.5, _verdict("UNKNOWN")),
        _finish("aborted", steps=1, at=3.0),
    )

    summary = summarize_agent_run(events)

    assert summary is not None
    assert (summary.wasted_s, summary.wasted_steps) == (2.5, 0)


def test_planner_calls_decisions_rejections_errors_and_timeouts_are_counted() -> None:
    events = _log(
        (1.0, _verdict("UNKNOWN")),
        (1.0, _called(1)),
        (1.0, _decided("retry")),
        (2.0, _called(2)),
        (2.0, _rejected("skill: same_as_failed_skill", "kind: missing")),
        (3.0, _called(3)),
        (3.0, _rejected("<root>: planner_raised RateLimitError")),
        (4.0, _called(4)),
        (4.0, PlannerTimedOut(skill="insert_scripted", deadline_s=30.0, waited_s=30.1)),
        (5.0, _called(5)),
        (5.0, _decided("escalate")),
        _finish("aborted", steps=50, at=5.0),
    )

    summary = summarize_agent_run(events)

    assert summary is not None
    assert summary.planner_calls == 5
    assert summary.decisions == ("retry", "escalate")
    # Invalid output is an answer that failed validation; a raised call is an error, not output.
    assert (summary.invalid, summary.errors, summary.timeouts) == (1, 1, 1)


def test_calls_cut_short_by_the_monitor_are_counted_and_shown() -> None:
    force = MonitorVerdict(skill="insert_scripted", failure_class="FORCE_LIMIT", rule="r")
    cut = PlannerPreempted(
        skill="insert_scripted", failure_class="FORCE_LIMIT", waited_s=0.2, waited_sim_s=0.7
    )
    events = _log(
        (1.0, _verdict("UNKNOWN")),
        (1.0, _called(1)),
        (1.7, force),
        (1.7, cut),
        _finish("failed", steps=3, at=2.0),
    )

    summary = summarize_agent_run(events)

    assert summary is not None
    assert (summary.planner_calls, summary.preempted, summary.timeouts) == (1, 1, 0)
    lines = render_agent_report(variant_rows([("C-llm", [events])]))
    header = lines[0].split()
    row = next(line for line in lines if line.startswith("C-llm ")).split()
    assert row[header.index("preempted")] == "1"


def test_an_answer_refused_as_stale_is_not_counted_as_invalid() -> None:
    # Valid, but the skill it chose could no longer start when it arrived (ADR 020).
    events = _log(
        (1.0, _verdict("UNKNOWN")),
        (1.0, _called(1)),
        (1.5, _rejected("skill: preconditions_not_met")),
        _finish("aborted", steps=3, at=1.5),
    )

    summary = summarize_agent_run(events)

    assert summary is not None
    assert (summary.invalid, summary.stale, summary.errors) == (0, 1, 0)
    lines = render_agent_report(variant_rows([("C-llm", [events])]))
    header = lines[0].split()
    row = next(line for line in lines if line.startswith("C-llm ")).split()
    assert (row[header.index("invalid")], row[header.index("stale")]) == ("0.0%", "1")


def test_latency_and_tokens_come_from_the_traced_calls() -> None:
    events = _log(
        (1.0, _called(1)),
        (1.0, _responded(8.0, (1000, 500))),
        (1.0, _decided("switch_skill")),
        (2.0, _called(2)),
        (2.0, _responded(2.0, (None, 100))),  # the host sent no prompt count
        (2.0, _decided("abort")),
        _finish("aborted", steps=20, at=2.0),
    )

    summary = summarize_agent_run(events)

    assert summary is not None
    assert summary.latencies_s == (8.0, 2.0)
    assert (summary.prompt_tokens, summary.completion_tokens) == ((1000,), (500, 100))


def test_an_unfinished_run_has_no_summary() -> None:
    assert summarize_agent_run(_log((1.0, _signal(1)))) is None
    assert summarize_agent_run([]) is None


# --- percentiles ----------------------------------------------------------------------------


def test_percentiles_are_nearest_rank() -> None:
    values = [9.0, 1.0, 5.0, 3.0, 7.0, 2.0, 8.0, 4.0, 6.0, 10.0]

    assert percentile(values, 50) == 5.0
    assert percentile(values, 95) == 10.0
    assert percentile([4.0], 95) == 4.0
    assert percentile([], 50) is None
    for bad in (0, 101, -5):
        with pytest.raises(ValueError):
            percentile(values, bad)


# --- labels ---------------------------------------------------------------------------------


def test_a_label_names_a_variant_and_optionally_a_repeat() -> None:
    assert parse_label("C-llm#2") == ("C-llm", 2)
    assert parse_label("B") == ("B", 1)
    assert parse_label("C#9999") == ("C", 9999)
    for bad in ("", "#1", "C-llm#0", "C-llm#x", "a b", "x" * 41, "C#1#2", "C#10000"):
        with pytest.raises(StoreRefused):
            parse_label(bad)


# --- variants -------------------------------------------------------------------------------


def _ok(seed: int, **kwargs: object) -> list[Event]:
    return _log(_finish("success", 10, 1.0), seed=seed, **kwargs)  # type: ignore[arg-type]


def _failed(seed: int, **kwargs: object) -> list[Event]:
    return _log(
        (0.0, _started("insert_scripted")),
        (1.0, _verdict("UNKNOWN")),
        (1.0, _called(1)),
        (1.0, _decided("retry")),
        (1.0, _started("insert_scripted")),
        (2.0, _verdict("UNKNOWN")),
        (2.0, _called(2)),
        (2.0, _decided("abort")),
        _finish("aborted", 2, 3.0),
        seed=seed,
        **kwargs,  # type: ignore[arg-type]
    )


def test_repeats_of_a_variant_are_pooled_and_their_spread_shown() -> None:
    rows = variant_rows(
        [
            ("C-llm#1", [_ok(0), _ok(1)]),
            ("B", [_failed(0, setting="B"), _failed(1, setting="B")]),
            ("C-llm#2", [_ok(0), _failed(1)]),
        ]
    )

    assert [r.variant for r in rows] == ["C-llm", "B"]  # first appearance first
    llm, b = rows
    assert (llm.repeats, llm.runs, llm.success_rate) == (2, 4, 0.75)
    assert llm.success_spread == (0.5, 1.0)
    assert (b.repeats, b.success_spread) == (1, None)
    assert (b.wasted_s, b.wasted_steps) == (2.0, 1.0)
    assert b.decisions == {"retry": 2, "abort": 2}
    assert b.planner_calls_per_run == 2.0


def test_a_variant_row_aggregates_calls_and_costs() -> None:
    run = _log(
        (1.0, _called(1)),
        (1.0, _responded(8.0, (1000, 500))),
        (1.0, _decided("switch_skill")),
        (2.0, _called(2)),
        (2.0, _rejected("kind: missing")),
        _finish("failed", 20, 2.0),
    )
    other = _log(
        (1.0, _called(1)),
        (1.0, _responded(2.0, (600, 300))),
        (1.0, _decided("abort")),
        _finish("aborted", 20, 1.0),
        seed=1,
    )

    [row] = variant_rows([("C-llm", [run, other])])

    assert row.planner_calls_per_run == 1.5
    assert row.invalid_rate == pytest.approx(1 / 3)
    assert (row.errors, row.timeouts) == (0, 0)
    assert (row.latency_p50_s, row.latency_p95_s) == (2.0, 8.0)
    assert (row.prompt_tokens_per_call, row.completion_tokens_per_call) == (800.0, 400.0)


def test_a_variant_without_planner_calls_shows_no_rates() -> None:
    [row] = variant_rows([("B", [_ok(0, setting="B")])])

    assert row.planner_calls_per_run == 0.0
    assert (row.invalid_rate, row.latency_p50_s, row.prompt_tokens_per_call) == (None, None, None)
    assert (row.wasted_s, row.wasted_steps) == (None, None)


def test_the_first_finished_attempt_of_a_unit_counts() -> None:
    first = _log(_finish("failed", 5, 1.0), attempt=2)
    later = _log(_finish("success", 5, 1.0), attempt=3)
    interrupted = _log((1.0, _signal(1)), attempt=1)

    [row] = variant_rows([("C-rule", [interrupted, later, first])])

    assert (row.runs, row.success_rate) == (1, 0.0)


def test_a_store_must_hold_one_setting_and_one_configuration() -> None:
    with pytest.raises(StoreRefused, match="one setting"):
        variant_rows([("mixed", [_ok(0, setting="B"), _ok(1, setting="C")])])
    with pytest.raises(StoreRefused, match="one configuration"):
        variant_rows([("mixed", [_ok(0, config_hash="a"), _ok(1, config_hash="b")])])
    with pytest.raises(StoreRefused, match="no finished runs"):
        variant_rows([("empty", [])])
    with pytest.raises(StoreRefused, match="twice"):
        variant_rows([("C#1", [_ok(0)]), ("C#1", [_ok(0)])])


def test_the_configuration_check_ignores_the_scenario_part() -> None:
    # The config hash covers the scenario, so an eval's store holds one hash per scenario.
    def parts(scenario: str, planner: str = "p") -> dict[str, str]:
        return {"scenario": scenario, "planner": planner, "code": "c"}

    sfp = _ok(0, scenario="yaw_mismatch", config_hash="1", config_parts=parts("y"))
    port = _ok(0, scenario="damaged_port", config_hash="2", config_parts=parts("d"))
    [row] = variant_rows([("C-llm", [sfp, port])])
    assert row.runs == 2

    other = _ok(1, scenario="damaged_port", config_hash="3", config_parts=parts("d", "q"))
    with pytest.raises(StoreRefused, match="one configuration"):
        variant_rows([("C-llm", [sfp, other])])


def test_two_configurations_in_one_store_are_refused_even_when_a_unit_ran_under_both() -> None:
    # The report keeps a unit's first finished attempt; a later eval under another planner, run
    # into the same store, must not hide behind it.
    first = _ok(0, attempt=1, config_parts={"scenario": "s", "planner": "rule"})
    later = _ok(0, attempt=2, config_parts={"scenario": "s", "planner": "fixed"})
    with pytest.raises(StoreRefused, match="one configuration"):
        variant_rows([("C-fixed", [first, later])])


def test_repeats_of_a_variant_must_share_the_planner() -> None:
    rule = _ok(0, config_parts={"scenario": "s", "planner": "rule", "code": "c1"})
    fixed = _ok(0, config_parts={"scenario": "s", "planner": "fixed", "code": "c1"})
    other_code = _ok(0, config_parts={"scenario": "s", "planner": "rule", "code": "c2"})

    [row] = variant_rows([("C#1", [rule]), ("C#2", [other_code])])  # a new commit is fine
    assert row.repeats == 2
    with pytest.raises(StoreRefused, match="planner"):
        variant_rows([("C#1", [rule]), ("C#2", [fixed])])


def test_repeats_of_a_variant_must_share_the_setting() -> None:
    with pytest.raises(StoreRefused, match="setting"):
        variant_rows([("C#1", [_ok(0)]), ("C#2", [_ok(0, setting="B")])])


# --- rendering ------------------------------------------------------------------------------


def test_the_report_shows_one_row_per_variant_and_dashes_for_nothing_to_measure() -> None:
    rows = variant_rows(
        [("B", [_failed(0, setting="B")]), ("C-llm#1", [_ok(0)]), ("C-llm#2", [_failed(0)])]
    )

    lines = render_agent_report(rows)

    header = lines[0].split()
    assert header[:4] == ["variant", "setting", "runs", "success"]
    b_line = next(line for line in lines if line.startswith("B "))
    llm_line = next(line for line in lines if line.startswith("C-llm "))
    assert "100.0%" not in b_line and "0.0%" in b_line
    assert "50.0% (0.0-100.0%)" in llm_line  # pooled, with the spread over repeats
    assert " - " in b_line  # no latency for a planner without a trace
    assert any(line.startswith("decisions") for line in lines)
    assert any("abort 1" in line for line in lines)


# --- the command ----------------------------------------------------------------------------


def _store(path: Path, setting: str, planner: object | None = None) -> None:
    scenarios = load_scenarios(PHASE3)
    with EventStore(path) as store:
        for scenario in scenarios:
            run_unit(scenario, setting, 0, store, planner=planner)  # type: ignore[arg-type]


def test_wasted_steps_of_real_runs_lie_within_their_steps(tmp_path: Path) -> None:
    # Skill steps against skill steps: a count in other units (world steps) once went negative.
    _store(tmp_path / "fixed.sqlite", "C", FixedPlanner())
    with EventStore(tmp_path / "fixed.sqlite") as store:
        summaries = [summarize_agent_run(store.events(r.run_id)) for r in store.runs()]
        finished = {r.run_id: store.events(r.run_id)[-1].payload for r in store.runs()}

    wasted = [s for s in summaries if s is not None and s.wasted_steps is not None]
    assert wasted  # damaged_port fails after its UNKNOWN in every attempt
    for s in wasted:
        total = finished[s.run_id]
        assert isinstance(total, RunFinished)
        assert s.wasted_steps is not None and 0 <= s.wasted_steps <= total.steps


def _cellrt(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "cellrt", *args],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
        cwd=REPO,
    )


def test_agent_report_reads_one_store_per_variant(tmp_path: Path) -> None:
    _store(tmp_path / "b.sqlite", "B")
    _store(tmp_path / "fixed.sqlite", "C", FixedPlanner())

    done = _cellrt("agent-report", "--db", f"B={tmp_path / 'b.sqlite'}",
                   "--db", f"C-fixed={tmp_path / 'fixed.sqlite'}")  # fmt: skip

    assert done.returncode == 0, done.stderr
    lines = done.stdout.splitlines()
    assert [line.split()[0] for line in lines[1:3]] == ["B", "C-fixed"]
    assert next(line for line in lines if line.startswith("C-fixed")).split()[2] == "3"


def test_agent_report_refuses_a_missing_store_without_creating_it(tmp_path: Path) -> None:
    missing = tmp_path / "missing.sqlite"

    done = _cellrt("agent-report", "--db", f"B={missing}")

    assert done.returncode == 1
    assert done.stderr == f"B: no event store at {missing}\n"
    assert not missing.exists()


def test_agent_report_refuses_a_bad_label(tmp_path: Path) -> None:
    for arg in ("no-equals-sign", "=x.sqlite", "a b=x.sqlite"):
        done = _cellrt("agent-report", "--db", arg)
        assert done.returncode == 1
        assert done.stderr.startswith("--db takes LABEL=PATH")


def test_agent_run_summary_is_plain_data() -> None:
    summary = summarize_agent_run(_failed(0))
    assert isinstance(summary, AgentRunSummary)
    assert summary.outcome == "aborted" and summary.setting == "C"


def test_agent_report_refuses_the_same_store_twice(tmp_path: Path) -> None:
    _store(tmp_path / "c.sqlite", "C", FixedPlanner())
    store = tmp_path / "c.sqlite"

    done = _cellrt("agent-report", "--db", f"C#1={store}", "--db", f"C#2={store}")

    assert done.returncode == 1
    assert done.stderr == f"C#2: the same event store as C#1 ({store})\n"


def test_agent_report_names_a_store_it_cannot_read_without_its_text(tmp_path: Path) -> None:
    junk = tmp_path / "junk.sqlite"
    junk.write_text("not a database, SECRET-USER-TEXT", encoding="utf-8")

    done = _cellrt("agent-report", "--db", f"B={junk}")

    assert done.returncode == 1
    assert done.stderr == f"B: cannot read the event store at {junk} (DatabaseError)\n"
