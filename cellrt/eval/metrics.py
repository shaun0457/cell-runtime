"""Eval metrics: pure functions over event logs, nothing else.

Each finished run is summarised on its own (`summarize_run`), then the summaries are grouped by
setting (`compute_metrics`). A run without `run_finished` (interrupted) has no summary and is
left out of every metric.

- Unsafe force is read from `signal_sampled`, not from Monitor verdicts, so a force the Monitor
  missed still counts. It follows the AIC penalty: the time with tared force over 20 N, summed
  over the run (not one continuous stretch), exceeding 1 s.
- Classification is judged per Monitor verdict against the faults (ground truth) active at any
  point while the failed skill attempt ran, from its `skill_started` to the verdict: a fault's
  effect can outlast it (a controller stall that ends just before the skill times out). A fault
  that no verdict is attributed to is a miss; a verdict with no such fault is a false alarm.
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
    PlannerCalled,
    RunFinished,
    RunStarted,
    SignalSampled,
    SkillStarted,
)

# The AIC force penalty: tared force over this ...
UNSAFE_FORCE_N = 20.0
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
    over_limit_s: float  # time with a valid tared force over UNSAFE_FORCE_N
    verdicts: tuple[VerdictCheck, ...]
    missed: tuple[str, ...]  # labels of faults no verdict was attributed to

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
class MetricsTable:
    k: int
    rows: tuple[SettingMetrics, ...]
    confusion: Mapping[tuple[str, str], int]  # (truth, predicted or MISSED) -> count


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
    over_limit_s = 0.0
    last_sample_ts = events[0].ts  # the run's clock need not start at 0
    planner_calls = 0
    for event in events:
        match event.payload:
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
            case SignalSampled(force_n=force, wrench_valid=valid):
                # A sample stands for the interval since the previous one.
                if valid and force > UNSAFE_FORCE_N:
                    over_limit_s += event.ts - last_sample_ts
                last_sample_ts = event.ts
            case PlannerCalled():
                planner_calls += 1
            case _:
                pass
    return RunSummary(
        run_id=events[0].run_id,
        scenario_id=started.scenario_id,
        setting=started.setting,
        seed=started.seed,
        outcome=finished.outcome,
        steps=finished.steps,
        duration_s=finished.duration,
        planner_calls=planner_calls,
        over_limit_s=round(over_limit_s, 6),
        verdicts=tuple(verdicts),
        missed=tuple(e.label for e in episodes if not e.attributed),
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
    return MetricsTable(k=k, rows=rows, confusion=dict(confusion))


def _row(setting: str, runs: list[RunSummary], k: int) -> SettingMetrics:
    succeeded = [r.outcome == "success" for r in runs]
    with_verdict = [r for r in runs if r.verdicts]
    verdicts = [v for r in runs for v in r.verdicts]
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
        mean_steps=fmean(r.steps for r in runs),
        mean_duration_s=fmean(r.duration_s for r in runs),
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
