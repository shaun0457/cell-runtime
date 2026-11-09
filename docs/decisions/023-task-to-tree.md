# 023: How a task becomes a tree, and what checks it before it runs

Date: 2026-11-10
Status: proposed

## Context

Every task runs the same tree. `build_tree` (`cellrt/executor/trees.py`) takes the fixed
`SKILL_SEQUENCE = ("approach", "align", "insert_scripted", "verify")` and wraps it in the
setting's recovery; the `Task` that arrives (plug type, port type, target) is recorded in
`run_started` and changes nothing. A setting C run on the rotated plug, selected lines
(`cellrt run --scenarios eval/scenarios/phase3.yaml --scenario yaw_mismatch --setting C --seed 0`,
then `cellrt replay` on its run id; the lines between these, the injected fault among them, are
left out):

```text
    2    0.000 skill_started    approach attempt=1
   67    2.800 skill_started    align attempt=1
  100    3.950 skill_started    insert_scripted attempt=1
  172    7.200 monitor_verdict  insert_scripted UNKNOWN rule=insert_stalled {...}
  174    7.200 planner_called   insert_scripted UNKNOWN (call 1)
  178    7.250 planner_decided  insert_scripted -> {'kind': 'switch_skill', 'skill': 'insert_act'}
  180    7.250 skill_started    insert_act attempt=1
```

Here the failure is an injected fault, which no task description could predict. When the need
for another insert skill follows from the task itself (a plug type that `insert_scripted`
cannot seat), the fixed tree pays that stall and that planner call on every task of the type,
and setting B, which has no planner, fails it every time.

The Phase 3 brief's second LLM role reads "from an AIC `Task` (plug and port type, target),
produce a behaviour tree that uses only registered skills; validated against a schema and the
skill registry before it runs. This is LLM-driven program generation with a verifier in front
of execution." Its first draft exit criterion asks that invalid output be an event, never a
crash. This ADR decides what the model may write, what checks it, when it runs, and what an
invalid answer does.

Usual practice: classical planning describes each action by its preconditions and effects
(STRIPS, PDDL) and checks a plan by applying them in order: a plan validator (VAL, for PDDL)
accepts a plan only if every action's preconditions hold when it runs and the goal holds at
the end. Some work that puts a language model into planning keeps that split, the model
proposing and a symbolic check deciding (LLM+P hands the problem to a classical planner;
LLM-Modulo surrounds the model's plans with external verifiers); other work runs what the
model writes without such a check (SayCan, Code as Policies). On the industrial side, the
procedure a product follows is chosen per product from recipes defined ahead of time (ISA-88,
for batch processes); the nearest thing in this cell is a table keyed by plug type: a lookup,
no model.

Constraints:

- Known failures are handled deterministically (principle 1): recovery belongs to the setting
  (A, B, C), its table and its budgets (ADR 007), not to a generated program.
- LLM output is untrusted (principle 4); the model never sits in the control loop.
- The default must add no config part and no event to any run, so the committed baseline and
  #52's results read the same (ADR 010); the `code` part changes with any source edit, as for
  every change.
- Same scenario and seed give the same result in mock mode (principle 5): replay reads the
  plan from the log.

## Options

### 1. What the model writes

#### Option A — a plan: an ordered list of registered skills
`{"steps": ["approach", "align", "insert_act", "verify"]}`, 1 to 8 names. The tree builder
wraps each step in the setting's recovery, as it does for `SKILL_SEQUENCE` today.
- Pros: the language is small enough to check completely; recovery stays the setting's, so
  settings A, B and C keep their meaning with any plan; the tree builders change by one
  parameter.
- Cons: no control flow; a task that needs a branch ("if the latch is felt, then...") cannot
  be written.

#### Option B — a behaviour tree in JSON (sequence, fallback, retry, parallel nodes)
- Pros: what "task to tree" says literally; any structure py_trees can run.
- Cons: the model would write recovery (fallbacks, retries) that the Monitor, the recovery
  table and the budgets already own, and two recovery layers would interact in ways no
  setting measures; checking a tree with fallbacks needs a model of which branch runs.

#### Option C — code (Python) the runtime executes
- Pros: anything is expressible.
- Cons: cannot be checked before it runs; the opposite of principle 4.

### 2. What checks a plan

#### Option A — the schema and the registry
- Pros: simple; catches an unknown skill.
- Cons: accepts `["verify"]`, or insert before align: plans that are well-formed and wrong.

#### Option B — A, plus a step contract per skill, applied in order
Each plannable skill needs one fact and gives one: `approach` gives `at_port`; `align` needs
`at_port` and gives `aligned`; an insertion skill needs `aligned` and gives `inserted`;
`verify` needs `inserted` and gives `verified`. A plan is valid when every step's need was
given by an earlier step, no fact is given twice, and `verified` is given.
- Pros: the usual precondition/effect check, on four facts; each refusal has a code that names
  the step by position, never by the planner's text (`empty`, `too_long`, `not_a_name`,
  `unregistered`, `no_contract`, `needs_unmet`, `repeated`, `goal_not_reached`;
  `planner_timed_out`, and `planner_error` with the exception type for a planner that raised).
- Cons: the facts are ours and coarse; they say a plan is in a sensible order, not that it
  will work.

#### Option C — B, plus a dry run of the plan in the mock world
- Pros: catches a plan that is in order but fails.
- Cons: the mock is the world the eval measures, so a dry run would make the eval measure the
  dry run; useless in the sim, where there is no second world to try in.

### 3. Where the step contracts live

#### Option A — a table next to the verifier, keyed by skill name
- Pros: no change to the `Skill` protocol or to any skill; the mock and the AIC skills share
  names. A skill with no row cannot be planned (`no_contract`), so a new skill fails closed.
- Cons: the contract is apart from the skill it describes; adding a skill means adding a row.

#### Option B — attributes on every skill (`needs`, `gives`)
- Pros: the contract lives with the skill; the registry can list them.
- Cons: changes the `Skill` protocol, every mock skill and every AIC skill, for a feature the
  AIC adapter does not use yet.

### 4. When planning happens

#### Option A — once per run, after `run_started` and before the first skill
- Pros: the shape of an AIC goal (a `Task` arrives, the tree is built, then the arm moves);
  an invalid answer is an event of the run, as exit criterion 1 asks; no motion has started.
- Cons: one model call per run; the eval's call pre-check must count it.

#### Option B — once per task type, offline, approved like a promoted rule
- Pros: no model call in a run; a person sees each plan once.
- Cons: the validation gate is no longer in front of a run; the AIC engine sends tasks one at
  a time, and a type that was never planned has no tree.

#### Option C — step by step during the run
- Cons: a model call between skills is a model call in the loop the arm waits on.

### 5. What an invalid plan does

#### Option A — the run ends `aborted` with `task_plan_rejected`, before any motion
- Pros: an unverified program never runs; the eval counts the generator's failures honestly.
- Cons: a task fails that the fixed tree might have done.

#### Option B — fall back to the fixed tree
- Pros: the cell keeps working.
- Cons: the eval then measures the fixed tree whenever the generator fails, and a generator
  that always fails looks as good as no generator.

#### Option C — ask again, a bounded number of times
- Cons: more calls; the same prompt at temperature 0 is the same experiment.

### 6. Hash and events

#### Option A — events and a `tree` config part only when the tree is not `fixed`
`task_planned` (the planner and its steps; a model planner's trace is a later ticket) or
`task_plan_rejected` (codes, and a bounded, scrubbed excerpt of the proposed steps), right after
`run_started`.
- Pros: a run with the fixed tree gets no new part and no new event; its log reads as before.
- Cons: a conditional part (the promotion work on `phase3/promotion` makes the same choice for
  promoted rules).

#### Option B — always record the plan
- Cons: every run gets a `tree` part and a `task_planned` event, including the fixed tree's;
  the configuration of every earlier result differs for a change that alters no behaviour.

## Decision

<!-- Decision and Why drafted by Claude. Shaun delegated these sections to Claude on 2026-11-09 and accepts them by merging; he may rewrite any of it. -->

1. Option A: a plan, an ordered list of 1 to 8 registered skills.
2. Option B: the schema, the registry and a step contract per skill, applied in order.
3. Option A: the contracts are a table next to the verifier; a skill without a row cannot be
   planned.
4. Option A: once per run, after `run_started` and before the first skill.
5. Option A: an invalid plan ends the run `aborted` before any motion.
6. Option A: plan events and a `tree` config part only when the tree is not `fixed`.

## Why

The model's job is to say which skills a task needs and in what order; how a failure is
recovered is already decided by the setting, and letting a generated program decide it too
would leave the A/B/C comparison measuring an unknown mix of two recovery layers. A flat plan
keeps that line, and it is small enough that a precondition/effect check, the usual way a plan
is validated, applies to every plan the language can express. The contracts sit in a table because the protocol change
would touch the AIC skills for nothing yet, and a missing row refuses a plan rather than
letting it through. Planning once per run, before motion, is where an AIC goal would plan and
where an invalid answer can be an event of the run without anything having moved. Ending the
run on an invalid plan keeps the generator's failure rate visible; a silent fallback would hide
it behind the fixed tree. The conditional part keeps the change one-sided: only runs that ask
for a plan look different.

## Consequences

- `cellrt/planner/task_plan.py`: `STEP_CONTRACTS`, `verify_plan` and its codes, `plan_task`
  (asks the planner within its wall-time `deadline_s`, default and limit as for the UNKNOWN
  planner (ADR 016); reads at most 9 names; refuses anything that is not a string; turns a
  timeout into `planner_timed_out` and an exception into `planner_error`), the
  `TaskPlanner` protocol and the `table` planner (plug type to insert skill, the non-model
  baseline). `--tree fixed`, the default, is no task planner at all. A model planner (a later
  ticket) parses its answer into a strict schema of 1 to 8 names, through the `ChatClient` seam
  with its deadline and the eval's cap.
- `build_tree` takes the steps as a parameter; by default they are `SKILL_SEQUENCE`.
- `task_planned` and `task_plan_rejected` are new event types (schema version 11). They are
  emitted only when there is a task planner; `cellrt replay` prints them. A rejected plan keeps
  at most 9 names, each scrubbed and cut to 64 characters.
- The `table` planner encodes what we wrote into it, as a recipe table does. The mock scenario
  where a plug type needs `insert_act` is ours too (`eval/scenarios/phase3-tasks.yaml`: the
  rotated-plug fault on every SC task, after #28's SC plug jamming at the port mouth; whether
  `insert_act` frees it in the sim is not known). On it the table's plan needs no recovery and
  no planner call, but descends at `insert_act`'s half speed from the start, so in setting C it
  takes longer in sim time than the fixed steps with one planner switch; a planner call costs
  wall time the arm holds in the sim, which the mock does not count. A model planner's value has
  to be read against the table, not against the fixed tree.
- A plan whose skill clears a fault before any verdict (the table's `insert_act` twisting the
  SC plug free) leaves that fault without a verdict, and the eval's confusion matrix counts it
  as missed: classification accuracy is not comparable between trees until the metrics tell a
  fault a skill cleared from one the Monitor missed.
- A rejected run counts as an aborted task with no steps and no time in the eval table, so it
  pulls the mean duration and steps down, and the table has no column for rejections. Before a
  model plans tasks, the eval has to report the plan rejection rate and keep rejected runs out
  of the means.
- The AIC adapter keeps the fixed tree; nothing in `adapters/` changes.
- When skills get real preconditions (today every mock skill's `preconditions` returns True),
  the contract table and the runtime precondition check should be reconciled into one
  description.
