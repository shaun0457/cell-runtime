"""The Phase 0 planner: fixed rules behind the planner protocol (an LLM takes its place in
Phase 3 without changing the executor). It answers in the raw form an LLM would, so its output
goes through the same validation."""

from __future__ import annotations

from dataclasses import dataclass

from cellrt.planner.base import FailureContext


@dataclass(frozen=True)
class RulePlanner:
    """UNKNOWN in insert_scripted: switch to insert_act. UNKNOWN in insert_act: retry it once.
    Anything else: abort."""

    async def triage(self, ctx: FailureContext) -> object:
        if ctx.failure_class != "UNKNOWN":
            return {"kind": "abort", "reason": f"no rule for {ctx.failure_class}"}
        if ctx.skill == "insert_scripted" and "insert_act" in ctx.catalogue:
            return {"kind": "switch_skill", "skill": "insert_act"}
        if ctx.skill == "insert_act" and ctx.attempt == 1:
            return {"kind": "retry"}
        return {"kind": "abort", "reason": f"no rule for UNKNOWN in {ctx.skill}"}
