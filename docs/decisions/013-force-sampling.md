# 013: Force sampling at the F/T rate, summed the scorer's way

Date: 2026-11-09
Status: proposed

## Context

AIC penalises force over 20 N, summed over the whole task, when the sum passes 1 s. The scorer
reads every `/fts_broadcaster/wrench` message (50 Hz) and tares each one with the latest tare
offset, per axis. Then, for each message after the first, it adds the time since the previous
message whenever the tared magnitude is over 20 N (`ScoringTier2.cc:419-430`, `:863-903` @
`3ab0653`). Messages that arrive before the first tare are skipped.

The runtime samples the cell once per 50 ms step (ADR 011), which is about 2.5 wrench messages
per sample. ADR 006 makes the Monitor fire at half the AIC budget. Since #23, a sample may carry
`force_over_limit_s`, the time over the limit within its step. The fold, the Monitor and the
unsafe metric use it in place of the time between samples.

Current shape (since #26, the plan's defaults):

- The adapter subscribes to the wrench and to the controller state. Each wrench is tared in its
  callback with the newest tare, the scorer's order. The tared reading goes into a bounded buffer
  of 256. When the buffer is full the oldest reading is dropped, and the drops are counted.
- At each step end the world drains the buffer. The newest reading gives `force_n`. The
  scorer's rule over the drained readings, continued from the last reading of the previous step,
  gives `force_over_limit_s`.
- `wrench_valid` means the newest reading is at most 0.1 s of sim time old. A reading with a
  non-finite value is not used.

## Options

### Where force comes from

#### Option A — a direct subscription to the F/T broadcaster (current default)
- Pros: every message the scorer sees, at 50 Hz. The submission ACL allows the topic
  (`aic_zenoh_config.json5:288-323`).
- Cons: one more subscription per goal; its first message may arrive late (ADR 011).

#### Option B — `Observation.wrist_wrench` from `get_observation`
- Pros: what the policies use; nothing to subscribe to.
- Cons: arrives at the camera rate (20 Hz), only the newest wrench at the image time
  (`aic_adapter.cpp:213-224`). Most of the messages the scorer sums are missing.

### When to tare

#### Option A — in the wrench callback, with the newest tare seen then (current default)
- Pros: the scorer's order exactly. The tare is recomputed every control cycle, so a later tare
  could differ.
- Cons: the result depends on callback order between two topics.

#### Option B — at step end, with the tare current then
- Pros: simpler; one tare per step.
- Cons: readings early in the step are tared with a later offset than the scorer uses.

### What a sample's time over the limit is

#### Option A — the scorer's sum over the readings drained in the step, including the pair across the step boundary (current default)
- Pros: summed over a task, it equals the scorer's total for the same messages. Comparing
  the two on the VM (#28) is then a direct check.
- Cons: a step's share can differ a little from its elapsed sim time, because message stamps are
  not the step's clock. Not capped, so the total stays the scorer's.

#### Option B — the elapsed time of the step times the fraction of readings over the limit
- Pros: always at most the step's time.
- Cons: not the scorer's number; the two would disagree on every task.

### When the buffer fills

#### Option A — drop the oldest, count the drops (current default, 256 readings ≈ 5 s at 50 Hz)
- Pros: bounded memory; the newest force is never lost; a drop is visible in the log.
- Cons: a dropped reading is missing from the sum. After a drop the first kept reading is not
  paired with the reading before the gap (that pair would count the whole gap), so the sum
  can undercount after a long stall, never overcount.

#### Option B — block the callback until the policy thread drains
- Pros: nothing is lost.
- Cons: stalls the node's executor, which serves other callbacks too.

## Decision

<!-- Written by Shaun. -->

## Why

<!-- Written by Shaun: the reason in your own words, as you would explain it in an interview. -->

## Consequences

- The mock is unchanged: it never sets `force_over_limit_s`.
- `signal_sampled` records the drops per step. `cellrt replay --signals` prints the tared force
  and the time over 20 N for every sample.
- Known differences from the scorer's total:
  - Readings during the stop confirmation at run end (up to 1 s, ADR 012) are not drained, so
    force while the arm holds in contact is missing from our sum. The scorer counts it to the
    end of the task.
  - A stamp that goes backwards adds 0, where the scorer would add a negative gap; the log
    rejects negative time.
  - The ROS subscription keeps 5 messages (QoS depth, as aic_adapter). Messages lost there,
    while the node's executor is busy, are not counted as drops.
- To check on the VM (#28): our summed time over 20 N against the scorer's report for the same
  trial, and how often the buffer drops.
