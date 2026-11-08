"""Setting C: failures the Monitor classifies as UNKNOWN go to a planner. Its output is untrusted:
validated against a schema and the skill registry before anything runs."""

from __future__ import annotations

import asyncio
import gc
import logging
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from cellrt.eval.runner import run_unit
from cellrt.eval.scenarios import Scenario, load_scenarios
from cellrt.executor.recovery import RecoveryConfig
from cellrt.monitor.profiles import MonitorProfile
from cellrt.planner.base import FailureContext
from cellrt.planner.schemas import MAX_INPUT_CHARS, MAX_JSON_DEPTH, MAX_RAW_CHARS
from cellrt.runtime.events import Event, PlannerOutputRejected
from cellrt.runtime.state import RunState
from cellrt.runtime.store import EventStore
from cellrt.skills.base import SkillContext, SkillResult
from cellrt.skills.mock import Align, Approach, InsertAct, Retract, Verify, WaitSensor
from cellrt.skills.registry import SkillRegistry
from cellrt.world.mock_world import MockParams

PHASE0 = Path(__file__).resolve().parents[1] / "eval" / "scenarios" / "phase0.yaml"
SCENARIOS = {s.id: s for s in load_scenarios(PHASE0)}


@dataclass
class FakePlanner:
    """Scripted answers, in order; records what it was asked."""

    answers: list[object]
    asked: list[FailureContext] = field(default_factory=list)

    async def triage(self, ctx: FailureContext) -> object:
        self.asked.append(ctx)
        return self.answers.pop(0) if self.answers else {"kind": "abort", "reason": "no answer"}


def _run(
    store: EventStore,
    scenario: Scenario,
    setting: str,
    seed: int = 0,
    **kwargs: object,
) -> list[Event]:
    run = run_unit(scenario, setting, seed=seed, store=store, **kwargs)  # type: ignore[arg-type]
    return store.events(run.run_id)


def _types(events: list[Event]) -> list[str]:
    return [e.type for e in events]


@pytest.mark.parametrize("seed", range(8))
def test_a_yaw_mismatch_lost_in_b_is_won_in_c_by_switching_to_insert_act(
    store: EventStore, seed: int
) -> None:
    b = _run(store, SCENARIOS["yaw_mismatch"], "B", seed)
    c = _run(store, SCENARIOS["yaw_mismatch"], "C", seed)

    assert b[-1].payload.outcome == "failed"
    assert "planner_called" not in _types(b)
    assert c[-1].payload.outcome == "success"
    called = [e.payload for e in c if e.type == "planner_called"]
    decided = [e.payload for e in c if e.type == "planner_decided"]
    assert [(p.skill, p.failure_class, p.call) for p in called] == [
        ("insert_scripted", "UNKNOWN", 1)
    ]
    assert [p.decision for p in decided] == [{"kind": "switch_skill", "skill": "insert_act"}]
    recoveries = [
        (e.payload.failure_class, e.payload.action) for e in c if e.type == "recovery_started"
    ]
    assert recoveries == [("UNKNOWN", "switch_skill:insert_act")]
    assert "insert_act" in [e.payload.skill for e in c if e.type == "skill_started"]


def test_the_planner_is_told_the_failure_its_evidence_and_what_is_left(store: EventStore) -> None:
    planner = FakePlanner([{"kind": "switch_skill", "skill": "insert_act"}])

    _run(store, SCENARIOS["yaw_mismatch"], "C", planner=planner)

    [ctx] = planner.asked
    assert (ctx.skill, ctx.attempt, ctx.failure_class, ctx.rule) == (
        "insert_scripted",
        1,
        "UNKNOWN",
        "insert_stalled",
    )
    assert ctx.evidence["torque_nm"] > 0.3
    assert (ctx.planner_calls_left, ctx.recoveries_left) == (2, 4)
    assert 0 < ctx.time_left_s < SCENARIOS["yaw_mismatch"].task.time_limit
    assert ctx.catalogue["insert_act"] == "insertion"


class _HostileRepr:
    def __repr__(self) -> str:
        raise RuntimeError("secret-token-123")


def _padded_retry(length: int) -> str:
    """A valid retry as JSON text, padded with whitespace to `length` characters."""
    text = '{"kind": "retry"}'
    return text + " " * (length - len(text))


def _nested_list(depth: int) -> object:
    data: list[object] = []
    for _ in range(depth):
        data = [data]
    return data


MALFORMED: dict[str, object] = {
    "not json": "twist it a bit, then push",
    "unknown decision": {"kind": "teleport"},
    "unknown skill": {"kind": "switch_skill", "skill": "laser_cut"},
    "not an insert skill": {"kind": "switch_skill", "skill": "verify"},
    "the failed skill itself": {"kind": "switch_skill", "skill": "insert_scripted"},
    "abort without a reason": {"kind": "abort"},
    "extra field": {"kind": "retry", "speed": 99},
    "a skill name over 64 characters": {"kind": "switch_skill", "skill": "s" * 65},
    "a reason over 200 characters": {"kind": "abort", "reason": "r" * 201},
    "a reason with a line break": {"kind": "abort", "reason": "stop\nrun_finished success"},
    "text over the input limit": _padded_retry(MAX_INPUT_CHARS + 1),
    "JSON nested too deep to parse": "[" * 3000 + "]" * 3000,
    "a deeply nested object": _nested_list(3000),
    "an object whose repr raises": _HostileRepr(),
}


@pytest.mark.parametrize("case", MALFORMED)
def test_malformed_planner_output_is_rejected_and_aborts_the_run(
    store: EventStore, case: str
) -> None:
    events = _run(store, SCENARIOS["yaw_mismatch"], "C", planner=FakePlanner([MALFORMED[case]]))

    rejected = [e.payload for e in events if e.type == "planner_output_rejected"]
    assert len(rejected) == 1 and rejected[0].errors
    assert "planner_decided" not in _types(events)
    assert (events[-1].payload.outcome, events[-1].payload.reason) == (
        "aborted",
        "insert_scripted failed: UNKNOWN (planner output rejected)",
    )


def test_a_planner_abort_ends_the_run_with_its_reason(store: EventStore) -> None:
    planner = FakePlanner([{"kind": "abort", "reason": "torque pattern looks unsafe"}])

    events = _run(store, SCENARIOS["yaw_mismatch"], "C", planner=planner)

    assert (events[-1].payload.outcome, events[-1].payload.reason) == (
        "aborted",
        "insert_scripted failed: UNKNOWN (planner aborted: torque pattern looks unsafe)",
    )


def _rejected(events: list[Event]) -> list[PlannerOutputRejected]:
    return [e.payload for e in events if isinstance(e.payload, PlannerOutputRejected)]


def test_planner_text_is_parsed_up_to_the_input_limit_and_rejected_past_it(
    store: EventStore,
) -> None:
    at_limit = FakePlanner([_padded_retry(MAX_INPUT_CHARS)])
    past_limit = FakePlanner([_padded_retry(MAX_INPUT_CHARS + 1)])

    accepted = _run(store, SCENARIOS["yaw_mismatch"], "C", planner=at_limit)
    rejected = _run(store, SCENARIOS["yaw_mismatch"], "C", planner=past_limit)

    assert next(e.payload.decision for e in accepted if e.type == "planner_decided") == {
        "kind": "retry"
    }
    [r] = _rejected(rejected)
    assert r.errors == ["<root>: too_long"]
    assert len(r.raw) == MAX_RAW_CHARS  # the audit trail keeps only the start


def test_rejections_never_echo_what_the_planner_wrote(store: EventStore) -> None:
    secret = "sk-SECRETKEY-123 user@example.com"
    extras = {f"{secret}-{i}": i for i in range(5000)}
    planner = FakePlanner([{"kind": "retry", **extras}])

    events = _run(store, SCENARIOS["yaw_mismatch"], "C", planner=planner)

    [r] = _rejected(events)
    # Planner-made keys are not named, and 5000 of them make one code.
    assert r.errors == ["retry.<extra>: extra_forbidden"]
    hostile = _run(store, SCENARIOS["yaw_mismatch"], "C", planner=FakePlanner([_HostileRepr()]))
    [h] = _rejected(hostile)
    assert h.raw == "<_HostileRepr>"  # its own repr is never called
    assert "secret-token-123" not in str([e.payload for e in hostile])


def test_rejected_output_is_scrubbed_before_it_is_stored(store: EventStore) -> None:
    secret = "sk-SECRETKEY-1234567890"
    text = FakePlanner([f"cannot comply: key {secret} refused, ask bob@example.com"])
    huge = FakePlanner([{"kind": "retry", **{f"{secret}-{i}": i for i in range(5000)}}])

    [t] = _rejected(_run(store, SCENARIOS["yaw_mismatch"], "C", planner=text))
    [h] = _rejected(_run(store, SCENARIOS["yaw_mismatch"], "C", planner=huge))

    for r in (t, h):
        assert "SECRETKEY" not in r.raw and "bob@" not in r.raw
        assert 0 < len(r.raw) <= MAX_RAW_CHARS
    assert t.raw.startswith("cannot comply: key <redacted> refused")


def test_an_abort_reason_is_scrubbed_and_must_not_steer_the_terminal(store: EventStore) -> None:
    leaky = FakePlanner([{"kind": "abort", "reason": "key sk-SECRETKEY-1234567890 refused"}])
    bidi = FakePlanner([{"kind": "abort", "reason": "looks fine\u202edesrever"}])
    c1 = FakePlanner([{"kind": "abort", "reason": "csi\x9b2J"}])

    leaked = _run(store, SCENARIOS["yaw_mismatch"], "C", planner=leaky)

    assert "SECRETKEY" not in str([e.payload for e in leaked])
    assert leaked[-1].payload.reason.endswith("(planner aborted: key <redacted> refused)")
    for planner in (bidi, c1):
        [r] = _rejected(_run(store, SCENARIOS["yaw_mismatch"], "C", planner=planner))
        assert r.errors == ["abort.reason: string_pattern_mismatch"]


def test_the_audit_text_of_a_structure_is_bounded_and_the_same_on_every_run(
    store: EventStore,
) -> None:
    deep = FakePlanner([_nested_list(3000)])
    wide = FakePlanner([{"kind": "retry", **{f"k{i:02}": i for i in range(50)}}])
    other = FakePlanner([{"kind": "retry", "at": object(), "ok": None, "x": 1.5}])

    [d] = _rejected(_run(store, SCENARIOS["yaw_mismatch"], "C", planner=deep))
    [w] = _rejected(_run(store, SCENARIOS["yaw_mismatch"], "C", planner=wide))
    [o] = _rejected(_run(store, SCENARIOS["yaw_mismatch"], "C", planner=other))

    assert d.raw == "[[[[[...]]]]]"  # four levels, then "..."
    assert w.raw.count(":") == 20 and w.raw.endswith(", ...}")  # twenty entries, then "..."
    # Not plain data: the type name only, never an address that differs from run to run.
    assert o.raw == "{'at': <object>, 'kind': 'retry', 'ok': None, 'x': 1.5}"


def _retry_nested(depth: int) -> str:
    """A retry with an extra field nested so the whole text is `depth` brackets deep."""
    inner = "[" * (depth - 1) + "]" * (depth - 1)
    return '{"kind": "retry", "x": ' + inner + "}"


def test_json_is_parsed_up_to_the_depth_limit_and_rejected_past_it(store: EventStore) -> None:
    at_limit = FakePlanner([_retry_nested(MAX_JSON_DEPTH)])
    past_limit = FakePlanner([_retry_nested(MAX_JSON_DEPTH + 1)])
    # Brackets inside a string are text, not nesting.
    in_string = FakePlanner(['{"kind": "abort", "reason": "' + "[{" * 50 + '"}'])
    # An escaped quote does not end the string, so the brackets after it are still text.
    escaped = FakePlanner(['{"kind": "abort", "reason": "a\\"' + "[" * 20 + '"}'])

    [parsed] = _rejected(_run(store, SCENARIOS["yaw_mismatch"], "C", planner=at_limit))
    [deep] = _rejected(_run(store, SCENARIOS["yaw_mismatch"], "C", planner=past_limit))
    text = _run(store, SCENARIOS["yaw_mismatch"], "C", planner=in_string)
    after_escape = _run(store, SCENARIOS["yaw_mismatch"], "C", planner=escaped)

    assert parsed.errors == ["retry.<extra>: extra_forbidden"]
    assert deep.errors == ["<root>: too_deep"]
    assert text[-1].payload.reason.startswith(
        "insert_scripted failed: UNKNOWN (planner aborted: [{"
    )
    assert after_escape[-1].payload.reason.startswith(
        'insert_scripted failed: UNKNOWN (planner aborted: a"[['
    )


@dataclass
class _Raises:
    async def triage(self, ctx: FailureContext) -> object:
        raise ValueError("upstream said: api key sk-123 is invalid")


@dataclass
class _CancelsItself:
    async def triage(self, ctx: FailureContext) -> object:
        raise asyncio.CancelledError


def test_a_planner_that_cancels_itself_is_rejected_and_aborts_the_run(store: EventStore) -> None:
    events = _run(store, SCENARIOS["yaw_mismatch"], "C", planner=_CancelsItself())

    [r] = _rejected(events)
    assert r.errors == ["<root>: planner_raised CancelledError"]
    assert events[-1].payload.outcome == "aborted"


def test_a_planner_that_raises_is_recorded_by_type_and_aborts_the_run(store: EventStore) -> None:
    events = _run(store, SCENARIOS["yaw_mismatch"], "C", planner=_Raises())

    [r] = _rejected(events)
    assert r.errors == ["<root>: planner_raised ValueError"]
    assert "sk-123" not in str([e.payload for e in events])
    assert events[-1].payload.outcome == "aborted"


def test_a_planner_error_left_unread_at_the_time_limit_is_never_printed(
    store: EventStore, caplog: pytest.LogCaptureFixture
) -> None:
    yaw = SCENARIOS["yaw_mismatch"]
    first = _run(store, yaw, "C", planner=_Raises())
    asked_at = next(e.ts for e in first if e.type == "planner_called")
    # The time limit lands on the step in which the planner raises: the run is stopped before
    # the executor reads the planner's result.
    limit = asked_at + MockParams().dt
    short = yaw.model_copy(update={"task": yaw.task.model_copy(update={"time_limit": limit})})

    with caplog.at_level(logging.ERROR, logger="asyncio"):
        events = _run(store, short, "C", planner=_Raises())
        gc.collect()

    assert (events[-1].payload.outcome, events[-1].payload.reason) == (
        "aborted",
        "time limit reached",
    )
    assert "planner_called" in _types(events) and not _rejected(events)
    assert "never retrieved" not in caplog.text
    assert "sk-123" not in caplog.text


def test_a_planner_budget_of_zero_never_calls_the_planner(store: EventStore) -> None:
    planner = FakePlanner([{"kind": "switch_skill", "skill": "insert_act"}])

    events = _run(
        store,
        SCENARIOS["yaw_mismatch"],
        "C",
        planner=planner,
        recovery=RecoveryConfig(planner_budget=0),
    )

    assert planner.asked == []
    assert events[-1].payload.reason == "insert_scripted failed: UNKNOWN (planner budget used up)"


def test_planner_decisions_count_against_the_task_recovery_budget(store: EventStore) -> None:
    planner = FakePlanner([{"kind": "retry"}] * 5)

    events = _run(
        store,
        SCENARIOS["yaw_mismatch"],
        "C",
        planner=planner,
        recovery=RecoveryConfig(planner_budget=9, total_budget=1),
    )

    assert len(planner.asked) == 1
    assert (events[-1].payload.outcome, events[-1].payload.reason) == (
        "failed",
        "insert_scripted failed: UNKNOWN (task recovery budget used up)",
    )


YAW_THEN_TIGHT = Scenario.model_validate(
    {
        "id": "yaw_then_tight",
        "faults": [
            {
                "kind": "yaw_mismatch",
                "stall_depth_mm": [3.0, 3.0],
                "force_n": [7.0, 7.0],
                "torque_nm": [0.6, 0.6],
                "yaw_deg": [10.0, 10.0],
            },
            # Deeper than the stall, and tight enough that insert_act at full speed is over 20 N.
            {"kind": "tight_fit", "onset_depth_mm": [6.0, 6.0], "force_n_per_mm_s": [15.0, 15.0]},
        ],
    }
)


def test_a_skill_switched_to_is_rerun_slow_after_a_force_limit(store: EventStore) -> None:
    events = _run(store, YAW_THEN_TIGHT, "C")

    recoveries = [
        (e.payload.skill, e.payload.action) for e in events if e.type == "recovery_started"
    ]
    assert recoveries == [
        ("insert_scripted", "switch_skill:insert_act"),
        ("insert_act", "retract_slow"),
    ]
    act_params = [
        e.payload.params
        for e in events
        if e.type == "skill_started" and e.payload.skill == "insert_act"
    ]
    assert act_params == [{}, {"speed_scale": 0.5}]
    assert events[-1].payload.outcome == "success"


def test_a_planner_is_accepted_only_in_setting_c(store: EventStore) -> None:
    with pytest.raises(ValueError, match="only in setting C"):
        run_unit(SCENARIOS["baseline"], "B", seed=0, store=store, planner=FakePlanner([]))


def _config_hash(events: list[Event]) -> str:
    return events[0].payload.config_hash


def test_the_planner_budget_counts_in_the_config_hash_of_c_only(store: EventStore) -> None:
    more = RecoveryConfig(planner_budget=5)
    yaw = SCENARIOS["yaw_mismatch"]

    b, b_more = _run(store, yaw, "B"), _run(store, yaw, "B", recovery=more)
    c, c_more = _run(store, yaw, "C"), _run(store, yaw, "C", recovery=more)

    assert _config_hash(b) == _config_hash(b_more)
    assert _config_hash(c) != _config_hash(c_more)


def test_planner_calls_are_bounded_per_task(store: EventStore) -> None:
    planner = FakePlanner([{"kind": "retry"}] * 5)  # retrying insert_scripted never helps

    events = _run(store, SCENARIOS["yaw_mismatch"], "C", planner=planner)

    assert len(planner.asked) == 2
    assert [e.payload.call for e in events if e.type == "planner_called"] == [1, 2]
    assert (events[-1].payload.outcome, events[-1].payload.reason) == (
        "failed",
        "insert_scripted failed: UNKNOWN (planner budget used up)",
    )


@dataclass
class _GivesUpAsUnknown:
    name: str = "insert_scripted"
    monitor_profile: MonitorProfile = "insertion"
    timeout_s: float = 5.0

    def preconditions(self, state: RunState) -> bool:
        return True

    async def run(self, ctx: SkillContext) -> SkillResult:
        return SkillResult(ok=False, failure="UNKNOWN")


def test_only_unknown_failures_classified_by_the_monitor_reach_the_planner(
    store: EventStore,
) -> None:
    registry = SkillRegistry(
        [Approach(), Align(), _GivesUpAsUnknown(), InsertAct(), Verify(), Retract(), WaitSensor()]
    )
    planner = FakePlanner([{"kind": "switch_skill", "skill": "insert_act"}])

    events = _run(store, Scenario(id="baseline"), "C", registry=registry, planner=planner)

    assert planner.asked == []
    assert events[-1].payload.outcome == "failed"


@pytest.mark.parametrize(
    "scenario_id",
    ["baseline", "controller_stall", "sensor_dropout", "obstacle", "tight_fit", "pose_shift"],
)
def test_on_known_failures_c_does_what_b_does(store: EventStore, scenario_id: str) -> None:
    for seed in range(4):
        b = _run(store, SCENARIOS[scenario_id], "B", seed)
        c = _run(store, SCENARIOS[scenario_id], "C", seed)

        assert "planner_called" not in _types(c)
        assert c[-1].payload.outcome == b[-1].payload.outcome
        assert c[-1].payload.reason == b[-1].payload.reason
