# 016: Waiting on the planner

Date: 2026-11-09
Status: accepted

## Context

A model planner (#45) answers over the network: its call takes real time, from well under a
second to tens of seconds, and sometimes never returns. The loop of ADR 003 cannot wait on
that. Drain requires every live task to wait on the sim clock within 100 yields; a task that
awaits a socket instead raises `DrainError`. In Phase 0 this never came up, because the rule
planner answers without awaiting anything and finishes inside the drain.

The planner is asked only between skills, never inside the control loop (ADR 006): the failed
skill has been stopped, its command access revoked and HOLD sent. So the question is not how
to control the arm during the call, but four smaller ones:

1. What happens to the world and the sim clock while the call runs?
2. How long may a call take, and who sets that?
3. What happens to an answer that arrives too late?
4. How does the run end cleanly when a cancelled call takes time to clean up, or never does?

Constraints: eval is deterministic in mock mode (same scenario and seed give the same result,
CLAUDE.md principle 5), the core has no wall-clock reads outside the files allowed to have them,
the Phase 0 baseline must not change, and every wait needs a bound that a test hits.

## Options

### 1. The world during the call

#### Option A — the world stands still; the call costs wall time only
While a call waits on outside work the loop does not step the world or advance the clock. It
waits on the call in wall time and ticks the tree again when the call finishes or a deadline
passes.
- Pros: in the mock, sim-time results do not depend on how fast the network was, so the eval
  stays deterministic and the Phase 0 baseline is unchanged; it is what HOLD means for the arm.
  Latency is still measured, in wall time, in the trace (`planner_responded.latency_s`).
- Cons: the mock's durations leave out the time spent thinking, so they flatter a slow model;
  latency has to be reported next to them, not inside them. On AIC the sim does not stop (see
  Consequences).

#### Option B — the world keeps stepping while the call runs
- Pros: closest to a real cell, where time passes while the model thinks; the task time limit
  bounds the call for free.
- Cons: in the mock a step takes microseconds of wall time, so how many steps pass during a
  call depends on network speed: the same seed gives different signal logs and durations, which
  breaks deterministic eval. Making it deterministic needs a mapping from wall to sim time,
  which is Option C.

#### Option C — charge each call a fixed sim-time cost
- Pros: deterministic, and durations include a cost for thinking.
- Cons: the cost is a number we invent; it still needs a wall-time deadline for a call that
  never answers; the real latency would sit in two places that disagree.

#### Option D — run the call on a thread and block the loop
- Pros: no change to drain.
- Cons: breaks the single-threaded lockstep of ADR 003 and cancellation across a thread
  boundary; a blocked loop cannot tick anything else.

### 2. The deadline

#### Option A — a wall-time deadline per planner, in its configuration
Default 30 s, at most 120 s, part of the run's config hash (`LlmPlannerConfig.deadline_s`; any
planner may set `deadline_s`).
- Pros: explicit, bounded, recorded; two deadlines never share a hash, so a resumed eval does not
  mix them.
- Cons: one more setting.

#### Option B — derive it from the task's time left
- Pros: no new setting.
- Cons: under Option 1A the sim clock does not move during the call, so sim time left says
  nothing about wall time; a 60 s task limit would allow a 60 s call on every failure.

### 3. A late answer

#### Option A — cancel the call at the deadline; the answer is never read
Record `planner_timed_out` (deadline and wall time waited) and fail the step without running
anything. Under ADR 008's recovery that aborts the run with "planner timed out".
- Pros: simple and safe: nothing acts on an answer to a state that has moved on; cancelling
  frees the connection.
- Cons: a call that would have answered a moment later is wasted.

#### Option B — keep the call and use the answer if it arrives before the next decision point
- Pros: no wasted call.
- Cons: the answer was made for a state that no longer holds; it needs a second validation
  against the new state, and the run must keep a task alive across decisions.

### 4. Cleanup after a cancel

#### Option A — a short wall-time grace, then fail loudly
When the run ends, cancelled calls get up to `CANCEL_GRACE_S` (1 s) to finish their cleanup
(closing a connection). A call still running then ignored its cancellation: the run raises
`DrainError`, as a skill does that never waits on the clock.
- Pros: bounded; a broken client is found, not hidden.
- Cons: a slow but honest cleanup over 1 s also fails the run.

#### Option B — abandon the task
- Pros: the run always ends.
- Cons: a task outlives its run, holding a connection and possibly writing into the next run's
  state.

## Decision

<!-- Decision and Why drafted by Claude. Shaun delegated these sections to Claude on 2026-11-09 and accepts them by merging; he may rewrite any of it. -->

Option 1A, Option 2A, Option 3A, Option 4A.

- A planner call is started as an **outside wait**: a task with a deadline. A call that
  answers in its first step, without awaiting anything (the rule and fixed planners), finishes
  inside the drain and costs one step, exactly as before. A call still running after its first
  step has awaited outside work: it holds the world for that tick and every tick until it
  answers, even if it answers within the drain, so whether the world moves never depends on how
  fast the answer came. After the drain's yields, live calls are allowed as long as every other
  task waits on the clock; a skill that never waits on the clock still raises `DrainError`.
- While an outside wait runs, the loop does not step the world or advance the clock. It waits
  in wall time until the call finishes or the earliest deadline passes, then ticks the tree.
  `planner_called` and `planner_decided` then carry the same sim time and no signal sample lies
  between them.
- The deadline is the planner's: default 30 s, at most 120 s, refused outside (0, 120], and
  hashed into the run's configuration.
- At the deadline the node cancels the call, records `planner_timed_out`, and fails; the
  recovery ends the run `aborted` with "planner timed out" (event schema version 8).
- When the run ends, cancelled calls get up to 1 s of wall time to finish; a call still running
  then raises `DrainError`. A cancelled call that fails in its cleanup has its exception read
  and dropped, so its text never reaches a log.
- Besides the recorder's `wall_ts`, wall time is read only in `cellrt/executor/runner.py` (the
  loop's wait) and in the model planner's latency timer; none of them is folded into state.

## Why

The eval's job is to compare settings on the same seeds. If the world moved while the model
thought, the comparison would measure the network as much as the planner. Holding the world
keeps the mock deterministic and the Phase 0 numbers unchanged, and it is also what the
runtime already does to the arm: HOLD until a decision. The cost of thinking does not
disappear; it is recorded per call as wall-time latency and reported next to success rates.

The deadline belongs to the planner because only the planner knows how slow it can be, and a
deadline in the configuration is one more thing a reader of the results can see. A late answer
is dropped rather than reused because it answers a question about a state that has already been
acted on; reusing it would need a second validation and a task that lives across decisions.

The grace after a cancel exists because real clients clean up asynchronously; the 1 s bound
keeps "no task outlives the run" true, and failing loudly past it treats a client that ignores
cancellation like a skill that breaks its contract.

## Consequences

- Mock durations leave out thinking time. Reports must show planner latency (p50/p95 from
  `planner_responded.latency_s`) next to durations and success rates (#51, #52).
- **Open: AIC** (closed by ADR 020, which decides that on AIC the world keeps stepping during
  a call; this decision still holds for the mock). In Gazebo the sim keeps running while the
  policy waits; we only stop stepping the world. Sim time still passes at the real-time factor,
  so `Task.time_limit` keeps counting, and the hold must keep the arm still (ADR 014). The adapter notices a cancel request
  when the world steps (ADR 012), so a cancel arriving during a planner call is seen only after
  the call answers or times out, up to the deadline. Before the planner runs on AIC: set the
  deadline below the time left, check hold behaviour during a long call, and decide whether a
  cancel should interrupt the wait.
- A timeout currently aborts the run. An escalation decision (#48) may change what a timeout
  leads to; the event stays the same.
- A run cancelled from outside while it waits on a call (Ctrl+C, or a cancelled eval task)
  does not go through the tree's stop or the 1 s grace: `asyncio.run` cancels the call on
  shutdown, as it does a skill. A client that ignores cancellation then blocks the shutdown.
- Every tick during a wait re-reads the tree's state without new signals, so tree nodes that
  run during a wait must not count ticks as time. The trees built today have only the planner
  running at that point.
