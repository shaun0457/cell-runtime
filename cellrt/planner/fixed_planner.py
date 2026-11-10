"""A fixed retry-then-abort policy behind the planner protocol (ADR 019).

It is the simple policy a model planner is compared with: it never reads the evidence. It
retries the failed skill while more than one planner call and more than one recovery are left,
and aborts otherwise. With the default budgets (2 planner calls, 4 recoveries per task), that
is: retry on the first call, abort on the second. It stops by its own decision before either
budget runs out, so a run it gives up on ends `aborted`, never `failed` with a budget used up.
(With a planner budget of 0 it is never asked.)

It keeps no state: the calls left come from the failure context. One instance serves every run
of an eval and answers the same way each time. It has no settings either, so its type, which
`config_hash` records, describes it fully. Like the rule planner it answers in raw form, so its
output goes through the same validation as a model's.
"""

from __future__ import annotations

from dataclasses import dataclass

from cellrt.planner.base import FailureContext

ABORT_REASON = "fixed policy: abort on the last planner call"


@dataclass(frozen=True)
class FixedPlanner:
    """Retry while more than one planner call and more than one recovery are left; else abort."""

    async def triage(self, ctx: FailureContext) -> object:
        # A retry spends a recovery: on the last one, the next failure would end the task with
        # the budget used up instead of by this policy's decision.
        if ctx.planner_calls_left > 1 and ctx.recoveries_left > 1:
            return {"kind": "retry"}
        return {"kind": "abort", "reason": ABORT_REASON}
