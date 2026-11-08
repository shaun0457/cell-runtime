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
or 500 buffered events, flushes.
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
