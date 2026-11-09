# 004: Event granularity and the event store

Date: 2026-11-08
Status: proposed

## Context

Design principle 2: every step writes an append-only event, state is a fold over events, any
run can be replayed and forked from step N. Two questions follow: what counts as an event, and
how the log is stored. Signals arrive every world step (20 Hz in mock mode); an eval is ~400 runs
of a few hundred steps each.

## Options

### Granularity A — record every world signal sample (`signal_sampled`)
- Pros: every Monitor verdict can be recomputed from the log alone; replay can show force and
  depth curves; a future fork has the complete input history.
- Cons: hundreds to thousands of rows per run (~10⁵–10⁶ per eval); needs buffered writes.

### Granularity B — record decisions only (skill start/finish/progress, verdicts with a signal summary, tree changes, planner calls)
- Pros: small store; replay is a short list of steps.
- Cons: verdicts cannot be re-checked; a summary chosen today may miss what a later question
  needs.

### Store A — SQLite, one `events` table, append-only enforced by triggers
`(run_id, seq)` primary key, `ts` (clock time), `wall_ts` (debugging only), `type`, JSON
`payload`; WAL mode; `BEFORE UPDATE` / `BEFORE DELETE` triggers abort; no mutable runs table
(run lists come from `run_started` / `run_finished`). Writes are buffered: any non-signal event,
or 500 buffered events, flushes. A field added to an event later gets a default (e.g.
`signal_sampled.xy_offset_mm`, None in older logs), so a store written by older code still
reads; a change that cannot have a default would need a format version in `run_started`.
- Pros: stdlib only; a single file that survives a VM restart; queryable with `json_extract`;
  "append-only" is a database property, not a convention.
- Cons: a crash can lose up to 500 buffered signal samples (never a decision event); one writer
  per file.

### Store B — JSON Lines file per run
- Pros: trivially appendable and greppable.
- Cons: no cross-run queries (completed units, attempts) without scanning every file; nothing
  stops a rewrite.

## Decision

<!-- Written by Shaun. -->

## Why

<!-- Written by Shaun: the reason in your own words, as you would explain it in an interview. -->

## Consequences

What becomes easier, what becomes harder, what we will need to revisit.

- Payloads change shape over time, and the store keeps every row forever. Since Phase 1 (#21)
  `run_started` records `schema_version` (`SCHEMA_VERSION` in `runtime/events.py`). The field
  is required, so no writer can forget it; the store's one read path (`parse_stored`) gives
  rows written before it existed version 1. The rule that keeps old stores readable: a new
  payload field gets a default. Payloads still forbid unknown fields, so a store written by
  newer code is not readable by older code. The first change that cannot have a default will
  need the reader to convert rows by their run's version; nothing does that yet. Version 3
  (#23) added `signal_sampled.force_over_limit_s`, version 4 (#24)
  `run_started.geometry_source`, version 5 (#25) `hold_checked`, version 6 (#26)
  `signal_sampled.wrench_dropped`, version 7 (#45) `planner_responded`, version 8 (#47)
  `planner_timed_out`, version 9 (#56) `planner_preempted`, version 10 (#57)
  `planner_decided.waited_sim_s`, version 11 (ADR 023) `task_planned` and
  `task_plan_rejected`. A test hashes every payload's JSON schema and pins the hash per version,
  so a shape change without a bump fails.
