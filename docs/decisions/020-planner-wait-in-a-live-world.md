# 020: Waiting on the planner in a live world

Date: 2026-11-09
Status: accepted

## Context

ADR 016 holds the mock world still while a planner call runs: the loop waits in wall time, the
sim clock does not move, and the eval stays deterministic. It left AIC open. In Gazebo the
simulation does not stop because the policy is waiting.

#52 measured what a call costs. Over the calls that answered, the median was 12.8 s of wall time
and the p95 19.7 s; 6 of 89 calls were cut at the 30 s deadline. In the Phase 1 sim run (#28)
the AIC sim ran at about 0.43 of real time, so a median call is about 5.5 s of sim time. A
CheatCode task is about 32 s.

If the loop stopped stepping during that time, as it does in the mock, four things would go
wrong on AIC:

1. **The run's clock lags the engine's.** `Task.time_limit` is counted by the engine in sim time.
   Our clock would stand still, so the `time_left_s` the planner is told would be too high, and a
   run could reach the engine's limit without its own loop noticing.
2. **Nobody watches the arm.** The arm holds where the failed skill left it, often in contact.
   The scorer keeps adding force time over 20 N, but no sample is taken and the Monitor sees
   nothing.
3. **A cancel goes unseen.** The adapter notices a cancelled goal when the world steps (ADR 012),
   so a cancel during a call would wait for the answer or the deadline.
4. **The answer is stale.** It answers a question about the state when the call started.

The usual practice for this is old. In a three-layer robot architecture (Gat, ATLANTIS 1992;
"On Three-Layer Architectures" 1998), computations that are slow relative to how fast the world
changes belong in the deliberator, which runs alongside the sequencer and the controllers
instead of blocking them. Recent vision-language-action systems keep a similar split of rates:

- Figure's Helix runs its VLM at 7-9 Hz and its controller at 200 Hz, asynchronously.
- NVIDIA's GR00T N1 reports 10 Hz for its vision-language part and 120 Hz for its action module.

The comparison is about the split, not about failure handling: those slow layers feed every
control step. Our planner is about a hundred times slower than they are, which is why it is
asked only about failures, never inside control (ADR 006).

Constraints:

- The mock eval stays deterministic, and Phase 0's baseline and #52's results do not change.
- ROS stays in `adapters/`.
- Every wait has a bound that a test hits.
- The Monitor keeps its authority over everything else (ADR 006).

## Options

### 1. The world during a call, on AIC

#### Option A — keep ADR 016: stop stepping while the call runs
- Pros: one behaviour for mock and sim; sim-time results would not depend on how long the model
  took.
- Cons: all four problems above. The log would say no time passed when the engine counted
  seconds.

#### Option B — live hold: keep stepping the world while the call runs
The arm holds (HOLD was sent when the skill ended). Every step is sampled and recorded, the clock
advances, and the time limit and the cancel check apply as on any step. The call's wall deadline
still applies, checked on each step instead of waited on.
- Pros: the log matches what the engine counted. The Monitor can watch the hold. A cancel is seen
  within a step.
- Cons: two modes for one loop. In a fast mock world the number of steps during a call depends
  on network speed, so live mode cannot be the mock's default.

#### Option C — pause the simulator during the call
- Pros: deterministic in the sim too.
- Cons: the policy's interface has no way to pause the simulation: it gets the task,
  observations, `move_robot` and `send_feedback` (`aic_model/aic_model/policy.py`). A real cell
  cannot be paused either, so results would hide the cost of thinking.

### 2. What watches the hold

#### Option A — record only
- Pros: simple.
- Cons: a force problem during a long call is seen only after the answer.

#### Option B — the Monitor with the safety rules only
SENSOR_DROPOUT and FORCE_LIMIT, ADR 006's safety classes. These are the rules the Monitor already
applies to motion skills (the `motion` profile).
- Pros: these are the hazards that can still grow while the arm is commanded to hold: force from
  contact the hold presses into (expected from #28's seated plugs, not yet measured during a
  hold), and a force budget gone blind when the F/T stream drops. Progress, stall and pose rules
  would fire at once on an arm that is correctly standing still.
- Cons: a hold that drifts in position without force goes unseen until the next skill.

#### Option C — every Monitor rule
- Cons: holding still is no progress, so stall and stuck rules would preempt every call.

### 3. A Monitor verdict during a call

#### Option A — the Monitor wins
Cancel the call, record the verdict and a `planner_preempted` event, and recover as if the failed
step had failed with the verdict's class: that class's plan within its budgets (ADR 007; for
FORCE_LIMIT one recovery: retract, then the skill again at half speed). With the budget spent,
the step fails as it would have. The late answer is never read. ADR 007 notes that a FORCE_LIMIT
outside insertion is not reachable in the mock; a live hold after a failed align makes it
reachable in the sim.
- Pros: the Monitor's authority (ADR 006) holds during a wait too. A known hazard gets the known
  plan, now.
- Cons: the call is wasted, and it still counts against the planner budget.

#### Option B — let the call finish, then decide
- Cons: a hazard waits on the network.

### 4. A stale answer

#### Option A — record the staleness and recheck preconditions on arrival
`planner_decided` records `waited_sim_s`, the sim time between call and answer: 0 for a call
the mock's loop waited on, one period for an answer in the call's first step (the rule
planner's), which is read on the next tick. A
switch or retry is checked against the chosen skill's preconditions on the state at answer time.
One that fails them is rejected like any invalid output (ADR 008): it is recorded, nothing runs,
and the run ends aborted.
- Pros: cheap. The trace shows the cost of thinking in the robot's time. A decision about a state
  that changed in a way the skill cares about is not acted on.
- Cons: it catches only what preconditions express; a stale answer that passes them still runs.

#### Option B — ask again with fresh context when more than N s passed
- Cons: doubles the cost and latency exactly when time is short; N is a number we would invent.

#### Option C — nothing
- Cons: the trace cannot show how stale a decision was.

## Decision

<!-- Decision and Why drafted by Claude. Shaun delegated these sections to Claude on 2026-11-09 and accepts them by merging; he may rewrite any of it. -->

Option 1B, Option 2B, Option 3A, Option 4A.

- The executor has a planner-wait mode, part of the run's configuration (and of its
  `config_hash`, with the planner):
  - `frozen` is the default and keeps ADR 016 unchanged: every mock run and eval.
  - `live` is what the AIC adapter runs.
- In `live`, a tick on which a call holds the world still steps it:
  - the clock advances by what the world reports, and every step is recorded as `signal_sampled`;
  - the time limit and the adapter's cancel check apply as on any step;
  - the call's wall deadline is checked on each step without blocking, and a call past it is
    cancelled on the next tick, before another step, and recorded as in ADR 016;
  - an answer is read on the tick after it arrives, before another step, as in `frozen`;
  - a run that ends during a call cancels it, with ADR 016's grace.
- During a live wait the Monitor observes each step with the safety rules only: dropout, and the
  force limit on the running sum the scorer uses. A verdict cancels the call, is recorded as
  `monitor_verdict` and `planner_preempted` (event schema version 9, with wall and sim time
  waited), and the step recovers as if the failed skill had failed with that class.
- `planner_decided` records `waited_sim_s` (event schema version 10). A switch or retry is
  checked against the chosen skill's preconditions on the state at answer time; a decision that
  fails them is rejected with a fixed code (`skill: preconditions_not_met`), as invalid output is
  (ADR 008).

## Why

On a real cell, time passes while the model thinks, and the arm is in contact while it does.
The runtime has to keep counting that time, keep watching that contact, and keep its promise
that the Monitor outranks the planner. Live hold does all three. It keeps the split of the
three-layer architecture, which today's dual-system models also keep: the slow layer runs
alongside, and the fast layer does not wait on it.

The mock keeps `frozen` because its job is different: comparing settings on the same seeds. A
world that moves while the network answers would make the comparison measure the network.
Latency is reported next to the mock's results instead (#51, #52). Pausing the simulator would
make the sim deterministic too, but it is not available to a policy and would hide exactly the
cost this ADR is about.

During a hold the Monitor runs only the safety rules because every other rule reads "no
progress" as a fault, and holding still is the intended behaviour. Rechecking preconditions on
arrival, instead of asking again, keeps a slow call from turning into two slow calls when time is
shortest.

## Consequences

- Implementation: live hold (`ExecEnv.planner_wait`) is done in #55. The Monitor during a hold,
  preemption and schema 9 are #56; `waited_sim_s` and the recheck on arrival are #57; the AIC
  adapter runs `live` from #59.
- Sim runs in setting C are not deterministic: how much sim time a call costs depends on the
  network and the real-time factor. Sim results are a sample. Their traces show the time, so a
  reader can see it. The mock stays `frozen`, so its results and the baseline do not move.
- The trace shows, per decision, how stale it was (`waited_sim_s`) and when the Monitor cut a
  call short (`planner_preempted`).
- The deadline is checked when a step returns. A world step that blocks is bounded by the world
  itself (`AicWorld` stops a run whose sim clock stalls), not by this check.
- A blocking step also blocks the loop. With an async HTTP client a request could only move
  while the loop was free, so each network round trip waited out a step, and sim latencies
  included that (#64 review F4). The NIM client now makes the request on a thread of its own
  (ADR 017): it goes on during a step, and only the answer waits, at most one step, for the
  loop to read it. The world and the skills stay on the loop's one thread (ADR 003). A thread
  cannot be cancelled: a cancelled call ends at once, its request runs on until it is answered
  or the SDK's timeout ends it, and its answer is dropped. The sim latencies in
  `docs/sim-results.md` were measured before this change.
- Schema version 9: replay, trace export and the agent report read the new event and field. Rows
  of earlier versions still read.
- Schema version 10 (`planner_decided.waited_sim_s`): replay and trace export show it. A
  refused stale answer is a `planner_output_rejected` with the code
  `skill: preconditions_not_met`; the agent report counts it as stale, not invalid, and the
  run ends "planner answer no longer applies". Like a rejection, it spends no recovery.
- No registered skill has a precondition yet, so the recheck rejects nothing today (#64 review
  F2). A precondition that fails ends the step with no failure class, so recovery has nothing to
  act on, while a plug off the axis or a stale wrench is a Monitor verdict with a plan. A
  precondition on AIC insertion (within 1 mm of the axis) would refuse the reruns that skip
  align: after a FORCE_LIMIT retract, a sensor wait, a TIMEOUT retry. A like condition in the
  mock took B's success from 0.619 to 0.571, so it was not kept. The recheck is for a skill whose
  start needs a state the Monitor does not classify.
- ADR 016's "Open: AIC" is closed by this ADR, and ADR 016 still holds for the mock. Of its
  three to-dos: a cancel is now seen within a step; the time limit ends a run during a call, but
  the call's deadline is not yet set below the time left; hold behaviour during a long call is
  checked only in the sim session (#61).
