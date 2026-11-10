# 011: Time and the main loop over ROS sim time

Date: 2026-11-09
Status: accepted

## Context

In Phase 0 the core's `SimClock` is the only time there is. It moves only when the runner
advances it after `world.step(dt)` (ADR 001, ADR 003), and the mock world says that exactly `dt`
passed. In the AIC cell, time belongs to Gazebo. The `aic_model` node runs with `use_sim_time`.
Its clock follows `/clock`, and the sim can run slower than real time, pause, or reset to 0.

`aic_model` calls `insert_cable` in a thread of its own for each goal. That thread has no event
loop (`aic_model.py:236-255` @ `3ab0653`). Callbacks run on the node's `MultiThreadedExecutor`.
CheatCode, the expert behind the scripted skills, paces itself with `sleep_for(0.05)` on the
node clock, which is sim-time aware (`policy.py:86-88`).

Two things must hold whatever is chosen:

- The core stays ROS-free and keeps its determinism in mock mode, with the same 20-seed table.
- A run must end even when the sim stops. `Task.time_limit` counts sim time, so a paused sim
  never reaches it.

Current shape (since #23 and #24, the plan's defaults):

- `World.step(dt)` returns the time that passed, and the runner advances `SimClock` by that.
- `AicWorld.step` blocks the policy thread, polling the node clock every 2 ms of wall time, until
  sim time has moved on by the period (50 ms). It returns the sim time that passed.
- A step that does not finish within 10 s of wall time raises `WorldStopped("sim_clock_stalled")`.
  Sim time going backwards raises `WorldStopped("sim_time_reset")`. Both end the run aborted.
- Each goal runs `asyncio.run` in the action thread, with a new `SimClock` starting at 0.

## Options

### Where skills get time from

#### Option A — the core `SimClock`, advanced by the sim time each step reports (current default)
- Pros: skills and the executor are identical in mock and sim. Timeouts, Monitor windows and the
  force sum all use one clock, and that clock is the one the log records. Replay needs nothing
  new.
- Cons: time moves in steps of at least one period, so a skill cannot wait for less than 50 ms.
  The Monitor's windows still count samples at the nominal period (#31 review F1), so a step
  longer than the period skews them.

#### Option B — a `RosClock` implementing the core `Clock` protocol, inside the adapter
- Pros: skills wait on real sim time with any resolution; the clock never needs to be advanced.
- Cons: sleeps resolve in ROS callbacks on other threads, which breaks the single-threaded
  lockstep that drain and determinism rely on (ADR 003). Two clock implementations would need
  testing.

#### Option C — wall-clock pacing (sleep the period in real time, read sim time only for the log)
- Pros: simplest; no polling.
- Cons: wrong whenever the real-time factor is not 1. Timeouts and the force sum would disagree
  with the scorer, which uses sim time.

### Where the event loop runs

#### Option A — `asyncio.run` per goal, in aic_model's action thread (current default)
- Pros: one goal is one loop, one clock and one run, with nothing shared between goals. Nothing
  outlives the goal.
- Cons: a new loop per goal (cheap). The thread blocks inside `step`, so the policy only
  notices a cancel at step boundaries (#25).

#### Option B — one long-lived loop thread per node; goals are submitted to it
- Pros: the loop could also serve ROS callbacks (`call_soon_threadsafe`).
- Cons: a cancelled goal's tasks can leak into the next goal; another thread to start and stop
  with the node's lifecycle.

#### Option C — no asyncio in the adapter: tick the tree synchronously
- Pros: no event loop in a ROS thread.
- Cons: skills are async by contract, so this would need a second executor; it goes against
  ADR 003.

### Detecting a sim that stopped

#### Option A — a wall-clock bound per step: the period of sim time must pass within N s (current default, N = 10)
- Pros: a hard bound on every wait, and simple to test. It also catches a sim that creeps
  (real-time factor below period / N, 0.005 here).
- Cons: a heavily loaded machine running below that factor is stopped even though it works.
  N is a guess until the VM runs (#28).

#### Option B — a bound on wall time since sim time last moved
- Pros: tolerates any real-time factor above zero.
- Cons: a sim that creeps (one tick a second) keeps a step waiting almost forever: no hard bound.

#### Option C — no detection; rely on aic_engine's own timeout and cancel
- Pros: nothing to build.
- Cons: with the sim paused, the engine's timeout may never fire either. The policy thread
  would block forever, holding the arm's last command.

### The step period

#### Option A — 50 ms (current default)
- Pros: the camera rate, at which observations exist, and CheatCode's step. Logs stay small:
  a 32 s task is about 640 samples.
- Cons: F/T runs at 50 Hz, so a sample sees about 2.5 wrench messages. The force sum needs
  `force_over_limit_s` (#26) rather than the sample alone.

#### Option B — 20 ms (the F/T rate)
- Pros: every wrench message has its own sample.
- Cons: 2.5 times the events; skills ported from CheatCode would need retimed loops.

### Waiting for the cell to be ready at a goal's start

TF and the F/T stream may not be up when a goal arrives: the subscriptions are made per goal,
and the wrench is usable only after the first controller state has given a tare.

#### Option A — a bounded wait in sim time before the tree starts: ground-truth frames and a fresh tared wrench, up to 10 s, polled every 50 ms (current default)
- Pros: the same bound as CheatCode's TF wait (`CheatCode.py:45-68`), so the policy is no less
  patient than the expert it is built from. Frames still missing at the bound stop the run as
  `ground_truth_unavailable`; a wrench still missing is left to the Monitor (SENSOR_DROPOUT), so
  a broken F/T stream is classified, not hidden. Cancels and a stalled sim are still seen during
  the wait. The run's clock counts the wait, so `Task.time_limit` does too, and the wait never
  outlasts it: a wait that uses the whole limit ends the run without moving.
- Cons: up to 10 s of a task's time limit is spent before anything moves. 10 s and 50 ms are
  CheatCode's numbers, not measured ones (#28).

#### Option B — subscribe once, when the policy is created, instead of per goal
- Pros: from the second goal on the stream is already flowing; no wait at all.
- Cons: the first goal still needs a wait; readings from between goals sit in the feed; ROS-only
  code that cannot be tested without the sim.

#### Option C — no wait: read at once, as before
- Pros: nothing to build.
- Cons: a goal that arrives before TF fails as `ground_truth_unavailable` where CheatCode would
  have waited, and a first wrench later than 0.2 s is a SENSOR_DROPOUT that is not a sensor
  fault.

## Decision

<!-- Decision and Why drafted by Claude from the option the code took. Shaun delegated these
sections to Claude on 2026-11-09 and accepts them by merging; he may rewrite any of it. -->

1. Skills get time from the core `SimClock`, advanced by the sim time each step reports
   (Option A).
2. One `asyncio.run` per goal, in aic_model's action thread (Option A).
3. A step whose 50 ms of sim time does not pass within 10 s of wall time stops the run as
   `sim_clock_stalled`; sim time going backwards stops it as `sim_time_reset` (Option A).
4. The step period is 50 ms (Option A); the faster F/T stream reaches the Monitor through
   `force_over_limit_s` (ADR 013).
5. At a goal's start, wait in sim time for the ground-truth frames and a fresh tared wrench, up
   to 10 s, polled every 50 ms (Option A). Missing frames end the run as
   `ground_truth_unavailable`; a missing wrench is left to the Monitor as SENSOR_DROPOUT.

## Why

One clock for everything is worth more than resolution below 50 ms. Fed by sim time, the same
`SimClock` drives skills, Monitor windows and the force sum in the mock and in the cell, and the
log records the clock everything used, so replay needs nothing new. A `RosClock` (option B)
would resolve sleeps on ROS threads and break the single-threaded lockstep ADR 003 rests on.
Wall-clock pacing (option C) is wrong whenever the real-time factor is not 1, and the scorer
counts sim time.

A loop per goal means nothing from a cancelled goal can leak into the next. The stall bound is
needed because `Task.time_limit` counts sim time: a paused sim never reaches it, and without a
bound the policy thread would block forever holding the arm's last command.

50 ms is the camera rate and CheatCode's step; force fidelity comes from summing the 50 Hz
stream, not from shorter steps. The readiness wait is as patient as CheatCode's own TF wait, so
the policy is no less robust than the expert it is built from, while a broken F/T stream is
still classified instead of hidden.

Weakness: 10 s, 50 ms and the wait bound are CheatCode's numbers, not measured ones; #28
measures the real-time factor, and the Monitor's windows still count samples at the nominal
period.

## Consequences

- The mock is unchanged: it reports its period exactly.
- A run in the cell starts its clock at 0 when the goal arrives. `ts` is sim time relative to
  the goal, not the sim's absolute time.
- Wall time appears only in the adapter's watchdog, never in the log (`wall_ts` stays
  debugging-only).
- To check on the VM (#28): the real-time factor and how long a step takes in wall time.
- Not bounded by the watchdog: the time `move_robot` takes. On the first motion update
  aic_model switches the controller's target mode with a synchronous service call that has no
  timeout (`aic_model.py:190-194`, `:312-320`). A controller that never answers would block the
  policy thread inside `world.command`. CheatCode has the same exposure.
- A goal first waits for the cell to be ready (Option A of the readiness options above): the
  first `skill_started` is stamped with the time the wait took. A wait that ends in a stop has
  sent no command, so it neither holds nor records `hold_checked` (ADR 012). Wrench readings
  drained during the wait are not summed into the time over 20 N (ADR 013). Whether 10 s is
  enough on the VM is to be checked there (#28).
- One thing may outlive a goal (#64 review F4): a model request's thread. The NIM client makes
  each request on a daemon thread, so it goes on while `step` blocks the loop (ADR 020). A call
  the run cancels ends at once, but its request runs on until it is answered or the SDK's
  timeout ends it, and its answer is dropped. The goal does not wait for it. That timeout is per
  network phase, so a host that sends a byte now and then keeps the request alive: at most 4
  such requests run at once per client, and a call beyond them is refused before it sends
  anything (`RequestsBusy`).
