# 003: Main loop and the py_trees ↔ async skill bridge

Date: 2026-11-08
Status: accepted

## Context

py_trees is tick-based and synchronous; skills are async and long-running (they map to ROS 2
actions later). Something has to own the loop, start skills, report RUNNING while they run,
and cancel them when the tree moves on. With a simulated clock (ADR 002), the loop also decides
when time advances.

## Options

### Option A — asyncio owns the loop, single-threaded lockstep
Each step: `tree.tick_once()` → drain (yield with `asyncio.sleep(0)` until every live skill task
is waiting on the clock or done, at most 100 yields, else `DrainError`) → `world.step(dt)` →
`clock.advance` by the time the step reports (exactly `dt` in the mock; a sim may take longer,
since #23) → record a signal sample. `SkillBehaviour` starts the skill as a task on its
first tick, returns RUNNING until the task is done, maps the result to SUCCESS / FAILURE, and
cancels the task in `terminate()`.
- Pros: one thread and a FIFO ready queue, so the schedule is reproducible; no locks; the bridge
  lives in one class; a skill sees the world once per step, like a controller.
- Cons: a finished skill is observed on the next tick (one step of latency); "all tasks wait on
  the clock" is inferred by counting live tasks against pending clock sleeps, so a skill may only
  block on the clock; the drain bound is a heuristic.

### Option B — py_trees on the main thread, skills on an event loop in a second thread
Behaviours submit coroutines with `run_coroutine_threadsafe` and poll the futures.
- Pros: closer to how a ROS 2 executor with callbacks is arranged; the tree never awaits.
- Cons: shared world state across threads needs locks; thread scheduling breaks determinism;
  cancellation crosses a thread boundary.

### Option C — skills as generators stepped by the tree (no asyncio)
- Pros: fully deterministic, no event loop.
- Cons: breaks the skill contract (async, `CancelledError`); does not map to async ROS / LLM
  clients in later phases.

## Decision

<!-- Decision and Why drafted by Claude from the option the code took. Shaun delegated these
sections to Claude on 2026-11-09 and accepts them by merging; he may rewrite any of it. -->

Option A. asyncio owns one single-threaded loop that runs in lockstep: tick the tree, drain the
ready tasks until every live skill is waiting on the clock (bounded; otherwise `DrainError`),
step the world, advance the clock by the time the step reports, record a signal sample.
`SkillBehaviour` is the only bridge between py_trees and async skills: it starts the skill on
its first tick, returns RUNNING while the task runs, maps the result to SUCCESS / FAILURE and
cancels the task in `terminate()`.

## Why

Determinism decides it. One thread and asyncio's FIFO ready queue give the same schedule on
every run, with no locks. A second thread (option B) looks more like a ROS executor, but then
the order of events depends on the OS scheduler, which breaks reproducibility, and cancellation
has to cross a thread boundary. Generators (option C) would be deterministic too, but they break
the async skill contract that the ROS skills in Phase 1 and the LLM client in Phase 3 need.

The price is one tick of latency when a skill finishes (50 ms of simulated time), and the rule
that a skill may only block on the clock, which the drain bound enforces. Phase 1 kept the same
loop in the cell: the only blocking happens inside the world step, after the drain (ADR 011).

## Consequences

- A skill that breaks the contract cannot stop the eval: one that raises, or that ends
  cancelled on its own (not cancelled by the executor), aborts its run with
  `"<skill> raised <ExceptionType>"`; a planner task that does either is a rejected output.
- When a skill is stopped (finished, preempted, timed out, terminated) both its command access
  and its progress callback are revoked, so a stopped skill cannot move the arm or report
  progress that would land on the node's next attempt.
- Since #47 a planner call may wait on outside work (a model's answer): a task started with a
  deadline does not count as clock-bound in drain. While it runs the loop does not step the
  world; it waits on the call in wall time up to its deadline (ADR 016). Skill tasks keep the
  rule above.
- The Monitor judges the sample as it was logged (`executor.samples.signals_of` of the run
  state's last sample), not a fresh `world.signals()`: a world fed by callbacks (Phase 1) may
  change between two steps, and every verdict stays checkable from the log.
