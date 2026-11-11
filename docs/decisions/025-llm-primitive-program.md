# 025: Setting D, a model-written program of bounded motion primitives

Date: 2026-11-11
Status: proposed (not built)

## Context

In setting C a model answers an UNKNOWN with a decision: `retry`, `switch_skill`, `abort` or
`escalate` (`cellrt/planner/schemas.py`). Each decision names something the cell can already
do. The AIC sim registers one insert skill, so the model's useful choices in the sim come down
to retry, abort or escalate. With 4096 tokens it chose retry 3 times and escalate once
(`docs/sim-results.md`). Shaun's review of the first sim demo (2026-11-10) asked what the model
adds if retry is all it can say. It also asked whether the model could drive the insertion
itself where no rule exists.

There is a second gap. A known failure whose recovery runs out also ends the task with no model
call. The SC plug stuck on the entrance rim is classed INSERT_STUCK. The recovery table runs
`retract_realign` up to its budget of 2 and then fails the step, and only an UNKNOWN goes to a
planner (`cellrt/executor/recovery.py`). In the F1 rerun, C-LLM #1's goal3 used both realigns
and failed with no model call; in C-LLM #3 one realign seated it (`docs/sim-results.md`).

The question this ADR answers: how could the model get more room to act on a failure the rules
do not fix, without breaking the design principles? Those principles are:
- the model never sits in the real-time loop;
- its output is untrusted and checked before anything runs;
- known failures are handled deterministically (principle 1). Today that also means only an
  UNKNOWN reaches the model;
- a recorded run replays the same, because replay reads the model's answer from the log. A new
  eval with a model does not repeat itself (three repeats on the mock gave 56.7-66.7%); the
  same scenario and seed give the same result only with the scripted planners.

Usual practice:
- Language-model robotics work has the model write code that calls perception and control
  functions. Code as Policies runs the generated Python directly. Other work keeps a checker
  between the model and the robot: LLM-Modulo surrounds the model's plans with external
  verifiers.
- Peg-in-hole insertion commonly recovers a missed hole with a contact strategy, such as a
  spiral or tilt search or a compliant wiggle (ADR 024 names the same practice). Collaborative
  robot controllers commonly enforce speed and force limits whatever the program asks. This is
  the general pattern, not a survey.
- The parts this cell already has are the rule language and its plans for promoted UNKNOWNs
  (ADR 021) and the plan check against the registry (ADR 023).

## Options

### Option 1 — keep the decision set (today)
- Pros: nothing new to verify. Every decision maps to code that is tested.
- Cons: the model can only pick what was written beforehand. Where the cell has one insert
  skill, that is retry or stop.

### Option 2 — a short program of bounded primitives, checked before it runs
The model is asked in two cases: an UNKNOWN, as today, and a known class whose recovery budget is
spent, where today's runtime fails the step. It answers with a program of at most N steps from
a fixed set:
- `move_rel(dx, dy, dz)`: a relative move, capped per step and in total;
- `spiral_search(radius, pitch, turns)`;
- `wiggle(axis, amplitude, cycles)`;
- `press_until(force_n, max_travel)`;
- `hold(seconds)`;
- the existing skills by name.

A validator checks the program before it runs:
- the schema;
- that every primitive is in the registry;
- the step count, each step's and the total displacement, and the workspace relative to the
  port;
- force targets under a cap below the 20 N penalty threshold;
- the total time against the task's remaining budget.

It runs as one skill. The Monitor keeps its authority and stops it on a verdict. The skill
needs a Monitor profile of its own:
- the insertion profile's `stuck_under_force` rule (over 10 N with under 0.2 mm of progress in
  0.5 s) would stop a search that presses on the rim, which is what a search does;
- the motion profile would not see a stall.

So the profile is part of the design, with the force limit unchanged. The program, or its
rejection with codes, is an event, so replay reads it without calling the model.
- Pros: the model can propose a contact strategy no one wrote for this failure. The
  verifier-in-front pattern stays the same as task-to-tree's. Every limit is a number in the
  validator, which can be tested.
- Cons:
  - a new language, a validator and a skill to build and test;
  - it widens when the model is asked: a known class now reaches the model once its rules are
    spent, which changes principle 1's second half;
  - the mock has no contact dynamics, so a program's effect on a jam can only be judged in the
    sim;
  - a bounded program can still be a bad one, and the bounds limit the harm, not the
    judgement.

### Option 3 — the model writes Python, run in a sandbox
- Pros: the most freedom, and the least to design.
- Cons: what generated code will do cannot be checked before it runs, only contained. That
  breaks "checked before anything runs", and a sandbox does not bound the forces on the arm.

### Option 4 — the model steers the arm step by step
- Pros: it could react to every contact.
- Cons: it puts the model in the control loop. The sim calls on record that answered took
  6.5-28 s each, and one was cut at the 30 s deadline, against a 50 ms step. Ruled out by the
  project's non-goals: the LLM never sits inside the real-time control loop.

## Decision

<!-- Decision and Why drafted by Claude from the option chosen in plan mode. Shaun delegated these
sections to Claude on 2026-11-09 and accepts them by merging; he may rewrite any of it. -->

Option 2, as a design only, not built in Phase 3 or Phase 4. If built, it is a fourth setting,
**D**: C's recovery, with two changes:
- the model's answer may be a primitive program as well as a decision;
- the model is asked once more when a known class's recovery budget is spent, where C fails the
  step. The known recovery always runs first, deterministic and unchanged.

The validator's limits are constants in the config hash, and the committed baseline does not
change. In D, principle 1 would read: known failures are handled deterministically first, and
the model gets an UNKNOWN or a known failure the rules could not fix. CLAUDE.md keeps today's
wording until D is built.

How it would be evaluated, first on the mock:
- **Scenarios.**
  - A plug on the rim that a small spiral frees and a realign does not. The Monitor classes it
    INSERT_STUCK, B's `retract_realign` runs twice and fails, and only then is the model asked.
    The mock would model it as a lateral offset that only a search of the right radius removes.
  - The UNKNOWNs of `eval/scenarios/phase3.yaml`, so D is checked against C where C already
    works.
  - These faults are ours, as in ADR 019, so the mock numbers measure the validator and the
    loop, not the model's skill at contact.
- **Rows:** C (decisions only), D (programs), and B with a hand-written spiral after the
  realigns, the rule equivalent. Columns: success, unsafe events, programs rejected by code,
  steps per program, calls per task, and latency.
- **Validator tests** that hit every limit: one program per limit, just over it, rejected with
  its code.
- **Then the sim,** on the SC rim jam (`docs/sim-results.md`), a jam no one designed. There the
  Monitor calls it INSERT_STUCK, and both realigns run before the model is asked. The question
  is whether D frees a jam that B's recovery could not, without a force event, and how it
  compares with the hand-written spiral.

## Why

- **It is the smallest step that lets the model act where no rule was written, and keeps the
  check before the motion.** The same split as task-to-tree (ADR 023): the model proposes, and
  a validator with written limits decides what may run.
- **The model comes after the rules, not before them.** A known class keeps its tested recovery,
  which costs no model call; a sim call that answered took 6.5-28 s, and one hit the 30 s
  deadline. The model gets the jam only once the
  table has nothing left to try, where today the task fails. That keeps principle 1's first
  half, the part that keeps the cell predictable. It also gives D a case in the sim that no one
  designed: the SC rim jam, 1 of 3 clean runs.
- **The weakness to name first:** the bounds make a program safe to try, not good. On the mock,
  D's success measures faults we designed with their remedies in mind. Its value has to come
  from the sim, where contact is real.
- **Why not now:** the time box for the interview version (2026-11-16). D needs a language, a
  validator, a skill, mock contact faults and sim runs. Each is small, but together they are a
  phase. The decision set, measured as it is, is the honest state of the project.

## Consequences

- Nothing changes in the code now. The README lists setting D under Next.
- If built:
  - new events: a program proposed, rejected with codes, and each primitive started and
    finished;
  - a model consult when a known class's budget is spent, recorded with that class, so replay
    and the agent report can tell it from an UNKNOWN;
  - a setting letter in the config hash, and a validator module with its own tests;
  - one Monitor profile for the program skill, with the force limit unchanged;
  - principle 1's wording in CLAUDE.md, as in the Decision.
  Budgets and replay stay as they are.
- D and ACT as `insert_act` both aim at the contact strategy. If both are built, they should be
  compared on the same SC rim jam.
