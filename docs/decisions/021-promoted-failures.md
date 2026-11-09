# 021: How a promoted UNKNOWN is represented and handled

Date: 2026-11-10
Status: proposed

## Context

Setting C asks the planner about every Monitor UNKNOWN, and nothing it learns carries over. In
#52 the model picked `insert_act` for the rotated plug in 26 of the 30 runs where it answered,
and each of those calls cost about 1,950 tokens and 12.8 s of wall time at the median
(`docs/phase3-llm-results.md`). The next rotated plug was as unknown as the first.

The Phase 3 brief proposes UNKNOWN promotion: a recurring UNKNOWN with a clear signature becomes
a Monitor rule with a recovery plan, after a replay check (ADR 022) and a human approval. This
ADR decides what the adopted rule is inside the runtime: what the Monitor reports when it fires,
how recovery finds its plan, and what may never change.

Usual practice: process-industry alarm management (ISA-18.2 / IEC 62682) adds an alarm only
through a documented rationalisation and a management-of-change step, with its own priority
against existing alarms. Detection engineering in security operations tests a new rule against
historical logs for false positives before it is turned on. Both keep the new rule separate from
the existing set and traceable to who approved it.

Constraints:

- Built-in rules and thresholds do not change; Phase 0's results and the committed baseline keep
  their meaning and their hashes (ADR 010).
- A promoted rule may not override a safety verdict or a known class (ADR 006).
- Classification accuracy is measured against injected ground-truth labels (ADR 005); the mock
  faults' labels are fixed in the scenario files.
- The LLM's output is untrusted (principle 4): the rule language must be small enough to
  validate and to replay.

## Options

### 1. What the Monitor reports when a promoted rule fires

#### Option A — a new failure class named by the rule (`PLUG_ROTATED`)
- Pros: what the brief describes; reads naturally in the log and the report; the recovery table
  stays keyed by class only.
- Cons: `FailureClass` is a closed enum used by the classifier's accuracy metric, the recovery
  budgets and the planner schema; an open set of classes touches all three. The injected label of
  `yaw_mismatch` is UNKNOWN, so the accuracy metric would count every promoted verdict as wrong
  unless labels are remapped, which changes what Phase 0's accuracy numbers mean.

#### Option B — the class stays UNKNOWN; the rule is `promoted:<NAME>`
- Pros: the class set, the accuracy metric and the planner schema do not change; the log still
  says which rule fired and the report can group by it; removing a rule is deleting a file.
- Cons: "UNKNOWN" for a failure the system now handles reads oddly; recovery has to look at the
  rule as well as the class.

### 2. How recovery finds the plan

#### Option A — a plan keyed by rule name, ahead of the class plan
The recovery table gets `promoted: {NAME: plan}`; a verdict whose rule is `promoted:NAME` uses
it in settings B and C, without the planner. The plan counts against the UNKNOWN class budget
and the task budget.
- Pros: setting B, which has no planner, gains the handling too, so the before/after shows the
  rule's value apart from any model; budgets keep bounding it.
- Cons: two lookups instead of one.

#### Option B — the planner gets the promoted rule's plan as a hint
- Pros: no change to recovery.
- Cons: still a model call per failure, which is what promotion is meant to remove.

### 3. Where a promoted rule sits in the Monitor's order

#### Option A — after the known-class rules, before `insert_stalled`
Order: dropout, force limit, stuck, pose error, promoted rules (in file order), stalled.
- Pros: on the same sample a known class or a safety verdict always wins; a promoted rule only
  takes verdicts that would have been UNKNOWN or none.
- Cons: a promoted rule can still fire on an earlier sample than a known rule would have; the
  replay check (ADR 022, gate 3) has to catch that.

#### Option B — before the known-class rules
- Pros: none that matter here.
- Cons: a promoted rule could hide a stuck or pose-error verdict on the same sample.

### 4. What enters the config hash

#### Option A — the promoted rules, only when there are any
`MonitorConfig.as_dict()` adds `promoted` only when the tuple is non-empty.
- Pros: every existing run, the committed baseline and #52's stores keep their hashes; a run
  with promoted rules never shares a hash with one without.
- Cons: `as_dict` has a conditional key.

#### Option B — always include `promoted: []`
- Pros: uniform.
- Cons: every config hash changes, so the baseline gate would report a change for every unit
  and the baseline would need re-approval for a change that alters no behaviour.

### 5. Which skills a promoted rule watches

#### Option A — the skill its group came from
The rule names one skill; the Monitor learns the running skill on `reset` and the rule is
silent for any other.
- Pros: the rule fires only where its evidence was collected: a group's signature includes the
  skill (ADR 022), so the replay validated the rule on that skill's samples and no other.
- Cons: the same failure under another skill needs its own group and its own approval.

#### Option B — every skill with the insertion profile
- Pros: one rule covers `insert_scripted` and `insert_act` at once.
- Cons: the rule judges a skill it was never validated on. `insert_act` stops descending and
  twists while it meets torque, which is close to what a rotated-plug rule describes. In the
  mock the twist lasts 0.15 s, too short for a 0.5 s window, so nothing shows; in the sim the
  ACT policy's behaviour is not in any recorded evidence.

## Decision

<!-- Decision and Why drafted by Claude. Shaun delegated these sections to Claude on 2026-11-09 and accepts them by merging; he may rewrite any of it. -->

1. Option B: the class stays UNKNOWN and the rule is `promoted:<NAME>`.
2. Option A: a plan keyed by rule name in the recovery table, used in B and C without the
   planner, inside the UNKNOWN class budget and the task budget.
3. Option A: promoted rules after the known-class rules and before `insert_stalled`.
4. Option A: promoted rules enter `MonitorConfig.as_dict()` only when there are any.
5. Option A: a promoted rule watches only the skill its group came from.

## Why

Promotion changes how a failure is handled, not what the failure is. The ground truth for a
rotated plug is still "no built-in rule describes it", which is what UNKNOWN means; keeping it
lets the accuracy metric and every earlier number keep their meaning, and the rule name carries
the new knowledge. The rule-keyed plan is what makes the effect measurable: setting B gains the
handling with no model at all, so the before/after separates what the rule is worth from what
the model is worth. The order and the conditional hash keep the change one-sided: a promoted
rule can only take what would have gone to the planner, and nothing that ran before it is
re-hashed. The skill scope keeps the rule inside its evidence: a replay can only vouch for the
samples it replayed, and those came from one skill.

## Consequences

- `MonitorConfig` gets `promoted: tuple[PromotedRule, ...] = ()`; `RecoveryConfig` gets
  `promoted: Mapping[str, PromotedPlan]`, keyed by rule name. Both enter their config's hash only
  when non-empty.
- Approval is a file: `cellrt promote approve REPORT --by NAME` refuses a report unless all four
  gates passed with no failing window, the report was made under the Monitor configuration in
  use now, and its candidate still has the hash the report names. It writes
  `eval/promoted/<NAME>.json` (rule, plan, candidate and report hashes, that configuration,
  approver) and never replaces an approved rule. `cellrt eval --promoted DIR` loads at most 16
  such files and checks each again (file name, configuration, hash, registered skills) before
  the store is opened. A rule approved under other Monitor thresholds or mock parameters is
  refused as `config_stale`: its gates vouched for a Monitor that no longer runs.
- The hashes catch an accident (a candidate edited after its report, a configuration changed
  since), not tampering: they are not signatures, and anyone who can write the report or the
  approval directory can forge both. Write access to `eval/promoted/` is the trust root, as
  write access to the code is. `approve` trusts the report's gate results; re-running the
  replay on the store at approval time would close that gap and is a follow-up.
- By default UNKNOWN has no class budget: only the planner handled it. With promoted rules and
  no UNKNOWN budget set, the budget becomes 2, the planner's per-task budget, so a retry plan
  cannot loop. In setting C a recovery the planner chose for an UNKNOWN also counts against
  that class, so a task whose planner already recovered twice from other UNKNOWNs has no
  budget left for a promoted plan; the run then fails with the budget named in its reason.
- Setting B gets the insert-skill alternatives it had no use for before, when a promoted plan
  switches skill, so a switch goes through the same option check as a planner's.
- An `abort` or `escalate` plan ends the run as `aborted`, with the rule named in the reason; it
  starts no recovery, so it spends no budget. The other plans emit `recovery_started` with the
  action `promoted:<NAME>:<action>` and count against both budgets.
- The log carries the rule on every verdict (`promoted:<NAME>`) and every promoted recovery. The
  eval reports do not yet group UNKNOWN by rule; that column is a follow-up.
- A rule is removed by deleting its file under `eval/promoted/`; the runs made with it keep their
  hash and stay separate.
- If promoted rules ever need their own budgets or their own accuracy numbers, Option 1A becomes
  worth revisiting with a label remap written down as its own decision.
