"""One readable line per event, for `cellrt replay`."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from cellrt.runtime.events import (
    BtNodeChanged,
    CommandDropped,
    Event,
    FaultCleared,
    FaultInjected,
    HoldChecked,
    MonitorVerdict,
    PlannerCalled,
    PlannerDecided,
    PlannerOutputRejected,
    PlannerPreempted,
    PlannerResponded,
    PlannerTimedOut,
    PreconditionFailed,
    PromotedStopped,
    RecoveryStarted,
    RunFinished,
    RunStarted,
    SignalSampled,
    SkillCancelled,
    SkillFinished,
    SkillProgress,
    SkillStarted,
    TaskPlanned,
    TaskPlanRejected,
    TaskPlanResponded,
)


def replay_lines(events: Iterable[Event], *, signals: bool = False) -> Iterator[str]:
    for event in events:
        if event.type == "signal_sampled" and not signals:
            continue
        yield f"{event.seq:>5} {event.ts:>8.3f} {event.type:<16} {describe(event)}"


def describe(event: Event) -> str:
    match event.payload:
        case RunStarted() as p:
            return f"{p.scenario_id} setting={p.setting} seed={p.seed} attempt={p.attempt}"
        case TaskPlanResponded() as p:
            tokens = f"{_count(p.prompt_tokens)}+{_count(p.completion_tokens)}"
            return (
                f"{p.planner} {p.provider}/{p.model} {p.latency_s:.2f}s tokens={tokens} "
                f"finish={p.finish_reason or '?'}"
            )
        case TaskPlanned() as p:
            return f"{p.planner}: {' -> '.join(p.steps)}"
        case TaskPlanRejected() as p:
            return f"{p.planner} rejected: {', '.join(p.errors)}"
        case SignalSampled() as p:
            # force is the tared magnitude; over20 is the step's time over 20 N where the world
            # reads the F/T stream faster than it samples (the AIC cell), absent in the mock.
            over = "" if p.force_over_limit_s is None else f" over20={p.force_over_limit_s:.3f}s"
            dropped = f" dropped={p.wrench_dropped}" if p.wrench_dropped else ""
            return (
                f"height={p.height_mm:.2f}mm depth={p.depth_mm:.2f}mm "
                f"xy={p.xy_error_mm:.3f}mm force={p.force_n:.2f}N{over} "
                f"valid={p.wrench_valid}{dropped}"
            )
        case SkillStarted() as p:
            params = f" params={p.params}" if p.params else ""
            return f"{p.skill} attempt={p.attempt}{params}"
        case PreconditionFailed() as p:
            return f"{p.skill} attempt={p.attempt} not started: preconditions not met"
        case MonitorVerdict() as p:
            return f"{p.skill} {p.failure_class} rule={p.rule} {p.evidence}"
        case FaultInjected() as p:
            return f"{p.fault_id} label={p.label} {p.params}"
        case FaultCleared() as p:
            return f"{p.fault_id} label={p.label}"
        case SkillProgress() as p:
            return f"{p.skill} {p.fraction:.0%}"
        case SkillFinished() as p:
            verdict = "ok" if p.ok else f"FAILED failure={p.failure}"
            return f"{p.skill} {verdict} {p.detail}"
        case SkillCancelled() as p:
            return f"{p.skill} ({p.reason})"
        case CommandDropped() as p:
            return f"{p.skill} attempt={p.attempt} commanded after its access was revoked (dropped)"
        case RecoveryStarted() as p:
            return (
                f"{p.skill} {p.failure_class} -> {p.action} "
                f"(class {p.count_for_class}, total {p.count_total})"
                + (" [class from skill]" if p.source == "skill" else "")
            )
        case PromotedStopped() as p:
            return f"{p.skill} UNKNOWN -> promoted {p.rule}: {p.action}"
        case PlannerCalled() as p:
            return f"{p.skill} {p.failure_class} (call {p.call})"
        case PlannerResponded() as p:
            tokens = f"{_count(p.prompt_tokens)}+{_count(p.completion_tokens)}"
            return (
                f"{p.skill} {p.provider}/{p.model} {p.latency_s:.2f}s tokens={tokens} "
                f"finish={p.finish_reason or '?'}"
            )
        case PlannerTimedOut() as p:
            return (
                f"{p.skill} no answer within {p.deadline_s:.2f}s "
                f"(waited {p.waited_s:.2f}s); call cancelled"
            )
        case PlannerPreempted() as p:
            return (
                f"{p.skill} call cut short by {p.failure_class} after {p.waited_sim_s:.2f}s "
                f"(wall {p.waited_s:.2f}s); call cancelled"
            )
        case PlannerDecided() as p:
            # 0: a frozen call the loop waited on, or a row from before schema 10
            after = f" after {p.waited_sim_s:.2f}s" if p.waited_sim_s else ""
            return f"{p.skill} -> {p.decision}{after}"
        case PlannerOutputRejected() as p:
            return f"{p.skill} rejected: {', '.join(p.errors)}"
        case BtNodeChanged() as p:
            return f"{p.node} -> {p.status}"
        case HoldChecked() as p:
            return (
                f"{p.result} (max {p.max_speed_mm_s:.1f}mm/s, {p.samples} samples, "
                f"{p.waited_s:.2f}s)"
            )
        case RunFinished() as p:
            return f"{p.outcome}: {p.reason} (steps={p.steps}, duration={p.duration:.2f}s)"
    raise AssertionError(f"no description for {event.type}")  # pragma: no cover


def _count(tokens: int | None) -> str:
    return "?" if tokens is None else str(tokens)
