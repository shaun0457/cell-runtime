# 018: Escalation to an operator

Date: 2026-11-09
Status: accepted

Amends ADR 008 (the decisions a planner may return).

## Context

ADR 008 gives the planner three decisions: `switch_skill(skill)`, `retry` and `abort(reason)`.
Abort says "this task cannot be done". It cannot say "something here needs a person": a port
that looks damaged, a failure the planner has never seen, evidence that does not add up. In a
real cell those are different outcomes. An abort moves on to the next task. An escalation stops
and waits for an operator, and the operator's time is the cost.

An LLM planner (#45) can tell these apart better than a rule can, so the decision should
exist. It must not change what the eval counts for Phase 0. Outcomes are `success`, `failed`
and `aborted` (`Outcome` in `cellrt/runtime/events.py`); the metrics (ADR 009), the
committed baseline and its gate (#22) read them.

## Options

### 1. How an escalation ends the run

#### Option A — a fourth decision; the run ends `aborted`, its reason says "escalated"
`escalate(reason)` is validated like `abort`: 1 to 200 printable characters, scrubbed, no
control or direction characters. The run ends `aborted` with "planner escalated: <reason>";
`planner_decided` records kind `escalate`.
- Pros: outcomes, metrics and the baseline keep their meaning; the event log still tells an
  escalation from an abort (the decision's kind in `planner_decided`), so a report can count
  both.
- Cons: a metric that only reads outcomes cannot see escalations; a report has to read the
  decision.

#### Option B — a new outcome, `escalated`
- Pros: an escalation is visible everywhere an outcome is.
- Cons: every reader of outcomes changes (metrics, report, baseline format). The baseline and
  its gate are a one-way door (`.claude/one-way-doors`), so the change needs Shaun's approval
  for a gain only the report needs.

#### Option C — no escalate; abort covers it
- Pros: nothing to build.
- Cons: the planner cannot express the one decision a person most needs to see, and the eval
  cannot ask whether a model knows when to stop and call someone.

### 2. What an escalation costs

#### Option A — nothing extra: it ends the run like an abort
- Pros: simple; the eval compares when each planner stopped and why.
- Cons: in a real cell an escalation costs operator time, which the mock does not model; a
  planner that escalates everything would look as good as one that aborts early.

#### Option B — a fixed cost (time or a penalty in the metrics)
- Pros: discourages escalating everything.
- Cons: an invented number; changes the metrics.

## Decision

<!-- Decision and Why drafted by Claude. Shaun delegated these sections to Claude on 2026-11-09 and accepts them by merging; he may rewrite any of it. -->

Option 1A, Option 2A.

- `escalate(reason)` is a fourth decision, validated like `abort`. The model sees it in the
  `decide` tool and in the prompt.
- An escalation ends the run `aborted` with "<skill> failed: UNKNOWN (planner escalated:
  <reason>)". No recovery starts.
- The event schema does not change: `planner_decided` already stores the decision as a mapping.
  The prompt version changes, so model runs before and after this change have different
  config hashes.
- The rule planner never escalates, so Phase 0's results and the baseline are unchanged.

## Why

The point of a planner for UNKNOWN failures is judgement about what the rules do not cover,
and the most useful judgement is often "a person should look at this". Making it a decision,
not a new outcome, keeps every Phase 0 number and the baseline gate as they are. The log still
separates an escalation from an abort, which is all a report needs.

No cost is charged yet because any number would be invented. Reports count escalations
separately, so a planner that escalates everything shows up there.

## Consequences

- Reports must count escalations from `planner_decided` (kind `escalate`), next to aborts
  (#51).
- Escalation is terminal: there is no operator in the loop, and nothing resumes after one.
  Resuming a task after an operator acts is out of scope for Phase 3.
- If escalations need a cost later, Option 2B is the place to start. It changes the metrics,
  so it would need its own decision and flag.
