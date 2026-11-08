"""Metrics are pure functions over the event log; here they run on hand-built logs."""

from __future__ import annotations

import pytest

from cellrt.eval.metrics import compute_metrics, pass_hat_k, summarize_run
from cellrt.eval.report import render_gains
from cellrt.runtime.events import (
    SCHEMA_VERSION,
    Event,
    FaultCleared,
    FaultInjected,
    MonitorVerdict,
    Payload,
    PlannerCalled,
    RecoveryStarted,
    RunFinished,
    RunStarted,
    SignalSampled,
    SkillStarted,
)
from cellrt.runtime.task import Task


def _log(
    scenario: str, setting: str, seed: int, *steps: tuple[float, Payload], finished: bool = True
) -> list[Event]:
    """A run's events: run_started, then `steps` as (ts, payload), then run_finished if given."""
    run_id = f"{scenario}/{setting}/{seed}/a1"
    started = RunStarted(
        scenario_id=scenario,
        setting=setting,
        seed=seed,
        attempt=1,
        task=Task(),
        config_hash="h",
        schema_version=SCHEMA_VERSION,
    )
    payloads = [(0.0, started), *steps]
    return [
        Event(run_id=run_id, seq=i, ts=ts, wall_ts=0.0, payload=p)
        for i, (ts, p) in enumerate(payloads, start=1)
    ]


def _finish(outcome: str = "success", steps: int = 4, duration: float = 10.0) -> RunFinished:
    return RunFinished(outcome=outcome, reason="r", steps=steps, duration=duration)  # type: ignore[arg-type]


def _signal(step: int, force_n: float, valid: bool = True) -> SignalSampled:
    return SignalSampled(
        step=step,
        height_mm=0.0,
        depth_mm=0.0,
        xy_offset_mm=(0.0, 0.0),
        xy_error_mm=0.0,
        raw_force_n=(0.0, 0.0, force_n),
        tare_offset_n=(0.0, 0.0, 0.0),
        force_n=force_n,
        torque_nm=0.0,
        wrench_valid=valid,
    )


def _verdict(failure_class: str) -> MonitorVerdict:
    return MonitorVerdict(skill="insert_scripted", failure_class=failure_class, rule="r")


def _fault(label: str, active: bool = True, fault_id: str = "f#0") -> Payload:
    kind = label.lower()
    if active:
        return FaultInjected(fault_id=fault_id, kind=kind, label=label)
    return FaultCleared(fault_id=fault_id, kind=kind, label=label)


# --- pass^k ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("n", "c", "k", "expected"),
    [
        (5, 5, 5, 1.0),
        (5, 4, 5, 0.0),
        (10, 8, 5, 56 / 252),  # C(8,5) / C(10,5)
        (4, 2, 1, 0.5),  # k = 1 is the plain success rate
        (3, 3, 5, None),  # fewer runs than k: undefined, not 0 and not 1
        (0, 0, 5, None),
    ],
)
def test_pass_hat_k_is_the_chance_that_k_runs_drawn_without_replacement_all_succeed(
    n: int, c: int, k: int, expected: float | None
) -> None:
    result = pass_hat_k(n, c, k)

    if expected is None:
        assert result is None
    else:
        assert result == pytest.approx(expected)


def test_pass_hat_k_rejects_impossible_counts() -> None:
    for n, c, k in [(3, 4, 1), (3, -1, 1), (3, 1, 0)]:
        with pytest.raises(ValueError):
            pass_hat_k(n, c, k)


def test_pass_k_is_averaged_over_scenarios_that_have_at_least_k_runs() -> None:
    runs = [
        *(_log("s1", "B", seed, (10.0, _finish("success"))) for seed in range(2)),
        *(
            _log("s2", "B", seed, (10.0, _finish("success" if seed else "failed")))
            for seed in range(2)
        ),
        _log("s3", "B", 0, (10.0, _finish("success"))),  # one run: fewer than k = 2
    ]

    [row] = compute_metrics(runs, k=2).rows

    assert row.pass_k == pytest.approx((1.0 + 0.0) / 2)


def test_pass_k_is_undefined_when_no_scenario_has_k_runs() -> None:
    [row] = compute_metrics([_log("s1", "A", 0, (10.0, _finish()))], k=5).rows

    assert row.pass_k is None


# --- per-setting rows -----------------------------------------------------------------------


def test_each_setting_gets_a_row_with_rates_and_means_over_its_finished_runs() -> None:
    runs = [
        _log(
            "s1",
            "C",
            0,
            (1.0, PlannerCalled(skill="x", failure_class="UNKNOWN", call=1)),
            (2.0, PlannerCalled(skill="x", failure_class="UNKNOWN", call=2)),
            (9.0, _finish("success", steps=6, duration=9.0)),
        ),
        _log("s1", "C", 1, (5.0, _finish("failed", steps=2, duration=5.0))),
        _log("s1", "A", 0, (4.0, _finish("aborted", steps=3, duration=4.0))),
        _log("s1", "A", 1, finished=False),  # interrupted: not counted anywhere
    ]

    rows = {r.setting: r for r in compute_metrics(runs, k=1).rows}

    assert list(rows) == ["A", "C"]
    c = rows["C"]
    assert (c.runs, c.success_rate, c.mean_steps, c.mean_duration_s) == (2, 0.5, 4.0, 7.0)
    assert c.planner_calls_per_task == 1.0
    a = rows["A"]
    assert (a.runs, a.success_rate, a.planner_calls_per_task) == (1, 0.0, 0.0)


def test_an_unfinished_run_has_no_summary() -> None:
    assert summarize_run(_log("s1", "A", 0, finished=False)) is None


def test_recovery_rate_counts_only_runs_where_the_monitor_saw_a_failure() -> None:
    runs = [
        _log("s1", "B", 0, (1.0, _verdict("TIMEOUT")), (9.0, _finish("success"))),
        _log("s1", "B", 1, (1.0, _verdict("TIMEOUT")), (9.0, _finish("failed"))),
        _log("s1", "B", 2, (1.0, _verdict("TIMEOUT")), (9.0, _finish("success"))),
        _log("s1", "B", 3, (9.0, _finish("success"))),  # no failure: not in the rate
    ]

    [row] = compute_metrics(runs, k=1).rows

    assert row.recovery_rate == pytest.approx(2 / 3)


def test_recovery_rate_is_undefined_without_any_failure() -> None:
    [row] = compute_metrics([_log("s1", "B", 0, (9.0, _finish()))], k=1).rows

    assert row.recovery_rate is None


# --- unsafe events --------------------------------------------------------------------------


def _over_limit_run(seed: int, samples_over: int, *, valid: bool = True) -> list[Event]:
    """Samples every 0.05 s; `samples_over` of them over 20 N, split in two separate stretches."""
    steps: list[tuple[float, Payload]] = []
    first_stretch = samples_over // 2
    for i in range(1, 60):
        over = i <= first_stretch or 30 < i <= 30 + samples_over - first_stretch
        steps.append((round(i * 0.05, 6), _signal(i, 25.0 if over else 5.0, valid)))
    return _log("s1", "B", seed, *steps, (3.0, _finish()))


def test_force_over_the_limit_is_summed_across_stretches_like_the_aic_penalty() -> None:
    # 21 samples x 0.05 s = 1.05 s over 20 N, in two stretches, and no Monitor verdict at all:
    # the metric reads the signals, so it counts what the Monitor missed.
    summary = summarize_run(_over_limit_run(0, 21))

    assert summary is not None
    assert summary.over_limit_s == pytest.approx(1.05)
    assert summary.unsafe
    assert summary.verdicts == ()


def test_exactly_one_second_over_the_limit_is_not_yet_unsafe() -> None:
    summary = summarize_run(_over_limit_run(0, 20))

    assert summary is not None
    assert summary.over_limit_s == pytest.approx(1.0)
    assert not summary.unsafe


def test_time_over_the_limit_counts_from_the_run_start_not_from_zero() -> None:
    # A later task of a trial: its clock starts at 32 s, and the first sample is over 20 N.
    started = RunStarted(
        scenario_id="s1",
        setting="B",
        seed=0,
        attempt=1,
        task=Task(),
        config_hash="h",
        schema_version=SCHEMA_VERSION,
    )
    payloads = [(32.0, started), (32.05, _signal(1, 25.0)), (33.0, _finish())]
    run = [
        Event(run_id="s1/B/0/a1", seq=i, ts=ts, wall_ts=0.0, payload=p)
        for i, (ts, p) in enumerate(payloads, start=1)
    ]

    summary = summarize_run(run)

    assert summary is not None
    assert summary.over_limit_s == pytest.approx(0.05)


def test_samples_without_a_valid_wrench_do_not_count_as_over_the_limit() -> None:
    summary = summarize_run(_over_limit_run(0, 30, valid=False))

    assert summary is not None
    assert summary.over_limit_s == 0.0


def test_the_row_counts_unsafe_runs_and_the_mean_time_over_the_limit() -> None:
    runs = [_over_limit_run(0, 21), _over_limit_run(1, 4)]

    [row] = compute_metrics(runs, k=1).rows

    assert row.unsafe_runs == 1
    assert row.mean_over_limit_s == pytest.approx((1.05 + 0.2) / 2)


# --- classification accuracy ----------------------------------------------------------------


def test_each_verdict_is_judged_against_the_fault_active_when_it_was_given() -> None:
    run = _log(
        "s1",
        "B",
        0,
        (0.5, _verdict("POSE_ERROR")),  # no fault yet: a false alarm
        (1.0, _fault("INSERT_STUCK")),
        (1.5, _verdict("INSERT_STUCK")),  # right
        (2.0, _verdict("FORCE_LIMIT")),  # wrong class for the active fault
        (2.5, _fault("INSERT_STUCK", active=False)),
        (3.0, _fault("SENSOR_DROPOUT", fault_id="f#1")),  # never reported: a miss
        (3.5, _fault("SENSOR_DROPOUT", active=False, fault_id="f#1")),
        (9.0, _finish("failed")),
    )

    table = compute_metrics([run], k=1)

    [row] = table.rows
    assert row.classification_accuracy == pytest.approx(1 / 3)
    assert dict(table.confusion) == {
        ("none", "POSE_ERROR"): 1,
        ("INSERT_STUCK", "INSERT_STUCK"): 1,
        ("INSERT_STUCK", "FORCE_LIMIT"): 1,
        ("SENSOR_DROPOUT", "missed"): 1,
    }


def test_with_two_faults_active_a_verdict_matching_either_is_right() -> None:
    run = _log(
        "s1",
        "B",
        0,
        (1.0, _fault("SENSOR_DROPOUT", fault_id="f#0")),
        (1.1, _fault("INSERT_STUCK", fault_id="f#1")),
        (1.5, _verdict("INSERT_STUCK")),
        (9.0, _finish("failed")),
    )

    table = compute_metrics([run], k=1)

    assert table.rows[0].classification_accuracy == 1.0
    # The dropout was active the whole time and got no verdict of its own.
    assert dict(table.confusion) == {
        ("INSERT_STUCK", "INSERT_STUCK"): 1,
        ("SENSOR_DROPOUT", "missed"): 1,
    }


def test_a_fault_that_ended_while_the_failed_skill_was_running_still_explains_its_verdict() -> None:
    # A controller stall from 1.0 to 4.65 s: the skill lost time it cannot make up, and times out
    # at 5.0 s, after the stall has ended. The retry starts after it and fails for another reason.
    run = _log(
        "s1",
        "B",
        0,
        (0.0, SkillStarted(skill="approach", attempt=1)),
        (1.0, _fault("TIMEOUT")),
        (4.65, _fault("TIMEOUT", active=False)),
        (5.0, MonitorVerdict(skill="approach", failure_class="TIMEOUT", rule="r")),
        (5.0, SkillStarted(skill="approach", attempt=2)),
        (6.0, MonitorVerdict(skill="approach", failure_class="POSE_ERROR", rule="r")),
        (9.0, _finish("failed")),
    )

    table = compute_metrics([run], k=1)

    assert dict(table.confusion) == {("TIMEOUT", "TIMEOUT"): 1, ("none", "POSE_ERROR"): 1}


def test_two_overlapping_faults_of_one_kind_are_each_credited_with_a_verdict() -> None:
    run = _log(
        "s1",
        "B",
        0,
        (1.0, _fault("INSERT_STUCK", fault_id="f#0")),
        (1.5, _fault("INSERT_STUCK", fault_id="f#1")),
        (2.0, _verdict("INSERT_STUCK")),
        (3.0, _verdict("INSERT_STUCK")),
        (9.0, _finish("failed")),
    )

    table = compute_metrics([run], k=1)

    assert dict(table.confusion) == {("INSERT_STUCK", "INSERT_STUCK"): 2}


def test_a_fault_still_active_at_the_end_of_the_run_can_be_missed() -> None:
    run = _log("s1", "A", 0, (1.0, _fault("TIMEOUT")), (9.0, _finish("aborted")))

    table = compute_metrics([run], k=1)

    assert table.rows[0].classification_accuracy is None  # no verdicts to judge
    assert dict(table.confusion) == {("TIMEOUT", "missed"): 1}


def test_k_must_be_at_least_one() -> None:
    with pytest.raises(ValueError):
        compute_metrics([], k=0)


# --- gains over A ---------------------------------------------------------------------------


def _recovery() -> RecoveryStarted:
    return RecoveryStarted(
        skill="insert_scripted",
        failure_class="POSE_ERROR",
        action="realign",
        count_for_class=1,
        count_total=1,
    )


def _planner_call() -> PlannerCalled:
    return PlannerCalled(skill="insert_scripted", failure_class="UNKNOWN", call=1)


def test_a_run_that_wins_where_a_lost_is_explained_by_a_recovery_or_a_planner_call() -> None:
    runs = [
        _log("s1", "A", 0, (1.0, _finish("failed"))),
        _log("s1", "B", 0, (0.5, _recovery()), (1.0, _finish())),
        _log("s1", "C", 0, (0.5, _planner_call()), (1.0, _finish())),
        _log("s1", "A", 1, (1.0, _finish("failed"))),
        _log("s1", "B", 1, (1.0, _finish())),  # won, but nothing in its log says why
        _log("s1", "C", 1, (1.0, _finish("failed"))),
        _log("s1", "A", 2, (1.0, _finish())),  # A won too: not a gain
        _log("s1", "B", 2, (1.0, _finish())),
    ]

    gains = compute_metrics(runs, k=1).gains

    assert {g.setting: (g.won, g.unexplained) for g in gains} == {
        "B": (2, ("s1/B/1/a1",)),
        "C": (1, ()),
    }
    assert render_gains(compute_metrics(runs, k=1))[1:] == [
        "  B: 2 won, 1 unexplained",
        "    unexplained: s1/B/1/a1",
        "  C: 1 won, 0 unexplained",
    ]


def test_without_setting_a_there_is_nothing_to_gain_over() -> None:
    runs = [_log("s1", "B", 0, (1.0, _finish()))]

    assert compute_metrics(runs, k=1).gains == ()
    assert render_gains(compute_metrics(runs, k=1)) == []
