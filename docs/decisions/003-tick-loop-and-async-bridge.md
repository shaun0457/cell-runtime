# 003: Main loop and the py_trees ↔ async skill bridge

Date: 2026-11-08
Status: proposed

## Context

py_trees is tick-based and synchronous; skills are async and long-running (they map to ROS 2
actions later). Something has to own the loop, start skills, report RUNNING while they run,
and cancel them when the tree moves on. With a simulated clock (ADR 002), the loop also decides
when time advances.

## Options

### Option A — asyncio owns the loop, single-threaded lockstep
Each step: `tree.tick_once()` → drain (yield with `asyncio.sleep(0)` until every live skill task
is waiting on the clock or done, at most 100 yields, else `DrainError`) → `world.step(dt)` →
`clock.advance(dt)` → record a signal sample. `SkillBehaviour` starts the skill as a task on its
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

<!-- Written by Shaun. -->

## Why

<!-- Written by Shaun: the reason in your own words, as you would explain it in an interview. -->

## Consequences

What becomes easier, what becomes harder, what we will need to revisit.
