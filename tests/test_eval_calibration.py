"""Calibration measurements over a run's events (`cellrt/eval/calibration.py`, #83, ADR 030).
Each test writes the events it needs: samples at chosen times, inside chosen skills."""

from __future__ import annotations

import math
from typing import Any

import pytest

from cellrt.eval import calibration
from cellrt.eval.calibration import (
    MIN_VALUES,
    REST_STEPS,
    ForceVerdict,
    Measured,
    Summary,
    measure,
    summarize,
)
from cellrt.executor.samples import signals_of
from cellrt.monitor.classifier import Monitor, MonitorConfig
from cellrt.runtime.events import (
    SCHEMA_VERSION,
    Event,
    MonitorVerdict,
    PreconditionFailed,
    RunStarted,
    SignalSampled,
    SkillCancelled,
    SkillFinished,
    SkillStarted,
)
from cellrt.runtime.state import FORCE_LIMIT_N, RunState, apply
from cellrt.runtime.task import Task

STUCK_N, LIMIT_N = calibration.STUCK_N, calibration.LIMIT_N
FREE_ABOVE_MM = 50.0
STEP = 0.05


def _sample(
    force: float = 0.5,
    height: float = 100.0,
    xy: tuple[float, float] | None = (0.0, 0.0),
    *,
    valid: bool = True,
    dropped: int | None = None,
    over_limit_s: float | None = None,
) -> SignalSampled:
    return SignalSampled(
        step=0,
        height_mm=height,
        depth_mm=0.0,
        xy_offset_mm=xy,
        xy_error_mm=0.0 if xy is None else math.hypot(*xy),
        raw_force_n=(0.0, 0.0, force),
        tare_offset_n=(0.0, 0.0, 0.0),
        force_n=force,
        torque_nm=0.0,
        wrench_valid=valid,
        force_over_limit_s=over_limit_s,
        wrench_dropped=dropped,
    )


def _events(*timed: tuple[float, Any]) -> list[Event]:
    return [
        Event(run_id="r", seq=n, ts=ts, wall_ts=ts, payload=p)
        for n, (ts, p) in enumerate(timed, start=1)
    ]


def _measure(*timed: tuple[float, Any]) -> Measured:
    return measure(_events(*timed), free_above_mm=FREE_ABOVE_MM)


def _verdict(failure_class: str, **evidence: Any) -> MonitorVerdict:
    return MonitorVerdict(
        skill="insert_scripted", failure_class=failure_class, rule="r", evidence=evidence
    )


def _held(n: int, t0: float = 0.0, **sample: Any) -> list[tuple[float, SignalSampled]]:
    """`n` samples one step apart, all at one pose."""
    return [(t0 + STEP * k, _sample(**sample)) for k in range(1, n + 1)]


# --- the sample period and the wrench -----------------------------------------------------------


def test_the_period_is_the_time_between_consecutive_samples() -> None:
    m = _measure(
        (0.0, SkillStarted(skill="approach", attempt=1)),
        (0.05, _sample()),
        (0.10, _sample()),
        (0.10, SkillFinished(skill="approach", ok=True)),
        (0.16, _sample()),  # between skills: still a sample
    )

    assert m.periods_s == pytest.approx((0.05, 0.06))
    assert m.samples == 3


def test_a_wrench_that_was_not_fresh_and_dropped_readings_are_counted() -> None:
    m = _measure(
        (0.05, _sample(valid=False)),
        (0.10, _sample(dropped=3)),
        (0.15, _sample(dropped=None)),  # the mock records none
        (0.20, _sample(valid=False, dropped=1)),
    )

    assert (m.not_fresh, m.dropped) == (2, 4)


# --- free space and rest ------------------------------------------------------------------------


def test_free_space_is_every_fresh_sample_of_approach() -> None:
    m = _measure(
        (0.0, SkillStarted(skill="approach", attempt=1)),
        (0.05, _sample(0.4, height=180.0)),
        (0.10, _sample(0.9, height=170.0)),
        (0.15, _sample(7.0, height=160.0, valid=False)),  # not fresh: its force is not one
        (0.15, SkillFinished(skill="approach", ok=True)),
        (0.15, SkillStarted(skill="align", attempt=1)),
        (0.20, _sample(0.6, height=150.0)),
    )

    assert m.free_space_n == (0.4, 0.9)


def test_rest_is_the_plug_still_for_several_steps_kept_apart_per_skill() -> None:
    assert REST_STEPS == 5
    m = _measure(
        (0.0, SkillStarted(skill="approach", attempt=1)),
        *_held(7, force=0.2),  # still from the 2nd sample; at rest from the 6th
        (0.40, _sample(0.8, height=90.0)),  # moved: the count starts over
        *_held(5, t0=0.40, force=0.3, height=90.0),  # at rest at the 5th only
        (0.65, SkillFinished(skill="approach", ok=True)),
        (0.65, SkillStarted(skill="align", attempt=1)),
        *_held(2, t0=0.65, force=0.4, height=90.0),  # still across the skills: at rest
        (0.75, SkillFinished(skill="align", ok=True)),
        (0.75, SkillStarted(skill="insert_scripted", attempt=1)),
        (0.80, _sample(0.5, height=90.0)),  # still, but in insert
    )

    assert m.rest_approach_n == (0.2, 0.2, 0.3)
    assert m.rest_align_n == (0.4, 0.4)


@pytest.mark.parametrize(
    ("change", "rest"),
    [
        ({}, (0.2, 0.2)),
        ({6: {"height": FREE_ABOVE_MM}}, (0.2,)),  # at the entrance: not clear of the port
        ({6: {"valid": False}}, (0.2,)),  # its force is not one; the plug is still still
        ({3: {"xy": None}}, ()),  # a log from before xy_offset_mm: the count starts over
        ({3: {"xy": (2 * calibration.REST_MM, 0.0)}}, ()),  # moved sideways
    ],
    ids=["held", "at the entrance", "not fresh", "no offset", "moved sideways"],
)
def test_rest_needs_the_plug_clear_still_and_a_fresh_wrench(
    change: dict[int, dict[str, Any]], rest: tuple[float, ...]
) -> None:
    samples = [(STEP * k, _sample(0.2, **change.get(k - 1, {}))) for k in range(1, REST_STEPS + 3)]

    m = _measure((0.0, SkillStarted(skill="align", attempt=1)), *samples)

    assert m.rest_align_n == rest


@pytest.mark.parametrize(("free_above_mm", "rest"), [(None, 2), (math.inf, 0)])
def test_the_entrance_height_bounds_rest(free_above_mm: float | None, rest: int) -> None:
    events = _events((0.0, SkillStarted(skill="align", attempt=1)), *_held(7, height=1.0))

    assert len(measure(events, free_above_mm=free_above_mm).rest_align_n) == rest


@pytest.mark.parametrize(
    "end",
    [
        SkillCancelled(skill="approach", reason="preempted"),
        SkillFinished(skill="approach", ok=False),
    ],
    ids=["cancelled", "finished"],
)
def test_a_skill_ends_when_it_is_cancelled_or_finished(end: Any) -> None:
    m = _measure(
        (0.0, SkillStarted(skill="approach", attempt=1)),
        (0.05, _sample(0.4)),
        (0.05, end),
        (0.10, _sample(0.5)),
        (0.10, PreconditionFailed(skill="approach", attempt=2)),  # never ran
        (0.15, _sample(0.6)),
    )

    assert m.free_space_n == (0.4,)


# --- force verdicts -----------------------------------------------------------------------------


def test_insert_stuck_is_timed_from_the_start_of_its_run_above_the_threshold() -> None:
    m = _measure(
        (0.05, _sample(12.0)),
        (0.10, _sample(9.0)),  # under again: the run starts over
        (0.15, _sample(12.0)),
        (0.20, _sample(STUCK_N)),  # at the threshold is not above it
        (0.25, _sample(11.0)),
        (0.30, _sample(2.0, valid=False)),  # a stale wrench neither ends nor starts a run
        (0.80, _sample(14.0)),
        (0.80, _verdict("INSERT_STUCK")),
        (0.85, _sample(14.0)),
        (0.85, _verdict("INSERT_STUCK")),  # the same rise: a repeat
    )

    (first, second) = m.force_verdicts
    assert first.failure_class == "INSERT_STUCK" and first.rise_s == pytest.approx(0.55)
    assert second == ForceVerdict("INSERT_STUCK", None, repeat=True)


def test_insert_stuck_starts_again_with_each_skill() -> None:
    m = _measure(
        (0.0, SkillStarted(skill="align", attempt=1)),
        (0.05, _sample(12.0)),
        (0.10, _sample(12.0)),
        (0.10, SkillFinished(skill="align", ok=True)),
        (0.10, SkillStarted(skill="insert_scripted", attempt=1)),  # the Monitor resets
        *_held(10, t0=0.10, force=12.0),
        (0.60, _verdict("INSERT_STUCK")),
    )

    (v,) = m.force_verdicts
    assert v.rise_s == pytest.approx(0.45)


def test_force_limit_is_timed_from_the_first_step_that_adds_to_the_monitors_sum() -> None:
    m = _measure(
        (0.00, _sample(3.0)),
        (0.05, _sample(25.0, over_limit_s=0.0)),  # the stream was not over it: the sum adds 0
        (0.10, _sample(15.0, over_limit_s=0.02)),  # a spike between samples (AIC): this step
        (0.15, _sample(3.0, over_limit_s=0.0)),  # under again: the Monitor's sum goes on
        (0.70, _sample(15.0, over_limit_s=0.03)),
        (0.70, _verdict("FORCE_LIMIT", over_s=0.52)),
        (0.75, _sample(25.0, over_limit_s=0.05)),
        (0.75, _verdict("FORCE_LIMIT", over_s=0.57)),  # every sample over it after the trigger
    )

    (first, second) = m.force_verdicts
    assert first.rise_s == pytest.approx(0.65) and first.over_s == 0.52
    assert second == ForceVerdict("FORCE_LIMIT", None, repeat=True, over_s=0.57)


def test_a_fresh_sample_over_the_limit_adds_its_step_where_no_stream_is_read() -> None:
    m = _measure(
        (0.05, _sample(LIMIT_N)),  # at the limit is not over it
        (0.10, _sample(LIMIT_N + 1.0)),  # the mock: no force_over_limit_s; adds 0.05 to 0.10
        (0.60, _sample(LIMIT_N + 1.0)),
        (0.60, _verdict("FORCE_LIMIT", over_s=0.55)),
    )

    (v,) = m.force_verdicts
    assert v.rise_s == pytest.approx(0.55)


@pytest.mark.parametrize(("started", "rise"), [(True, 0.50), (False, 0.45)])
def test_a_first_sample_over_the_limit_is_timed_from_the_tasks_start(
    started: bool, rise: float
) -> None:
    run_started = RunStarted(scenario_id="s", setting="A", seed=0, attempt=1, task=Task(),
                             config_hash="h", schema_version=SCHEMA_VERSION)  # fmt: skip
    m = _measure(
        *([(0.0, run_started)] if started else []),
        *_held(10, force=LIMIT_N + 1.0),
        (0.50, _verdict("FORCE_LIMIT", over_s=0.5)),
    )

    (v,) = m.force_verdicts
    assert v.rise_s == pytest.approx(rise)  # without a start, from the sample itself


@pytest.mark.parametrize(
    ("stream", "rise", "over"), [(False, 0.50, 0.50), (True, 0.55, 0.53)], ids=["mock", "aic"]
)
def test_the_force_limit_rise_covers_the_monitors_own_sum(
    stream: bool, rise: float, over: float
) -> None:
    """The real fold and Monitor: the force goes over the limit in the 3rd step (0.03 s into it
    on the AIC path, where the stream reads it) and stays over it."""
    state = RunState()
    monitor = Monitor(STEP, MonitorConfig(), force_time=lambda: state.over_limit_s)
    monitor.reset("motion", skill="align")
    events: list[Event] = []
    for k in range(1, 40):
        ts = round(STEP * k, 6)
        over_limit_s = None if not stream else (0.03 if k == 3 else STEP if k > 3 else 0.0)
        sample = _sample(LIMIT_N + 5.0 if k >= 3 else 3.0, over_limit_s=over_limit_s)
        events.append(Event(run_id="r", seq=len(events) + 1, ts=ts, wall_ts=ts, payload=sample))
        state = apply(state, events[-1])
        if (v := monitor.observe(signals_of(sample))) is not None:
            verdict = MonitorVerdict(
                skill="align", failure_class=v.failure_class.value, rule=v.rule, evidence=v.evidence
            )
            events.append(
                Event(run_id="r", seq=len(events) + 1, ts=ts, wall_ts=ts, payload=verdict)
            )
            break

    (fv,) = measure(events, free_above_mm=None).force_verdicts
    assert (fv.rise_s, fv.over_s) == pytest.approx((rise, over))
    assert fv.rise_s >= fv.over_s


def test_a_verdict_without_a_rise_is_counted_untimed() -> None:
    m = _measure(
        (0.05, _sample(12.0)),
        (0.05, _verdict("POSE_ERROR")),  # not a force verdict
        (0.10, _sample(3.0)),
        (0.15, _sample(25.0, valid=False)),  # stale: no rise for INSERT_STUCK nor the limit
        (0.15, _verdict("INSERT_STUCK")),
        (0.15, _verdict("FORCE_LIMIT", over_s="x")),  # evidence that is no number
    )

    assert m.force_verdicts == (
        ForceVerdict("INSERT_STUCK", None),
        ForceVerdict("FORCE_LIMIT", None),
    )


def test_the_thresholds_are_the_monitors_own() -> None:
    monitors = (MonitorConfig().stuck_force_n, FORCE_LIMIT_N)

    assert monitors == (STUCK_N, LIMIT_N)


# --- summaries ----------------------------------------------------------------------------------


def test_a_summary_has_the_mean_sd_nearest_rank_p95_and_max() -> None:
    values = [float(v) for v in range(1, 21)]  # 1 .. 20

    s = summarize(values)

    assert s == Summary(n=20, mean=10.5, sd=pytest.approx(5.766, abs=1e-3), p95=19.0, max=20.0)


def test_fewer_values_than_the_minimum_have_no_summary() -> None:
    assert summarize([1.0] * (MIN_VALUES - 1)) is None
    assert summarize([1.0] * MIN_VALUES) == Summary(
        n=MIN_VALUES, mean=1.0, sd=0.0, p95=1.0, max=1.0
    )


def test_a_minimum_can_be_asked_for() -> None:
    assert summarize([0.5], min_values=1) == Summary(n=1, mean=0.5, sd=0.0, p95=0.5, max=0.5)
    with pytest.raises(ValueError, match="at least 1"):
        summarize([0.5], min_values=0)


def test_no_events_measure_nothing() -> None:
    assert _measure() == Measured()
