"""Eval metrics: pure functions over event logs, nothing else.

Each finished run is summarised on its own (`summarize_run`), then the summaries are grouped by
setting (`compute_metrics`). A run without `run_finished` (interrupted) has no summary and is
left out of every metric.

- Unsafe force is read from `signal_sampled` through the fold (`RunState.over_limit_s`), not
  from Monitor verdicts, so a force the Monitor missed still counts. It follows the AIC penalty:
  the time with tared force over 20 N, summed over the run (not one continuous stretch),
  exceeding 1 s. The Monitor reads the same folded sum.
- Planner calls and recoveries are the fold's counts too: one definition for the runtime's
  budgets and the metrics.
- Classification is judged per Monitor verdict against the faults (ground truth) active at any
  point while the failed skill attempt ran, from its `skill_started` to the verdict: a fault's
  effect can outlast it (a controller stall that ends just before the skill times out). A fault
  that no verdict is attributed to is a miss; a verdict with no such fault is a false alarm.
- A gain is a (scenario, seed) that a setting wins and setting A loses. It is explained when the
  winning run's log has a `recovery_started` or a `planner_called`: the extra success came from
  recovery, which is the only thing the settings differ in.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from statistics import fmean

from cellrt.runtime.events import (
    Event,
    FaultCleared,
    FaultInjected,
    MonitorVerdict,
    RunFinished,
    RunStarted,
    SkillStarted,
    TaskPlanRejected,
)
from cellrt.runtime.state import FORCE_LIMIT_N, fold

# The AIC force penalty: tared force over this ...
UNSAFE_FORCE_N = FORCE_LIMIT_N
# ... for longer than this in total over the task.
UNSAFE_BUDGET_S = 1.0
# Truth for a verdict given while no fault was active.
NO_FAULT = "none"
# Confusion-matrix column for a fault that no verdict was attributed to.
MISSED = "missed"


@dataclass(frozen=True)
class VerdictCheck:
    truth: str  # the label of the fault the verdict is attributed to, or NO_FAULT
    predicted: str  # the class the Monitor gave


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    scenario_id: str
    setting: str
    seed: int
    outcome: str
    steps: int
    duration_s: float
    planner_calls: int
    recoveries: int  # recovery_started events
    over_limit_s: float  # time with a tared force over UNSAFE_FORCE_N (the fold's sum)
    verdicts: tuple[VerdictCheck, ...]
    missed: tuple[str, ...]  # labels of faults no verdict was attributed to
    # The task plan was rejected, so nothing moved: a failed task, but no steps or time to
    # average (#63 review F3).
    plan_rejected: bool

    @property
    def unsafe(self) -> bool:
        return self.over_limit_s > UNSAFE_BUDGET_S


@dataclass(frozen=True)
class SettingMetrics:
    setting: str
    runs: int
    success_rate: float
    pass_k: float | None  # None: no scenario has k runs
    recovery_rate: float | None  # None: no run had a verdict
    unsafe_runs: int
    mean_over_limit_s: float
    mean_steps: float
    mean_duration_s: float
    planner_calls_per_task: float
    classification_accuracy: float | None  # None: no verdicts


@dataclass(frozen=True)
class Gains:
    """The (scenario, seed) pairs a setting wins and setting A loses."""

    setting: str
    won: int
    unexplained: tuple[str, ...]  # run ids of wins with no recovery or planner call in the log


@dataclass(frozen=True)
class MetricsTable:
    k: int
    rows: tuple[SettingMetrics, ...]
    confusion: Mapping[tuple[str, str], int]  # (truth, predicted or MISSED) -> count
    gains: tuple[Gains, ...]  # one per setting other than A, sorted; empty without A


def pass_hat_k(n: int, c: int, k: int) -> float | None:
    """The chance that k runs drawn without replacement from n runs with c successes all
    succeed: C(c, k) / C(n, k). None when there are fewer than k runs."""
    if k < 1 or c < 0 or c > n:
        raise ValueError(f"need k >= 1 and 0 <= c <= n (n={n}, c={c}, k={k})")
    if n < k:
        return None
    return math.comb(c, k) / math.comb(n, k)


def summarize_run(events: Sequence[Event]) -> RunSummary | None:
    """One run's summary, or None if the run did not finish."""
    if not events or not isinstance(events[0].payload, RunStarted):
        return None
    finished = events[-1].payload
    if not isinstance(finished, RunFinished):
        return None
    started = events[0].payload
    episodes: list[_Episode] = []  # in the order the faults were injected
    open_episodes: dict[str, _Episode] = {}  # fault_id -> its episode, while active
    skill_started_ts: dict[str, float] = {}  # skill -> when its latest attempt started
    verdicts: list[VerdictCheck] = []
    plan_rejected = False
    for event in events:
        match event.payload:
            case TaskPlanRejected():
                plan_rejected = True
            case FaultInjected(fault_id=fid, label=label):
                open_episodes[fid] = episode = _Episode(label, event.ts)
                episodes.append(episode)
            case FaultCleared(fault_id=fid):
                if (episode := open_episodes.pop(fid, None)) is not None:
                    episode.end_ts = event.ts
            case SkillStarted(skill=skill):
                skill_started_ts[skill] = event.ts
            case MonitorVerdict(skill=skill, failure_class=predicted):
                since = skill_started_ts.get(skill, event.ts)
                episode = _attribute(predicted, [e for e in episodes if e.overlaps(since)])
                if episode is not None:
                    episode.attributed = True
                verdicts.append(VerdictCheck(episode.label if episode else NO_FAULT, predicted))
            case _:
                pass
    state = fold(events)
    return RunSummary(
        run_id=events[0].run_id,
        scenario_id=started.scenario_id,
        setting=started.setting,
        seed=started.seed,
        outcome=finished.outcome,
        steps=finished.steps,
        duration_s=finished.duration,
        planner_calls=state.planner_calls,
        recoveries=state.recoveries_total,
        over_limit_s=round(state.over_limit_s, 6),
        verdicts=tuple(verdicts),
        missed=tuple(e.label for e in episodes if not e.attributed),
        plan_rejected=plan_rejected,
    )


@dataclass
class _Episode:
    """One fault from injection to clearing (end_ts None: still active)."""

    label: str
    start_ts: float
    end_ts: float | None = None
    attributed: bool = False  # some verdict was attributed to it

    def overlaps(self, since: float) -> bool:
        """Active at some point from `since` to now (events arrive in time order)."""
        return self.end_ts is None or self.end_ts >= since


def _attribute(predicted: str, candidates: list[_Episode]) -> _Episode | None:
    """The fault a verdict is about, among those active while the failed skill attempt ran: one
    with the predicted label (one no verdict was attributed to yet first, so two overlapping
    faults of one kind can both be credited), else the earliest injected."""
    matching = [e for e in candidates if e.label == predicted]
    for episode in [*(e for e in matching if not e.attributed), *matching, *candidates]:
        return episode
    return None


def compute_metrics(runs: Iterable[Sequence[Event]], k: int) -> MetricsTable:
    """The metrics table for every finished run, one row per setting (sorted by name)."""
    if k < 1:
        raise ValueError("k must be at least 1")
    by_setting: dict[str, list[RunSummary]] = defaultdict(list)
    confusion: Counter[tuple[str, str]] = Counter()
    for events in runs:
        summary = summarize_run(events)
        if summary is None:
            continue
        by_setting[summary.setting].append(summary)
        confusion.update((v.truth, v.predicted) for v in summary.verdicts)
        confusion.update((label, MISSED) for label in summary.missed)
    rows = tuple(_row(setting, by_setting[setting], k) for setting in sorted(by_setting))
    return MetricsTable(k=k, rows=rows, confusion=dict(confusion), gains=_gains(by_setting))


def _gains(by_setting: Mapping[str, list[RunSummary]]) -> tuple[Gains, ...]:
    """For each setting other than A, the (scenario, seed) pairs it wins and A loses."""
    if "A" not in by_setting:
        return ()
    a_lost = {(r.scenario_id, r.seed) for r in by_setting["A"] if r.outcome != "success"}
    gains = []
    for setting in sorted(by_setting.keys() - {"A"}):
        won = [
            r
            for r in by_setting[setting]
            if r.outcome == "success" and (r.scenario_id, r.seed) in a_lost
        ]
        unexplained = tuple(r.run_id for r in won if not (r.recoveries or r.planner_calls))
        gains.append(Gains(setting=setting, won=len(won), unexplained=unexplained))
    return tuple(gains)


def _row(setting: str, runs: list[RunSummary], k: int) -> SettingMetrics:
    succeeded = [r.outcome == "success" for r in runs]
    with_verdict = [r for r in runs if r.verdicts]
    verdicts = [v for r in runs for v in r.verdicts]
    # Steps and time are averaged over the runs that moved; a rejected plan has neither. All of
    # them rejected: the means are those of the runs, zero.
    moved = [r for r in runs if not r.plan_rejected] or runs
    return SettingMetrics(
        setting=setting,
        runs=len(runs),
        success_rate=sum(succeeded) / len(runs),
        pass_k=_pass_k_over_scenarios(runs, k),
        recovery_rate=(
            sum(r.outcome == "success" for r in with_verdict) / len(with_verdict)
            if with_verdict
            else None
        ),
        unsafe_runs=sum(r.unsafe for r in runs),
        mean_over_limit_s=fmean(r.over_limit_s for r in runs),
        mean_steps=fmean(r.steps for r in moved),
        mean_duration_s=fmean(r.duration_s for r in moved),
        planner_calls_per_task=fmean(r.planner_calls for r in runs),
        classification_accuracy=(
            sum(v.truth == v.predicted for v in verdicts) / len(verdicts) if verdicts else None
        ),
    )


def _pass_k_over_scenarios(runs: list[RunSummary], k: int) -> float | None:
    """pass^k per scenario, averaged over the scenarios that have at least k runs."""
    by_scenario: dict[str, list[bool]] = defaultdict(list)
    for r in runs:
        by_scenario[r.scenario_id].append(r.outcome == "success")
    values = [
        value
        for outcomes in by_scenario.values()
        if (value := pass_hat_k(len(outcomes), sum(outcomes), k)) is not None
    ]
    return fmean(values) if values else None
