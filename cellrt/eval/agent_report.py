"""The agent report (#51): the planner variants side by side, from one event store each.

Phase 0's table (`metrics.py`) answers "did recovery help?". This one answers what an agent
costs for what it brings: for B, C with the rule planner, C with the fixed policy and C with a
model, how often the task succeeds, how much sim time and how many steps a run spends after its
first UNKNOWN when it fails anyway, how often the planner is called and what it decides, how
often its answer is invalid, and what a call costs in wall time and tokens.

Pure functions over event logs, like `metrics.py`, which this module leaves alone.

- A store holds one variant's runs, in one setting and under one configuration (every part of
  `run_started.config_parts` but the scenario, which differs per scenario). Its label is
  `name` or `name#N`: stores `C-llm#1`, `C-llm#2`, ... are repeats of the variant `C-llm`,
  pooled into one row that also shows the spread of the success rate over the repeats.
- A unit (scenario, setting, seed) counts once: its first finished attempt, as in the eval.
- Wasted: on a run that did not succeed, the sim time from the first UNKNOWN verdict to the end
  of the run, and the skills started after it (steps as in `run_finished.steps`). A run that
  succeeded, or had no UNKNOWN, wastes nothing here.
- Invalid output: an answer that failed validation. A call that raised (an SDK error, the call
  cap) is an error, and a call cut at its deadline a timeout; neither is output.
- Latency and tokens come from the traces of model calls (`planner_responded`); the rule and
  fixed planners leave none, so their columns show `-`.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from statistics import fmean

from cellrt.runtime.events import (
    Event,
    MonitorVerdict,
    PlannerCalled,
    PlannerDecided,
    PlannerOutputRejected,
    PlannerPreempted,
    PlannerResponded,
    PlannerTimedOut,
    RunFinished,
    RunStarted,
    SkillStarted,
)

UNKNOWN = "UNKNOWN"
# How a rejection records a call that raised instead of answering (executor/behaviours.py).
RAISED_PREFIX = "<root>: planner_raised "
MAX_LABEL_NAME = 40
MAX_REPEAT = 9999
_LABEL = re.compile(r"([A-Za-z0-9][A-Za-z0-9._-]*)(?:#([1-9][0-9]*))?")
LABEL_RULE = (
    f"a label is a variant name (letters, digits, '.', '_' or '-', at most {MAX_LABEL_NAME}) "
    "with an optional #N for a repeat"
)


class StoreRefused(ValueError):
    """A store or label the report cannot use. The message names the label, never the data."""


@dataclass(frozen=True)
class AgentRunSummary:
    run_id: str
    scenario_id: str
    setting: str
    seed: int
    attempt: int
    config_hash: str
    # The configuration without the scenario: config_hash covers the scenario too, so an eval's
    # store holds one hash per scenario. A run that recorded no parts falls back to its hash.
    config_key: str
    planner_part: str | None  # the planner's part of the configuration, if recorded
    outcome: str
    wasted_s: float | None  # None: the run succeeded, or had no UNKNOWN
    wasted_steps: int | None
    planner_calls: int
    decisions: tuple[str, ...]  # the kind of every valid decision, in order
    invalid: int  # answers that failed validation
    errors: int  # calls that raised
    timeouts: int  # calls cut at their deadline
    preempted: int  # calls cut short by a Monitor verdict during a live wait (ADR 020)
    latencies_s: tuple[float, ...]  # wall time of each traced call
    prompt_tokens: tuple[int, ...]  # of the traced calls that reported them
    completion_tokens: tuple[int, ...]


@dataclass(frozen=True)
class VariantRow:
    variant: str
    setting: str
    repeats: int
    runs: int
    success_rate: float
    success_spread: tuple[float, float] | None  # min and max over repeats; None for one store
    wasted_s: float | None  # mean over the runs that wasted anything; None: none did
    wasted_steps: float | None
    planner_calls_per_run: float
    decisions: dict[str, int]
    invalid_rate: float | None  # invalid answers per planner call; None: no calls
    errors: int
    timeouts: int
    preempted: int
    latency_p50_s: float | None
    latency_p95_s: float | None
    prompt_tokens_per_call: float | None
    completion_tokens_per_call: float | None


def summarize_agent_run(events: Sequence[Event]) -> AgentRunSummary | None:
    """One run's summary, or None if it did not finish."""
    if not events or not isinstance(events[0].payload, RunStarted):
        return None
    finished = events[-1].payload
    if not isinstance(finished, RunFinished):
        return None
    started = events[0].payload
    skills = 0  # skills started so far: the unit of run_finished.steps
    unknown_at: tuple[float, int] | None = None  # (ts, skills) at the first UNKNOWN verdict
    calls = invalid = errors = timeouts = preempted = 0
    decisions: list[str] = []
    latencies: list[float] = []
    prompt_tokens: list[int] = []
    completion_tokens: list[int] = []
    for event in events:
        match event.payload:
            case SkillStarted():
                skills += 1
            case MonitorVerdict(failure_class=failure_class):
                if failure_class == UNKNOWN and unknown_at is None:
                    unknown_at = (event.ts, skills)
            case PlannerCalled():
                calls += 1
            case PlannerDecided(decision=decision):
                decisions.append(str(decision.get("kind")))
            case PlannerOutputRejected(errors=found):
                if any(e.startswith(RAISED_PREFIX) for e in found):
                    errors += 1
                else:
                    invalid += 1
            case PlannerTimedOut():
                timeouts += 1
            case PlannerPreempted():
                preempted += 1
            case PlannerResponded() as trace:
                latencies.append(trace.latency_s)
                if trace.prompt_tokens is not None:
                    prompt_tokens.append(trace.prompt_tokens)
                if trace.completion_tokens is not None:
                    completion_tokens.append(trace.completion_tokens)
            case _:
                pass
    wasted = finished.outcome != "success" and unknown_at is not None
    return AgentRunSummary(
        run_id=events[0].run_id,
        scenario_id=started.scenario_id,
        setting=started.setting,
        seed=started.seed,
        attempt=started.attempt,
        config_hash=started.config_hash,
        config_key=_config_key(started),
        planner_part=started.config_parts.get("planner"),
        outcome=finished.outcome,
        wasted_s=round(events[-1].ts - unknown_at[0], 6) if wasted and unknown_at else None,
        wasted_steps=skills - unknown_at[1] if wasted and unknown_at else None,
        planner_calls=calls,
        decisions=tuple(decisions),
        invalid=invalid,
        errors=errors,
        timeouts=timeouts,
        preempted=preempted,
        latencies_s=tuple(latencies),
        prompt_tokens=tuple(prompt_tokens),
        completion_tokens=tuple(completion_tokens),
    )


def _config_key(started: RunStarted) -> str:
    parts = {k: v for k, v in started.config_parts.items() if k != "scenario"}
    if not parts:
        return f"hash:{started.config_hash}"
    return "parts:" + ",".join(f"{k}={parts[k]}" for k in sorted(parts))


def percentile(values: Sequence[float], p: float) -> float | None:
    """The nearest-rank percentile: the smallest value with at least p% of the values at or
    below it. None for no values."""
    if isinstance(p, bool) or not 0 < p <= 100:
        raise ValueError("p must be in (0, 100]")
    if not values:
        return None
    ordered = sorted(values)
    return ordered[math.ceil(p / 100 * len(ordered)) - 1]


def parse_label(label: str) -> tuple[str, int]:
    """(variant, repeat) from `name` or `name#N`; refused with a fixed message otherwise."""
    match = _LABEL.fullmatch(label)
    if match is None or len(match.group(1)) > MAX_LABEL_NAME:
        raise StoreRefused(LABEL_RULE)
    repeat = int(match.group(2) or 1)
    if repeat > MAX_REPEAT:
        raise StoreRefused(LABEL_RULE)
    return match.group(1), repeat


def variant_rows(stores: Sequence[tuple[str, Iterable[Sequence[Event]]]]) -> list[VariantRow]:
    """One row per variant, in the order the variants first appear. Each store is a label and
    its runs' events; the runs are read once."""
    seen: set[tuple[str, int]] = set()
    grouped: dict[str, list[list[AgentRunSummary]]] = {}
    for label, runs in stores:
        name, repeat = parse_label(label)
        if (name, repeat) in seen:
            raise StoreRefused(f"{label}: the same variant and repeat given twice")
        seen.add((name, repeat))
        summaries = _counted(label, runs)
        grouped.setdefault(name, []).append(summaries)
    rows = []
    for name, repeats in grouped.items():
        if len({r[0].setting for r in repeats}) > 1:
            raise StoreRefused(f"{name}: its repeats ran in different settings")
        # Repeats may differ in code (a later commit), never in the planner they ask.
        if len({r[0].planner_part for r in repeats}) > 1:
            raise StoreRefused(f"{name}: its repeats ran different planners")
        rows.append(_row(name, repeats))
    return rows


def _counted(label: str, runs: Iterable[Sequence[Event]]) -> list[AgentRunSummary]:
    """The store's finished runs, the first finished attempt of each unit; refused unless
    every finished run in it, counted or not, is of one setting and one configuration: a
    later eval under another planner must not hide behind the units' first attempts."""
    finished = [s for s in map(summarize_agent_run, runs) if s is not None]
    if not finished:
        raise StoreRefused(f"{label}: no finished runs")
    if len({s.setting for s in finished}) > 1:
        raise StoreRefused(f"{label}: a store must hold one setting")
    if len({s.config_key for s in finished}) > 1:
        raise StoreRefused(f"{label}: a store must hold runs of one configuration")
    first: dict[tuple[str, str, int], AgentRunSummary] = {}
    for summary in finished:
        unit = (summary.scenario_id, summary.setting, summary.seed)
        if unit not in first or summary.attempt < first[unit].attempt:
            first[unit] = summary
    return sorted(first.values(), key=lambda s: (s.scenario_id, s.seed))


def _row(name: str, repeats: list[list[AgentRunSummary]]) -> VariantRow:
    runs = [s for repeat in repeats for s in repeat]
    rates = [_success_rate(repeat) for repeat in repeats]
    wasted = [s for s in runs if s.wasted_s is not None]
    calls = sum(s.planner_calls for s in runs)
    latencies = [x for s in runs for x in s.latencies_s]
    prompt = [x for s in runs for x in s.prompt_tokens]
    completion = [x for s in runs for x in s.completion_tokens]
    return VariantRow(
        variant=name,
        setting=runs[0].setting,
        repeats=len(repeats),
        runs=len(runs),
        success_rate=_success_rate(runs),
        success_spread=(min(rates), max(rates)) if len(repeats) > 1 else None,
        wasted_s=fmean(s.wasted_s for s in wasted if s.wasted_s is not None) if wasted else None,
        wasted_steps=(
            fmean(s.wasted_steps for s in wasted if s.wasted_steps is not None) if wasted else None
        ),
        planner_calls_per_run=calls / len(runs),
        decisions=dict(sorted(Counter(k for s in runs for k in s.decisions).items())),
        invalid_rate=sum(s.invalid for s in runs) / calls if calls else None,
        errors=sum(s.errors for s in runs),
        timeouts=sum(s.timeouts for s in runs),
        preempted=sum(s.preempted for s in runs),
        latency_p50_s=percentile(latencies, 50),
        latency_p95_s=percentile(latencies, 95),
        prompt_tokens_per_call=fmean(prompt) if prompt else None,
        completion_tokens_per_call=fmean(completion) if completion else None,
    )


def _success_rate(runs: Sequence[AgentRunSummary]) -> float:
    return sum(s.outcome == "success" for s in runs) / len(runs)


HEADER = (
    "variant", "setting", "runs", "success", "wasted_s", "wasted_steps", "calls/run",
    "invalid", "errors", "timeouts", "preempted", "p50_s", "p95_s", "prompt/call",
    "compl/call",
)  # fmt: skip
NOTES = (
    "success: pooled over the repeats (name#N), with the min-max over repeats in brackets",
    "wasted: mean sim time, and skills started, from the first UNKNOWN to the end of failed runs",
    "invalid: answers that failed validation, per planner call; errors: calls that raised",
    "p50/p95: wall time of traced model calls (nearest rank); tokens: mean per traced call",
    "timed-out and raised calls leave no trace, so p50/p95 leave them out: see those columns",
    "preempted: calls a Monitor verdict cut short while the world stepped on (live mode)",
)


def render_agent_report(rows: Sequence[VariantRow]) -> list[str]:
    """The table, the decisions per variant, and notes on what the columns mean."""
    cells = [list(HEADER)] + [_cells(r) for r in rows]
    widths = [max(len(row[i]) for row in cells) for i in range(len(HEADER))]
    lines = [
        "  ".join(
            cell.ljust(width) if i < 2 else cell.rjust(width)
            for i, (cell, width) in enumerate(zip(row, widths, strict=True))
        ).rstrip()
        for row in cells
    ]
    lines += ["", "decisions (valid ones, by kind):"]
    for r in rows:
        kinds = ", ".join(f"{kind} {n}" for kind, n in r.decisions.items()) or "-"
        lines.append(f"  {r.variant}: {kinds}")
    lines += ["", *(f"note: {n}" for n in NOTES)]
    return lines


def _cells(r: VariantRow) -> list[str]:
    success = f"{r.success_rate:.1%}"
    if r.success_spread is not None:
        low, high = r.success_spread
        success += f" ({low * 100:.1f}-{high:.1%})"
    return [
        r.variant,
        r.setting,
        str(r.runs),
        success,
        _num(r.wasted_s, "{:.2f}"),
        _num(r.wasted_steps, "{:.1f}"),
        f"{r.planner_calls_per_run:.2f}",
        _num(r.invalid_rate, "{:.1%}"),
        str(r.errors),
        str(r.timeouts),
        str(r.preempted),
        _num(r.latency_p50_s, "{:.2f}"),
        _num(r.latency_p95_s, "{:.2f}"),
        _num(r.prompt_tokens_per_call, "{:.0f}"),
        _num(r.completion_tokens_per_call, "{:.0f}"),
    ]


def _num(value: float | None, fmt: str) -> str:
    return "-" if value is None else fmt.format(value)
