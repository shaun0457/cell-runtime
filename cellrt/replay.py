"""One readable line per event, for `cellrt replay`."""

from __future__ import annotations

from collections.abc import Iterable, Iterator

from cellrt.runtime.events import (
    BtNodeChanged,
    Event,
    FaultCleared,
    FaultInjected,
    MonitorVerdict,
    PreconditionFailed,
    RecoveryStarted,
    RunFinished,
    RunStarted,
    SignalSampled,
    SkillCancelled,
    SkillFinished,
    SkillProgress,
    SkillStarted,
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
        case SignalSampled() as p:
            return (
                f"height={p.height_mm:.2f}mm depth={p.depth_mm:.2f}mm "
                f"xy={p.xy_error_mm:.3f}mm force={p.force_n:.2f}N valid={p.wrench_valid}"
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
        case RecoveryStarted() as p:
            return (
                f"{p.skill} {p.failure_class} -> {p.action} "
                f"(class {p.count_for_class}, total {p.count_total})"
                + (" [class from skill]" if p.source == "skill" else "")
            )
        case BtNodeChanged() as p:
            return f"{p.node} -> {p.status}"
        case RunFinished() as p:
            return f"{p.outcome}: {p.reason} (steps={p.steps}, duration={p.duration:.2f}s)"
    raise AssertionError(f"no description for {event.type}")  # pragma: no cover
