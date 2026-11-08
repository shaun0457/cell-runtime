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

<!-- DRAFT by Claude from the code at 92d79e7 and the Phase 0 sessions. Shaun: rewrite
anything you would not say yourself, then set Status to accepted. -->

Granularity A and Store A. Every world signal sample is recorded as `signal_sampled`. Events go
into one SQLite table keyed by `(run_id, seq)`, with JSON payloads, WAL mode, and triggers that
abort any update, delete or replace. Signal samples are buffered up to 500; any decision event
flushes the buffer first.

## Why

The point of the log is that every claim can be re-checked from it. If only decisions were
recorded, nobody could verify a Monitor verdict afterwards, and classification accuracy would
become "trust me". With every sample stored, a verdict can be recomputed from the log, replay
can show the force curve, and a fork from step N has the full input history.

SQLite is in the standard library, is a single file that survives a VM restart, and lets the
eval ask cross-run questions in SQL (which units are finished, how many attempts each had).
Making append-only a database trigger means it holds even if a bug tries to rewrite history.

## Consequences

- Easier: verdicts and metrics are recomputed from the log; the resumable eval (ADR 010) is a
  query on the store.
- Harder: size. In Phase 0, 80 % of rows are signal samples (34 MB for 420 runs). A crash can
  lose up to 500 buffered samples, never a decision.
- Revisit in Phase 2: the AIC wrench topic may arrive faster than the mock's 20 Hz. The force
  sum must still see every sample (ADR 006); decide whether the store keeps every sample or a
  downsampled copy.
