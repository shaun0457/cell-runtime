# 008: How setting C hands unclassified failures to a planner

Date: 2026-11-08
Status: proposed

<!-- DRAFT by Claude: Context, Options and Consequences only. Decision and Why are Shaun's. -->

## Context

Setting C is setting B plus a planner for what B cannot handle: a failure the Monitor classified
as UNKNOWN (ADR 006). In Phase 0 the planner is a rule-based stub; in Phase 3 it becomes an LLM.
Design principle 4 says planner output is untrusted: it is validated against a schema and the
skill registry before anything runs, and invalid output is an event, not a crash. The planner
must not change what B does on known classes, so that C − B measures only the planner.

Current shape:

- `Planner` protocol: `async triage(FailureContext) -> object`. The context holds the failed
  skill and attempt, the class, the Monitor rule and its evidence, time left, planner calls and
  recoveries left, and a catalogue of registered skills with their Monitor profiles. The return
  value is raw (a dict or a JSON string), so a fake, a rule planner and an LLM share one path.
- `validate_decision` turns raw output into a `Decision` (`switch_skill(skill)`, `retry`,
  `abort(reason)`; pydantic, `extra="forbid"`, discriminator `kind`) or a `Rejection` holding the
  raw text (cut to 500 characters) and error codes (`field: type`; a key the planner made up
  shows as `<extra>`, so nothing the planner wrote is echoed in a code). Text over 16,000
  characters, or nested more than 8 brackets deep, is rejected unparsed (`too_long`,
  `too_deep`): parsing deep input would hit a recursion limit that differs by platform.
  Validation never raises (a hostile `repr` included).
  A switch must name a registered skill, not the failed one, and one of the step's options.
  An abort reason is at most 200 printable characters: it becomes the run's outcome reason.
- Only insert steps get a planner in Phase 0; their options are the other registered insert
  skills. Only a Monitor-sourced UNKNOWN reaches it; a class a skill reported itself, or a skill
  that raised, does not.
- `PlannerBehaviour` runs `triage` as a task on the same bridge as skills (ADR 003) and records
  `planner_called` (with the context), then `planner_decided` or `planner_output_rejected`.
- Running a retry or a switch records `recovery_started` (class UNKNOWN, action `retry` or
  `switch_skill:<name>`) and counts against the task recovery budget (4). Planner calls have
  their own budget, 2 per task, checked against `RunState.planner_calls` folded from the log.
- `abort` and a rejection end the run as `aborted` with a reason; an exhausted planner budget
  ends it as `failed`.
- A skill the planner switched to is a full member of the step: if it then fails with a known
  class, B's plan applies to it (a FORCE_LIMIT reruns it at half speed).
- In setting C, `config_hash` also covers the planner's type and budget; B's hash does not
  include the planner budget.

## Options

### 1. What the planner returns

#### Option A — raw output, validated by the executor
- Pros: one validation path for every planner; the stub cannot bypass checks an LLM must pass;
  the audit trail records exactly what the planner said.
- Cons: the stub has to produce dicts instead of typed objects; type checkers see `object`.

#### Option B — a typed `Decision`, each planner validates its own output
- Pros: simpler types; no parsing in the executor.
- Cons: validation depends on every planner doing it; an LLM wrapper that forgets breaks
  principle 4 silently.

### 2. What a switch may name

#### Option A — an explicit option list per step
- Pros: the tree states what may replace what; a planner cannot move a motion step into a verify
  step; easy to explain from the tree alone.
- Cons: options must be built per step; adding a skill means deciding which steps offer it.

#### Option B — any registered skill with the same Monitor profile
- Pros: no list to maintain.
- Cons: profile is about how the Monitor watches a skill, not about what a step needs (approach
  and align are both `motion` but are not interchangeable).

#### Option C — any registered skill
- Pros: most freedom for an LLM.
- Cons: the planner can skip steps or reorder the task; preconditions are the only guard.

### 3. What happens to invalid output

#### Option A — the run is aborted
- Pros: an untrusted component that misbehaves stops the task; easy to count in the eval.
- Cons: a single malformed answer loses a run that a second call might have saved.

#### Option B — ask again within the planner budget
- Pros: more forgiving of an LLM that sometimes breaks the format.
- Cons: hides how often the planner is wrong; spends budget and time on retries.

#### Option C — treat it as "no recovery" (the run fails like in B)
- Pros: C is never worse than B on a run.
- Cons: blurs a planner fault with an ordinary failure in the metrics.

### 4. Budgets

#### Option A — a planner budget (2 per task) plus the shared task recovery budget (4)
- Pros: bounded cost and time per task; decisions the planner makes are counted like any other
  recovery, so a planner cannot loop the task.
- Cons: two limits to tune and explain.

#### Option B — only the shared task recovery budget
- Pros: one limit.
- Cons: an abort or a rejection does not spend a recovery, so planner calls would be unbounded
  in principle; cost per task (an LLM call) is not capped.

## Decision

<!-- Written by Shaun. -->

## Why

<!-- Written by Shaun: the reason in your own words, as you would explain it in an interview. -->

## Consequences

- C equals B on every known-class scenario (20 seeds: same outcome, no planner calls), so the
  difference between C and B comes only from UNKNOWN failures. In the Phase 0 sweep that is the
  yaw mismatch: B 0/20, C 20/20, one planner call per run.
- The Phase 0 result says little about an LLM: the rule planner knows the one UNKNOWN fault the
  mock has. What it does show is that the plumbing (context, validation, budgets, events) holds,
  and the malformed-output tests show that bad output is contained.
- While the planner runs, the world keeps stepping and signals are recorded, but no skill is
  active, so the Monitor watches nothing; the arm holds (the failed skill's access was revoked
  and HOLD sent), and the task time limit still applies. A planner timeout, late answers, and what the supervisor does during a slow LLM
  call are open for Phase 3 (#14).
- The hash names the planner type, not how an instance is set up: two LLM planners of one type
  with different models or prompts would share a hash. Phase 3 needs a planner config in it.
- Adding a new insert skill adds it to every insert step's options automatically; adding a
  planner to other steps needs a decision about their options.
