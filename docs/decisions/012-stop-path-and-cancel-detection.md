# 012: The stop path, and how the policy learns that its goal was cancelled

Date: 2026-11-09
Status: proposed

## Context

In the cell a run can end in many ways:

- the action client cancels the goal;
- aic_engine aborts it through the `cancel_task` service;
- the node is deactivated;
- the sim clock stalls or resets;
- the task time limit runs out;
- the Monitor preempts a skill, a skill times out, or something raises.

Each of these must leave the arm stopped and the log saying why. aic_model does not tell the
policy about a cancel. `insert_cable` gets no goal handle. The node's execute loop notices a cancel
at most 1 s of sim time later, sets `self.goal_handle = None`, returns, and can then accept the
next goal while the old policy thread is still commanding the arm (`aic_model.py:236-307` @
`3ab0653`). The node stores the handle as `goal_handle` before the policy thread starts
(`:179-181`), and its lifecycle state as `is_active`.

ADR 006 already makes the executor send HOLD whenever a skill ends or is stopped, behind a
command lease that a stopped skill cannot get past. ADR 014 covers what HOLD means in each
world.

Current shape (since #25, the plan's defaults):

- The policy captures the goal handle at the start of `insert_cable`. If it cannot be read, the
  run fails fast as `aic_goal_handle_unavailable`.
- At every step boundary, and while a step waits for sim time, `AicWorld` checks three things:
  the cancel request (`goal_cancel_requested`), the handle no longer active (`goal_aborted`),
  and the node no longer active (`node_deactivated`). Any of them raises `WorldStopped`, so all
  of these share the runner's one stop: stop the tree, revoke the lease, send HOLD, record
  `run_finished(aborted, <code>)`.
- A node call that raises while a step waits stops the run as `node_error`. A failed HOLD
  stops it at the next step. An exception from the runtime itself still stops every skill and
  sends HOLD (`runtime raised <Type>`).
- At run end the policy confirms the stop: linear TCP speed under 5 mm/s for 3 samples, within
  1.0 s of sim time. It records `hold_confirmed` or `hold_unconfirmed`, with the maximum speed
  seen.

## Options

### Detecting a cancel

#### Option A — keep the goal handle aic_model stores on the node, and read it at step boundaries (current default)
- Pros: notices a cancel within one 50 ms step, long before the node's 1 s loop. Needs nothing
  from upstream. The fake node can script every case.
- Cons: reads an internal attribute of aic_model. A change upstream breaks it, which is why
  the commit is pinned and the run fails fast when the attribute is missing.

#### Option B — rely on `Task.time_limit` and the node's own loop
- Pros: no internal attributes.
- Cons: after a cancel the old thread keeps commanding the arm while the next goal starts; a
  paused sim never reaches the time limit.

#### Option C — patch aic_model to pass a cancel token into `insert_cable`
- Pros: a clean interface.
- Cons: diverges from the fork's upstream and from the submission environment; out of scope
  ("the robot policies are reused as they are").

### How many stop paths

#### Option A — one: every reason becomes the runner's stop (current default)
- Pros: one place to test that the lease is revoked and HOLD is sent. Every reason looks the
  same in the log apart from its code.
- Cons: external reasons (a cancel) must be turned into an exception from `World.step`, which
  is a little indirect.

#### Option B — a separate path for external stops (cancel and deactivate) in the adapter
- Pros: the adapter controls the order of events.
- Cons: two code paths that can disagree about HOLD and the lease; one more set of tests.

### The outcome of a stop decided inside the tree (Monitor preemption, skill timeout)

#### Option A — the core's outcome: `failed`, with the failure class in the reason (current)
- Pros: the mock and the cell mean the same thing by `failed` (a known failure) and `aborted`
  (stopped from outside, or broken). The metrics read both the same way. Nothing in the mock
  results changes.
- Cons: the plan (D2) says every stop ends `aborted`. A reader of the plan expects that.

#### Option B — `aborted` for every stop in the cell, as the plan says
- Pros: one outcome for "the arm was stopped"; the reason tells them apart.
- Cons: done in the adapter only, the cell's runs and the mock's runs disagree on what
  `failed` means, and 4 cell test assertions change. Done in the core, where every tree
  FAILURE that is not a raise or a planner abort becomes `failed`
  (`cellrt/executor/runner.py:56`, as of `7329a75`), it has two forms:
  - every such end becomes `aborted`: a skill that reports its own failure and a failed
    precondition too. No run ends `failed` any more, so `failed` in `Outcome` and in the fold
    becomes unreachable, and about 20 assertions on a run's outcome change (16 outside the
    cell tests);
  - only the stops the Monitor decides (preemption and skill timeout; the executor already
    tells them apart, `failure_source == "monitor"` in `cellrt/executor/behaviours.py`): a
    skill's own failure stays `failed`, and fewer assertions change.

  No metric changes in either form: the metrics and the report only tell success from the rest
  (`cellrt/eval/metrics.py`).

### Confirming the arm stopped

#### Option A — at run end only: speed under 5 mm/s for 3 samples, within 1.0 s (current default)
- Pros: cheap, bounded, and recorded. A run whose arm keeps moving is visible in the log.
- Cons: says nothing about the stop between skills. 5 mm/s and 1.0 s are guesses until the VM
  runs (#28).

#### Option B — after every skill
- Pros: the next skill starts from rest.
- Cons: costs up to 1 s per skill, and belongs with recovery tuning (Phase 2, #13).

#### Option C — no confirmation; trust the controller
- Pros: nothing to build.
- Cons: a HOLD that never arrived goes unnoticed.

## Decision

<!-- Written by Shaun. -->

## Why

<!-- Written by Shaun: the reason in your own words, as you would explain it in an interview. -->

## Consequences

- Reason codes only, in the store and in the log: `goal_cancel_requested`, `goal_aborted`,
  `node_deactivated`, `aic_goal_handle_unavailable`, `sim_clock_stalled`, `sim_time_reset`,
  `ground_truth_unavailable`, `node_error`, `goal_handle_unreadable` (the handle failed to read
  mid-run; the clock is not suspect, so the stop is still confirmed), plus the core's own
  reasons. Exceptions are recorded by type only. The cell's codes are snake_case; the core's are
  fixed phrases (`time limit reached`, `approach failed: FORCE_LIMIT`, `runtime raised
  RuntimeError`). Neither carries upstream text.
- A cancel is read from the goal's status (canceling or canceled), so it is still named a
  cancel after the node's execute loop has marked the goal canceled.
- `hold_checked` (schema version 5) is recorded just before `run_finished` and does not change
  the outcome. After `sim_clock_stalled`, `sim_time_reset` or `node_error` it is recorded as
  unconfirmed without waiting, since the clock cannot be trusted to move. The check waits on
  sim time only and reads no TF, so a run stopped for missing ground truth is still confirmed.
- A run that stops while the world is being built (`aic_goal_handle_unavailable`, or a failing
  first read) has sent no command, so it neither holds nor records `hold_checked`.
- `run_finished.duration` leaves out the check, so it means the same as for a mock run. The
  check itself still takes sim time: a run stopped at `Task.time_limit` returns up to
  `HOLD_DEADLINE_S` (1.0 s) of sim time later. To check against the engine's own time limit on
  the VM (#28).
- The fork is pinned at `3ab0653`. Moving the pin means re-checking `goal_handle`, its
  `status`, and `is_active`.
