"""Replaying the Monitor over stored runs (ADR 022, gate 0): before any candidate rule is
judged, the replay has to reproduce every verdict the live run recorded."""

from __future__ import annotations

from pathlib import Path

import pytest

from cellrt.eval.runner import run_eval, run_unit
from cellrt.eval.scenarios import load_scenarios
from cellrt.monitor.classifier import MonitorConfig
from cellrt.monitor.promoted import PromotedRule
from cellrt.promotion.replay import ReplayConfig, replay_run
from cellrt.runtime.events import (
    SCHEMA_VERSION,
    Event,
    MonitorVerdict,
    Payload,
    PlannerCalled,
    PlannerDecided,
    PlannerPreempted,
    RunFinished,
    RunStarted,
    SignalSampled,
    SkillCancelled,
    SkillFinished,
    SkillStarted,
)
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task

SCENARIOS = Path(__file__).resolve().parents[1] / "eval" / "scenarios"
SEEDS = 3
PERIOD_S = 0.05
# The mock Monitor's stall window is 1.0 s: its first verdict needs 21 samples of no progress.
STALL_SAMPLES = 21
STALL_EVIDENCE = {
    "progress_mm": 0.0,
    "window_s": 1.0,
    "force_n": 3.0,
    "torque_nm": 0.0,
    "xy_error_mm": 0.0,
}


def _sample(step: int, *, valid: bool = True) -> SignalSampled:
    """A plug that does not move: 3 N straight down, no torque, no XY error."""
    return SignalSampled(
        step=step,
        height_mm=-2.0,
        depth_mm=2.0,
        xy_offset_mm=(0.0, 0.0),
        xy_error_mm=0.0,
        raw_force_n=(0.0, 0.0, 3.0),
        tare_offset_n=(0.0, 0.0, 0.0),
        force_n=3.0,
        torque_nm=0.0,
        wrench_valid=valid,
    )


def _log(payloads: list[Payload], *, time_limit: float = 30.0) -> list[Event]:
    """A run: `run_started`, then `payloads` in order (a sample one period after the one before;
    anything else at the time of the last sample), then `run_finished`."""
    started = RunStarted(
        scenario_id="hand",
        setting="A",
        seed=0,
        attempt=1,
        task=Task(time_limit=time_limit),
        config_hash="x",
        config_parts=dict(ReplayConfig.mock().parts),
        schema_version=SCHEMA_VERSION,
    )
    timed: list[tuple[float, Payload]] = [(0.0, started)]
    t = 0.0
    for payload in payloads:
        if isinstance(payload, SignalSampled):
            t = round(t + PERIOD_S, 6)
        timed.append((t, payload))
    timed.append((t, RunFinished(outcome="failed", reason="x", steps=1, duration=t)))
    return [
        Event(run_id="hand/A/0/a1", seq=i, ts=ts, wall_ts=0.0, payload=p)
        for i, (ts, p) in enumerate(timed, start=1)
    ]


def _stall(skill: str = "insert_scripted", attempt: int = 1) -> list[Payload]:
    """`skill` starts and stalls for STALL_SAMPLES samples."""
    return [
        SkillStarted(skill=skill, attempt=attempt),
        *[_sample(k) for k in range(1, STALL_SAMPLES + 1)],
    ]


def _stalled_run(time_limit: float, after: list[Payload]) -> list[Event]:
    """insert_scripted stalls for STALL_SAMPLES samples, then `after` happens in the next tick."""
    return _log([*_stall(), *after], time_limit=time_limit)


STALL_VERDICT = MonitorVerdict(
    skill="insert_scripted", failure_class="UNKNOWN", rule="insert_stalled", evidence=STALL_EVIDENCE
)
STOPPED = SkillCancelled(skill="insert_scripted", reason="terminated")
PREEMPTED = SkillCancelled(skill="insert_scripted", reason="preempted")
CALLED = PlannerCalled(skill="insert_scripted", failure_class="UNKNOWN", call=1)


@pytest.fixture(scope="module")
def stores(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    """Stores the mock eval leaves for phase 0 and phase 3, settings A, B and C."""
    out: dict[str, Path] = {}
    for name in ("phase0", "phase3"):
        path = tmp_path_factory.mktemp(name) / "store.sqlite"
        with EventStore(path) as store:
            run_eval(load_scenarios(SCENARIOS / f"{name}.yaml"), ["A", "B", "C"], SEEDS, store)
        out[name] = path
    return out


def _runs(path: Path) -> list[list[Event]]:
    with EventStore(path) as store:
        return [store.events(run.run_id) for run in store.runs()]


@pytest.mark.parametrize("name", ["phase0", "phase3"])
def test_replay_reproduces_every_recorded_verdict(stores: dict[str, Path], name: str) -> None:
    config = ReplayConfig.mock()
    recorded = reproduced = 0
    for events in _runs(stores[name]):
        result = replay_run(events, config)
        assert result.status == "ok", (result.run_id, result.mismatch)
        recorded += sum(
            1
            for e in events
            if e.type == "monitor_verdict" and e.payload.rule != "skill_timeout"  # type: ignore[union-attr]
        )
        reproduced += sum(1 for j in result.judged if j.outcome == "recorded")
    assert recorded > 0
    assert reproduced == recorded


def test_a_stalled_sample_the_run_judged_is_reproduced() -> None:
    events = _stalled_run(time_limit=30.0, after=[STALL_VERDICT, STOPPED])
    result = replay_run(events, ReplayConfig.mock())
    assert result.status == "ok"
    [judged] = result.judged
    assert (judged.step, judged.skill, judged.attempt) == (STALL_SAMPLES, "insert_scripted", 1)
    assert judged.verdict.rule == "insert_stalled"
    assert judged.outcome == "recorded"


def test_the_sample_that_reaches_the_time_limit_is_never_judged() -> None:
    # The loop stops right after logging it: no tick follows, so no verdict on it is recorded.
    events = _stalled_run(time_limit=STALL_SAMPLES * PERIOD_S, after=[STOPPED])
    result = replay_run(events, ReplayConfig.mock())
    assert result.status == "ok"
    assert result.judged == ()


def test_a_verdict_the_run_did_not_record_is_a_mismatch() -> None:
    events = _stalled_run(time_limit=30.0, after=[STOPPED])
    result = replay_run(events, ReplayConfig.mock())
    assert result.status == "mismatch"
    assert result.mismatch is not None
    assert result.mismatch.recorded == "none"
    assert result.mismatch.replayed == "UNKNOWN/insert_stalled"
    assert result.mismatch.seq == events[2 + STALL_SAMPLES - 1].seq  # the last sample


def test_a_doctored_verdict_is_a_mismatch_at_that_event() -> None:
    doctored = STALL_VERDICT.model_copy(update={"rule": "insert_jammed"})
    events = _stalled_run(time_limit=30.0, after=[doctored, STOPPED])
    result = replay_run(events, ReplayConfig.mock())
    assert result.status == "mismatch"
    assert result.mismatch is not None
    assert result.mismatch.seq == events[2 + STALL_SAMPLES].seq  # the recorded verdict
    assert result.mismatch.recorded == "UNKNOWN/insert_jammed"


def test_a_verdict_with_other_evidence_is_a_mismatch_that_says_so() -> None:
    doctored = STALL_VERDICT.model_copy(update={"evidence": {**STALL_EVIDENCE, "force_n": 3.5}})
    events = _stalled_run(time_limit=30.0, after=[doctored, STOPPED])
    result = replay_run(events, ReplayConfig.mock())
    assert result.status == "mismatch"
    assert result.mismatch is not None
    assert result.mismatch.recorded == "UNKNOWN/insert_stalled"
    assert result.mismatch.replayed == "UNKNOWN/insert_stalled (other evidence)"


def test_a_run_made_with_other_thresholds_is_not_replayed(tmp_path: Path) -> None:
    [scenario] = [s for s in load_scenarios(SCENARIOS / "phase3.yaml") if s.id == "damaged_port"]
    with EventStore(tmp_path / "s.sqlite") as store:
        run = run_unit(scenario, "A", 0, store, monitor=MonitorConfig(stall_window_s=0.8))
        events = store.events(run.run_id)
    result = replay_run(events, ReplayConfig.mock())
    assert (result.status, result.detail) == ("config_mismatch", "monitor")
    # With the configuration the run used, it replays.
    config = ReplayConfig.mock(monitor=MonitorConfig(stall_window_s=0.8))
    assert replay_run(events, config).status == "ok"


def test_an_unfinished_run_is_not_replayed() -> None:
    events = _stalled_run(time_limit=30.0, after=[STALL_VERDICT, STOPPED])[:-1]
    assert replay_run(events, ReplayConfig.mock()).status == "unfinished"
    assert replay_run([], ReplayConfig.mock()).status == "unfinished"


def test_a_log_without_xy_offsets_cannot_be_judged() -> None:
    events = _stalled_run(time_limit=30.0, after=[STALL_VERDICT, STOPPED])
    old = [
        e.model_copy(update={"payload": e.payload.model_copy(update={"xy_offset_mm": None})})
        if isinstance(e.payload, SignalSampled)
        else e
        for e in events
    ]
    assert replay_run(old, ReplayConfig.mock()).status == "unjudgeable"


def test_a_verdict_that_lost_to_the_skills_own_success_is_accounted_for() -> None:
    # The skill reports done on the tick the stall rule matches: a non-safety verdict loses.
    events = _stalled_run(time_limit=30.0, after=[SkillFinished(skill="insert_scripted", ok=True)])
    result = replay_run(events, ReplayConfig.mock())
    assert result.status == "ok"
    [judged] = result.judged
    assert judged.outcome == "lost_to_success"


def test_another_skills_success_does_not_explain_away_a_verdict() -> None:
    events = _stalled_run(time_limit=30.0, after=[SkillFinished(skill="insert_act", ok=True)])
    assert replay_run(events, ReplayConfig.mock()).status == "mismatch"


def _switch_after_a_stall(window: list[Payload], end: list[Payload]) -> list[Event]:
    """insert_scripted stalls, the planner is called, `window` happens while it is open, then
    `end`; the samples are logged as the world stepped."""
    return _log([*_stall(), STALL_VERDICT, PREEMPTED, CALLED, *window, *end])


DECIDED = PlannerDecided(
    skill="insert_scripted",
    decision={"kind": "switch_skill", "skill": "insert_act"},
    waited_sim_s=0.05,
)
DROPOUT = MonitorVerdict(
    skill="insert_scripted",
    failure_class="SENSOR_DROPOUT",
    rule="wrench_invalid",
    evidence={"invalid_s": 0.2},
)


def test_a_frozen_wait_judges_no_sample_while_the_planner_answers() -> None:
    # The rule planner answers in its first step, so the world steps once with the call open.
    # The run's Monitor never judged that sample: with it, the dropout window (5 samples) would
    # fill one sample early and call a dropout insert_act never saw.
    events = _switch_after_a_stall(
        window=[_sample(22, valid=False), DECIDED],
        end=[
            SkillStarted(skill="insert_act", attempt=1),
            *[_sample(k, valid=False) for k in range(23, 27)],
            *[_sample(k) for k in range(27, 30)],
            SkillFinished(skill="insert_act", ok=True),
        ],
    )
    result = replay_run(events, ReplayConfig.mock())
    assert result.status == "ok", result.mismatch
    assert [j.verdict.rule for j in result.judged] == ["insert_stalled"]


def test_a_live_wait_judges_the_hold_and_stops_when_the_call_ends() -> None:
    # Live (ADR 020): the hold is judged while the call runs; a dropout preempts the call.
    # Samples after that belong to no skill and no call, so they are not judged.
    events = _switch_after_a_stall(
        window=[
            *[_sample(k, valid=False) for k in range(22, 27)],
            DROPOUT,
            PlannerPreempted(
                skill="insert_scripted",
                failure_class="SENSOR_DROPOUT",
                waited_s=1.0,
                waited_sim_s=0.25,
            ),
        ],
        end=[_sample(k, valid=False) for k in range(27, 33)],
    )
    result = replay_run(events, ReplayConfig.mock(planner_wait="live"))
    assert result.status == "ok", result.mismatch
    hold = result.judged[-1]
    assert (hold.skill, hold.attempt, hold.verdict.rule) == (
        "insert_scripted",
        None,
        "wrench_invalid",
    )
    assert len(result.judged) == 2
    # The same log read as frozen: the hold verdict has no sample behind it.
    assert replay_run(events, ReplayConfig.mock()).status == "mismatch"


TIMEOUT_EVIDENCE = {"elapsed_s": 5.0, "timeout_s": 5.0}


@pytest.mark.parametrize("skill, status", [("insert_scripted", "ok"), ("verify", "mismatch")])
def test_a_skill_timeout_is_accepted_only_for_the_skill_being_watched(
    skill: str, status: str
) -> None:
    timeout = MonitorVerdict(
        skill=skill, failure_class="TIMEOUT", rule="skill_timeout", evidence=TIMEOUT_EVIDENCE
    )
    events = _log(
        [
            SkillStarted(skill="insert_scripted", attempt=1),
            *[_sample(k) for k in range(1, 4)],
            timeout,
            SkillCancelled(skill="insert_scripted", reason="timeout"),
        ]
    )
    result = replay_run(events, ReplayConfig.mock())
    assert result.status == status
    if status == "ok":
        assert result.timeouts == 1


def test_two_verdicts_in_one_tick_are_a_mismatch() -> None:
    events = _stalled_run(time_limit=30.0, after=[STALL_VERDICT, STALL_VERDICT, PREEMPTED])
    result = replay_run(events, ReplayConfig.mock())
    assert result.status == "mismatch"
    assert result.mismatch is not None
    assert result.mismatch.seq == events[2 + STALL_SAMPLES + 1].seq


def test_each_skill_attempt_is_its_own_window() -> None:
    retry = SkillStarted(skill="insert_scripted", attempt=2)
    again = [retry, *[_sample(k) for k in range(STALL_SAMPLES + 1, 2 * STALL_SAMPLES + 1)]]
    events = _stalled_run(30.0, [STALL_VERDICT, PREEMPTED, *again, STALL_VERDICT, PREEMPTED])
    result = replay_run(events, ReplayConfig.mock())
    assert result.status == "ok"
    assert [(j.attempt, j.window) for j in result.judged] == [(1, 0), (2, 1)]


# With a candidate: each window's first verdict and one lost to success are kept, nothing is
# compared.

# Fires on a plug pressing at 3 N without moving, once 0.2 s of samples (5) are held.
PRESSING = PromotedRule.model_validate_json(
    '{"name": "PRESSING", "skill": "insert_scripted", "window_s": 0.2, "conditions": ['
    '{"field": "force_n", "op": ">", "value": 2.0},'
    ' {"field": "progress_mm", "op": "<", "value": 0.2}]}'
)


@pytest.mark.parametrize(
    "samples, outcomes",
    [
        (5, ["lost_to_success"]),
        # The 5th and the 6th match; the skill succeeds in the tick that judges the 6th.
        (6, ["replayed", "lost_to_success"]),
        # The 5th to the 15th match: only the first of them is kept, then the lost one.
        (15, ["replayed", "lost_to_success"]),
    ],
)
def test_a_candidate_verdict_on_the_tick_the_skill_succeeds_is_lost(
    samples: int, outcomes: list[str]
) -> None:
    events = _log(
        [
            SkillStarted(skill="insert_scripted", attempt=1),
            *[_sample(k) for k in range(1, samples + 1)],
            SkillFinished(skill="insert_scripted", ok=True),
        ]
    )
    result = replay_run(events, ReplayConfig.mock(), candidate=PRESSING)
    assert [j.outcome for j in result.judged] == outcomes
    assert {j.verdict.rule for j in result.judged} == {"promoted:PRESSING"}
    assert replay_run(events, ReplayConfig.mock()).judged == ()  # without it: nothing


def test_a_candidate_that_matches_every_sample_keeps_one_verdict_per_window() -> None:
    # Two stalled attempts of 21 samples each: PRESSING matches from the 5th sample of each.
    retry = SkillStarted(skill="insert_scripted", attempt=2)
    again = [retry, *[_sample(k) for k in range(STALL_SAMPLES + 1, 2 * STALL_SAMPLES + 1)]]
    events = _stalled_run(30.0, [STALL_VERDICT, PREEMPTED, *again, STALL_VERDICT, PREEMPTED])
    result = replay_run(events, ReplayConfig.mock(), candidate=PRESSING)
    assert result.status == "ok"
    assert [(j.window, j.attempt, j.outcome) for j in result.judged] == [
        (0, 1, "replayed"),
        (1, 2, "replayed"),
    ]
    assert result.judged[0].step == 5
