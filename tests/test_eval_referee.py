"""Each verdict judged against the referee at its time (`cellrt/eval/referee.py`, ADR 029), and
the trial's class (ADR 026)."""

from __future__ import annotations

from typing import Any

import pytest
from aic_fakes import FakeNode

from adapters.aic.run import run_goal
from cellrt.eval.referee import (
    ADAPTER_ABORT,
    COMPENSATED,
    FALSE_ALARM,
    FALSE_SUCCESS,
    KNOWN_FAILURE,
    RECOVERED,
    UNCLASSIFIED,
    UNCLASSIFIED_FAILURE,
    UNKNOWN_FAILURE,
    RefereeTrial,
    judge,
)
from cellrt.runtime.events import (
    SCHEMA_VERSION,
    Event,
    MonitorVerdict,
    RecoveryStarted,
    RunFinished,
    RunStarted,
    SkillFinished,
    SkillStarted,
    WorldReady,
)
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task

TARGET = ("nic_card_mount_0", "sfp_port_0")
ORIGIN = 100.0
TAU = 0.25


def _run(
    *body: tuple[float, Any], outcome: str = "failed", origin: float | None = ORIGIN
) -> list[Event]:
    """A run of `body` (run time, payload), with its start, `world_ready` and finish."""
    end = max((ts for ts, _ in body), default=0.0) + 1.0
    payloads = [
        (
            0.0,
            RunStarted(
                scenario_id="pilot-v1/x",
                setting="A",
                seed=0,
                attempt=1,
                task=Task(id="x_r0", time_limit=180.0),
                config_hash="h",
                schema_version=SCHEMA_VERSION,
            ),
        ),
        (0.0, WorldReady(sim_origin_s=origin, port=None, plug=None, gripper=None)),
        *body,
        (end, RunFinished(outcome=outcome, reason="r", steps=1, duration=end)),  # type: ignore[arg-type]
    ]
    return [
        Event(run_id="pilot-v1/x/A/0/a1", seq=n, ts=ts, wall_ts=0.0, payload=p)
        for n, (ts, p) in enumerate(payloads, start=1)
    ]


def _referee(
    last: tuple[str, ...] | None = TARGET, at: float | None = None, tau: float = TAU
) -> RefereeTrial:
    """`at` is the insertion's run time; None leaves it unmapped."""
    return RefereeTrial(
        target=TARGET,
        last_port=last,
        insertion_sim_s=None if at is None else ORIGIN + at,
        tolerance_s=None if at is None else tau,
        contact_messages=0,
        contacts_score=0.0,
        force_penalty=0.0,
        tier3=75.0 if last == TARGET else 0.0,
        total=None,
    )


def _start(ts: float, skill: str = "insert") -> tuple[float, SkillStarted]:
    return ts, SkillStarted(skill=skill, attempt=1)


def _verdict(
    ts: float, cls: str = "INSERT_STUCK", skill: str = "insert"
) -> tuple[float, MonitorVerdict]:
    return ts, MonitorVerdict(skill=skill, failure_class=cls, rule="r")


def _recovery(ts: float) -> tuple[float, RecoveryStarted]:
    return ts, RecoveryStarted(
        skill="insert",
        failure_class="INSERT_STUCK",
        action="retract_realign",
        count_for_class=1,
        count_total=1,
    )


# --- per verdict -----------------------------------------------------------------------------


def test_a_verdict_on_a_plug_that_never_went_in_is_a_real_failure_unconfirmed_in_a() -> None:
    judged = judge(_run(_start(1.0), _verdict(10.0)), _referee(last=None))

    (v,) = judged.verdicts
    assert (v.counted, v.unconfirmed) == ("real_failure", True)
    assert judged.trial_class == KNOWN_FAILURE


def test_a_real_failure_the_run_went_on_from_is_confirmed() -> None:
    judged = judge(_run(_start(1.0), _verdict(10.0), _recovery(10.5)), _referee(last=None))

    (v,) = judged.verdicts
    assert (v.counted, v.unconfirmed) == ("real_failure", False)


def test_an_unknown_real_failure_is_its_own_class() -> None:
    judged = judge(_run(_start(1.0), _verdict(10.0, "UNKNOWN")), _referee(last=None))

    assert judged.trial_class == UNKNOWN_FAILURE


def test_a_verdict_after_the_plug_went_in_is_a_false_alarm() -> None:
    judged = judge(_run(_start(1.0), _verdict(10.0)), _referee(at=5.0))

    (v,) = judged.verdicts
    assert (v.counted, v.tie, v.late) == ("false_alarm", False, False)
    assert judged.trial_class == FALSE_ALARM
    assert judged.insertion_t == pytest.approx(5.0)


@pytest.mark.parametrize("at", [10.0 - TAU, 10.0, 10.0 + TAU])
def test_an_insertion_within_tau_of_the_verdict_is_a_tie_and_a_false_alarm(at: float) -> None:
    (v,) = judge(_run(_start(1.0), _verdict(10.0)), _referee(at=at)).verdicts

    assert (v.counted, v.tie) == ("false_alarm", True)


def test_a_late_insertion_with_a_recovery_in_between_is_recovered() -> None:
    judged = judge(_run(_start(1.0), _verdict(10.0), _recovery(10.5)), _referee(at=20.0))

    (v,) = judged.verdicts
    assert v.counted == "recovered"
    assert judged.trial_class == RECOVERED


def test_a_late_insertion_nothing_acted_on_is_a_late_false_alarm() -> None:
    (v,) = judge(_run(_start(1.0), _verdict(10.0)), _referee(at=10.0 + TAU + 0.01)).verdicts

    assert (v.counted, v.late, v.tie) == ("false_alarm", True, False)


def test_a_recovery_only_after_the_insertion_does_not_make_it_recovered() -> None:
    (v,) = judge(_run(_start(1.0), _verdict(10.0), _recovery(25.0)), _referee(at=20.0)).verdicts

    assert (v.counted, v.late) == ("false_alarm", True)


def test_a_recovery_before_the_verdict_does_not_make_it_recovered() -> None:
    events = _run(_start(1.0), _verdict(5.0), _recovery(5.5), _start(6.0), _verdict(10.0))

    first, second = judge(events, _referee(at=20.0)).verdicts

    assert first.counted == "recovered"  # the recovery at 5.5 came after it
    assert (second.counted, second.late) == ("false_alarm", True)


@pytest.mark.parametrize("unmapped", ["no mapping in the bag", "no origin"])
def test_an_inserted_trial_whose_time_cannot_be_mapped_is_judged_as_a_whole(unmapped: str) -> None:
    body = (_start(1.0), _verdict(10.0), _recovery(10.5))
    if unmapped == "no origin":
        judged = judge(_run(*body, origin=None), _referee(at=20.0))
    else:
        judged = judge(_run(*body), _referee(at=None))

    (v,) = judged.verdicts
    assert judged.fallback is True
    assert judged.insertion_t is None
    assert (v.counted, v.tie, v.late) == ("false_alarm", False, False)  # ADR 015's rule


def test_an_unmapped_trial_that_never_went_in_is_not_a_fallback() -> None:
    judged = judge(_run(_start(1.0), _verdict(10.0)), _referee(last=None, at=None))

    assert judged.fallback is False
    assert judged.verdicts[0].counted == "real_failure"


def test_an_insertion_into_another_port_is_a_real_failure() -> None:
    judged = judge(
        _run(_start(1.0), _verdict(10.0)), _referee(last=("nic_card_mount_1", "sfp_port_0"))
    )

    assert judged.wrong_port is True and judged.inserted is False
    assert judged.verdicts[0].counted == "real_failure"


def test_a_skills_own_failed_result_is_a_verdict() -> None:
    events = _run(
        _start(1.0, "verify"), (9.0, SkillFinished(skill="verify", ok=False, failure=None))
    )

    judged = judge(events, _referee(last=None))

    (v,) = judged.verdicts
    assert (v.source, v.failure_class) == ("skill", UNCLASSIFIED)
    # Not the Monitor's UNKNOWN, which C-LLM acts on (D6): no setting recovers a classless
    # failure, such as verify's "not seated" or a skill that raised (#82 review).
    assert judged.trial_class == UNCLASSIFIED_FAILURE


def test_a_skills_own_failed_result_of_a_known_class_is_a_known_failure() -> None:
    events = _run(_start(1.0), (9.0, SkillFinished(skill="insert", ok=False, failure="TIMEOUT")))

    assert judge(events, _referee(last=None)).trial_class == KNOWN_FAILURE


@pytest.mark.parametrize(
    ("recovery", "counted"),
    [(20.0 - TAU - 0.01, "recovered"), (20.0 - TAU, "false_alarm"), (20.0, "false_alarm")],
)
def test_a_recovery_must_start_over_tau_before_the_insertion(recovery: float, counted: str) -> None:
    # The insertion's time is known to within tau: a recovery that starts as the plug goes in
    # did not put it in (against the Monitor, as ties are).
    (v,) = judge(_run(_start(1.0), _verdict(10.0), _recovery(recovery)), _referee(at=20.0)).verdicts

    assert v.counted == counted


def test_a_failure_the_monitor_already_named_is_counted_once() -> None:
    events = _run(
        _start(1.0),
        _verdict(9.0, "FORCE_LIMIT"),
        (9.0, SkillFinished(skill="insert", ok=False, failure="INSERT_STUCK")),
    )

    (v,) = judge(events, _referee(last=None)).verdicts

    assert (v.source, v.failure_class) == ("monitor", "FORCE_LIMIT")


def test_the_next_attempt_of_a_skill_can_fail_on_its_own_again() -> None:
    events = _run(
        _start(1.0),
        _verdict(5.0),
        _recovery(5.5),
        _start(6.0),
        (9.0, SkillFinished(skill="insert", ok=False, failure="INSERT_STUCK")),
    )

    assert [v.source for v in judge(events, _referee(last=None)).verdicts] == ["monitor", "skill"]


# --- per trial -------------------------------------------------------------------------------


def test_success_without_an_insertion_is_a_false_success_whatever_came_before() -> None:
    events = _run(_start(1.0), _verdict(5.0), _recovery(5.5), outcome="success")

    judged = judge(events, _referee(last=None))

    assert judged.trial_class == FALSE_SUCCESS
    assert len(judged.verdicts) == 1  # listed


def test_success_with_an_insertion_and_no_verdict_is_compensated() -> None:
    assert judge(_run(_start(1.0), outcome="success"), _referee(at=20.0)).trial_class == COMPENSATED


@pytest.mark.parametrize("last", [TARGET, None])
def test_an_end_without_success_or_verdict_is_an_adapter_abort(
    last: tuple[str, ...] | None,
) -> None:
    events = _run(_start(1.0), outcome="aborted")

    assert (
        judge(events, _referee(last=last, at=None if last is None else 3.0)).trial_class
        == ADAPTER_ABORT
    )


def test_the_first_verdict_gives_the_class() -> None:
    events = _run(
        _start(1.0), _verdict(5.0), _recovery(5.5), _start(6.0), _verdict(25.0, "UNKNOWN")
    )

    judged = judge(events, _referee(at=20.0))

    assert [v.counted for v in judged.verdicts] == ["recovered", "false_alarm"]
    assert judged.trial_class == RECOVERED


def test_an_unfinished_run_is_not_judged() -> None:
    events = _run(_start(1.0))[:-1]

    with pytest.raises(ValueError, match="run_finished"):
        judge(events, _referee())


def test_a_recorded_aic_run_is_judged_on_its_own_events(store: EventStore) -> None:
    task = Task(id="trial_1_task_1", time_limit=180.0)
    node = FakeNode(task=task)
    assert run_goal(node, task, store) is True
    (run,) = store.runs()
    events = store.events(run.run_id)
    referee = RefereeTrial(
        target=("nic_card_mount_0", "sfp_port_0"),
        last_port=("nic_card_mount_0", "sfp_port_0"),
        insertion_sim_s=node.sim_now_s() - 1.0,
        tolerance_s=TAU,
        contact_messages=0,
        contacts_score=0.0,
        force_penalty=0.0,
        tier3=75.0,
        total=None,
    )

    judged = judge(events, referee)

    assert judged.trial_class == COMPENSATED
    assert judged.insertion_t == pytest.approx(events[-1].ts - 1.0, abs=1e-6)
