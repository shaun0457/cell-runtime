# 011: Time and the main loop over ROS sim time

Date: 2026-11-09
Status: proposed

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

## Decision

<!-- Written by Shaun. -->

## Why

<!-- Written by Shaun: the reason in your own words, as you would explain it in an interview. -->

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
- A run starts reading TF and F/T as soon as the goal arrives. CheatCode first waits up to 10 s
  for the ground-truth frames (`CheatCode.py:45-70`). The policy aborts at once
  (`ground_truth_unavailable`), and the first wrench may take longer than the Monitor's 0.2 s
  dropout window to arrive. A bounded wait for readiness is still open (#25 or #27).
