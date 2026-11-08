"""Force at the F/T rate (ADR 013): every wrench message is tared with the newest tare, per axis,
and each step's sample carries the time over 20 N by the scorer's rule (`ScoringTier2.cc:863-903`
@ `3ab0653`): for each message after the first, add the time since the previous message when the
later one is over the limit, including the pair across a step boundary."""

from __future__ import annotations

import math

import pytest
from aic_fakes import TARE_N, FakeNode

from adapters.aic.force import (
    WRENCH_BUFFER_SIZE,
    TaredWrench,
    WrenchFeed,
    time_over_limit,
)
from adapters.aic.node import WrenchReading
from adapters.aic.run import run_goal
from adapters.aic.world import AicWorld
from cellrt.replay import replay_lines
from cellrt.runtime.events import MonitorVerdict, SignalSampled
from cellrt.runtime.store import EventStore
from cellrt.runtime.task import Task

TASK = Task(id="trial_1_task_1", time_limit=180.0)
ZERO = (0.0, 0.0, 0.0)


def _reading(stamp_s: float, force: tuple[float, float, float]) -> WrenchReading:
    return WrenchReading(stamp_s=stamp_s, force=force, torque=ZERO)


def _tared(stamp_s: float, force_z: float) -> TaredWrench:
    return TaredWrench(
        stamp_s=stamp_s,
        raw_force=(0.0, 0.0, force_z),
        tare_force=ZERO,
        raw_torque=ZERO,
        tare_torque=ZERO,
    )


# --- taring ----------------------------------------------------------------------------------


def test_each_wrench_is_tared_per_axis_with_the_newest_tare() -> None:
    feed = WrenchFeed()
    feed.on_tare((0.0, 0.0, 5.0), ZERO)
    feed.on_wrench(_reading(1.00, (3.0, 4.0, 5.0)))
    feed.on_tare((3.0, 0.0, 0.0), ZERO)  # the tare changes between two messages
    feed.on_wrench(_reading(1.02, (3.0, 4.0, 5.0)))

    first, second = feed.drain().readings
    # (3, 4, 5) - (0, 0, 5) = (3, 4, 0): 5 N. The difference of the magnitudes would be 2.07 N.
    assert first.force_n == pytest.approx(5.0)
    # (3, 4, 5) - (3, 0, 0) = (0, 4, 5): 6.40 N, with the tare that was newest then.
    assert second.force_n == pytest.approx(math.hypot(4.0, 5.0))


def test_a_wrench_before_the_first_tare_is_skipped_as_the_scorer_skips_it() -> None:
    feed = WrenchFeed()
    feed.on_wrench(_reading(1.00, (0.0, 0.0, 30.0)))
    feed.on_tare(ZERO, ZERO)
    feed.on_wrench(_reading(1.02, (0.0, 0.0, 30.0)))

    assert [w.stamp_s for w in feed.drain().readings] == [1.02]


def test_the_torque_is_tared_per_axis_too() -> None:
    feed = WrenchFeed()
    feed.on_tare(ZERO, (0.0, 0.0, 2.0))
    feed.on_wrench(WrenchReading(stamp_s=1.0, force=ZERO, torque=(0.0, 0.0, 2.5)))

    (reading,) = feed.drain().readings
    assert reading.torque_nm == pytest.approx(0.5)


# --- the bounded buffer ----------------------------------------------------------------------


def test_the_buffer_keeps_the_newest_256_readings_and_counts_the_drops() -> None:
    feed = WrenchFeed()
    feed.on_tare(ZERO, ZERO)
    for i in range(WRENCH_BUFFER_SIZE + 44):
        feed.on_wrench(_reading(i * 0.02, (0.0, 0.0, 1.0)))

    drained = feed.drain()
    assert WRENCH_BUFFER_SIZE == 256
    assert len(drained.readings) == 256
    assert drained.readings[0].stamp_s == pytest.approx(44 * 0.02)  # the oldest 44 dropped
    assert drained.dropped == 44

    again = feed.drain()  # the count is per drain
    assert (again.readings, again.dropped) == ((), 0)


def test_the_buffer_bound_must_be_positive() -> None:
    with pytest.raises(ValueError):
        WrenchFeed(capacity=0)


def test_drops_are_recorded_on_the_step_that_drained_them(store: EventStore) -> None:
    # The fake publishes 300 messages per step into a buffer of 256.
    node = FakeNode(task=TASK, wrench_period_s=0.05 / 300)

    run_goal(node, TASK, store)

    (run,) = store.runs()
    samples = [e.payload for e in store.events(run.run_id) if isinstance(e.payload, SignalSampled)]
    assert samples and all(s.wrench_dropped == 300 - 256 for s in samples)


# --- the scorer's rule -----------------------------------------------------------------------


def test_time_over_limit_adds_the_gap_before_each_reading_over_20_n() -> None:
    # Uneven stamps on purpose. Over the limit: 1.03 (gap 0.03), 1.04 (0.01), 1.10 (0.05).
    readings = [
        _tared(1.03, 25.0),
        _tared(1.04, 20.5),
        _tared(1.05, 20.0),  # not over: the scorer compares with >
        _tared(1.10, 30.0),
    ]
    previous = _tared(1.00, 0.0)

    assert time_over_limit(previous, readings) == pytest.approx(0.03 + 0.01 + 0.05)
    # Without a previous reading the first one has no gap, as the scorer's loop starts at 1.
    assert time_over_limit(None, readings) == pytest.approx(0.01 + 0.05)


def test_a_nan_reading_is_never_over_the_limit_but_still_closes_a_gap() -> None:
    nan = TaredWrench(
        stamp_s=1.02,
        raw_force=(math.nan, 0.0, 30.0),
        tare_force=ZERO,
        raw_torque=ZERO,
        tare_torque=ZERO,
    )
    readings = [nan, _tared(1.04, 30.0)]

    assert time_over_limit(_tared(1.00, 30.0), readings) == pytest.approx(0.02)


def test_a_stamp_going_back_adds_no_negative_time() -> None:
    assert time_over_limit(_tared(1.10, 30.0), [_tared(1.00, 30.0)]) == 0.0


def _force_at(t: float) -> tuple[float, float, float]:
    """25 N raw (24 N tared) from 100.06 s to 100.16 s, else 1.5 N."""
    return (0.0, 0.0, 25.0) if 100.06 - 1e-9 <= t <= 100.16 + 1e-9 else (0.0, 0.0, 1.5)


def test_summed_over_steps_the_time_over_20_n_is_the_scorers_hand_computed_total() -> None:
    # 50 Hz from 100.00 s: 100.00, 100.02, ... Steps end at 100.05, 100.10, 100.15, 100.20.
    node = FakeNode(task=TASK, force_at=_force_at)
    world = AicWorld(node, TASK)

    per_step = []
    for _ in range(4):
        world.step(0.05)
        per_step.append(world.signals().force_over_limit_s)

    # By hand: the steps drain (.02 .04), (.06 .08 .10), (.12 .14), (.16 .18 .20). Readings over
    # 20 N: .06 .08 .10 | .12 .14 | .16, each adding its 0.02 s gap to the reading before it.
    # 100.06's predecessor (100.04) was drained in the step before: the pair across the boundary.
    assert per_step == pytest.approx([0.0, 0.06, 0.04, 0.02])
    assert sum(per_step) == pytest.approx(6 * 0.02)


def test_the_newest_reading_gives_the_force_of_the_sample() -> None:
    node = FakeNode(task=TASK, force_at=_force_at)
    world = AicWorld(node, TASK)

    world.step(0.05)  # newest 100.04: 1.5 N raw
    assert world.signals().force_n == pytest.approx(1.5 - TARE_N[2])
    world.step(0.05)  # newest 100.10: 25 N raw
    assert world.signals().force_n == pytest.approx(25.0 - TARE_N[2])


# --- freshness and non-finite readings -------------------------------------------------------


def test_a_step_without_a_fresh_wrench_is_invalid() -> None:
    # The broadcaster stops 0.15 s into the goal.
    node = FakeNode(task=TASK, wrench_until_s=100.15)
    world = AicWorld(node, TASK)

    valid = []
    for _ in range(6):  # steps end at 100.05 ... 100.30
        world.step(0.05)
        valid.append(world.signals().wrench_valid)

    # The newest reading is 100.14 from then on: 0.06 s old at 100.20, 0.11 s at 100.25.
    assert valid == [True, True, True, True, False, False]


def test_a_wrench_exactly_at_the_freshness_bound_is_still_valid() -> None:
    node = FakeNode(task=TASK, wrench_until_s=100.10)  # the newest reading stays 100.10
    world = AicWorld(node, TASK)

    valid = []
    for _ in range(5):  # steps end at 100.05 ... 100.25
        world.step(0.05)
        valid.append(world.signals().wrench_valid)

    # 0.10 s old at 100.20 (the bound itself), 0.15 s at 100.25.
    assert valid == [True, True, True, True, False]


def test_after_a_drop_the_lost_gap_is_not_counted_as_time_over_the_limit() -> None:
    # Step 2 publishes 300 readings into the 256 buffer; only its first kept reading is over
    # 20 N. Paired with step 1's last reading it would add the whole lost stretch.
    def force_at(t: float) -> tuple[float, float, float]:
        first_kept = 100.05 + 45 * 0.05 / 300
        return (0.0, 0.0, 30.0) if abs(t - first_kept) < 1e-6 else (0.0, 0.0, 1.5)

    node = FakeNode(task=TASK, force_at=force_at, wrench_period_s=0.05 / 300)
    world = AicWorld(node, TASK)
    world.step(0.05)
    world.step(0.05)

    s = world.signals()
    assert s.wrench_dropped == 44
    assert s.force_over_limit_s == 0.0  # the scorer would add one reading's gap at most


def test_a_non_finite_reading_is_not_used_for_the_force(store: EventStore) -> None:
    def spike(t: float) -> tuple[float, float, float]:
        return (math.nan, 0.0, 30.0) if t >= 100.04 - 1e-9 else (0.0, 0.0, 1.5)

    node = FakeNode(task=TASK, force_at=spike)

    run_goal(node, TASK, store)

    (run,) = store.runs()
    samples = [e.payload for e in store.events(run.run_id) if isinstance(e.payload, SignalSampled)]
    # The last finite reading (100.02) stays the newest; it ages out after 0.1 s.
    assert samples[0].wrench_valid and samples[0].force_n == pytest.approx(0.5)
    assert not samples[-1].wrench_valid
    assert all(math.isfinite(v) for s in samples for v in (*s.raw_force_n, *s.tare_offset_n))


def test_a_non_finite_tare_is_not_used_for_the_force() -> None:
    feed = WrenchFeed()
    feed.on_tare((math.inf, 0.0, 0.0), ZERO)
    feed.on_wrench(_reading(1.0, (0.0, 0.0, 1.0)))

    (reading,) = feed.drain().readings
    assert not reading.finite


# --- replay ----------------------------------------------------------------------------------


def test_replay_signals_prints_the_tared_force_and_the_time_over_20_n(store: EventStore) -> None:
    node = FakeNode(task=TASK, force_at=_force_at)
    run_goal(node, TASK, store)

    (run,) = store.runs()
    lines = [
        line
        for line in replay_lines(store.events(run.run_id), signals=True)
        if "signal_sampled" in line
    ]

    assert [line.split(" force=", 1)[1] for line in lines[:4]] == [
        "0.50N over20=0.000s valid=True",
        "24.00N over20=0.060s valid=True",
        "24.00N over20=0.040s valid=True",
        "0.50N over20=0.020s valid=True",
    ]


def test_a_force_limit_verdict_names_the_time_the_stream_added_in_its_step(
    store: EventStore,
) -> None:
    node = FakeNode(task=TASK, raw_force=(0.0, 0.0, 30.0))  # 29 N tared from the start

    run_goal(node, TASK, store)

    (run,) = store.runs()
    events = [e.payload for e in store.events(run.run_id)]
    (at,) = [i for i, p in enumerate(events) if isinstance(p, MonitorVerdict)]
    verdict = events[at]
    assert isinstance(verdict, MonitorVerdict) and verdict.failure_class == "FORCE_LIMIT"
    sample = next(p for p in reversed(events[:at]) if isinstance(p, SignalSampled))
    # The step drained 2 or 3 readings 0.02 s apart, all over the limit.
    assert verdict.evidence["step_over_s"] == sample.force_over_limit_s
    assert verdict.evidence["step_over_s"] in (pytest.approx(0.04), pytest.approx(0.06))
