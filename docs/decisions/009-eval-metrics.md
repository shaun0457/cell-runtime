# 009: How the eval measures A, B and C

Date: 2026-11-08
Status: proposed

<!-- DRAFT by Claude: Context, Options and Consequences only. Decision and Why are Shaun's. -->

## Context

`cellrt eval` runs scenarios × settings × seeds and prints one row per setting. The numbers
are what the project claims, so each one must be computable from the event log alone (pure
functions over events, testable on hand-built logs), must not trust the component it judges,
and must say "undefined" rather than a misleading 0 or 100 %.

Settled in the Phase 0 grilling (Q13, Q18, Q19): pass^k with `--k` (default 5); classification
accuracy per verdict with a confusion matrix; recovery rate = runs with a detected failure that
end in success; unsafe events from `signal_sampled`, independent of the Monitor; off-limit
contact left for Phase 2. Since then the AIC force rule was corrected (the penalty is on time
over 20 N **summed** over the task, `ScoringTier2.cc:870-900`), and the first full sweep showed
that "the fault active at the verdict" mislabels faults whose effect outlasts them.

Current shape (`cellrt/eval/metrics.py`):

| Metric | Definition |
|---|---|
| success rate | runs with outcome `success` / finished runs |
| pass^k | C(c, k) / C(n, k) per (scenario, setting), averaged over scenarios with n ≥ k; `-` if none |
| recovery rate | runs with ≥ 1 Monitor verdict ending in success / runs with ≥ 1 verdict; `-` if none |
| unsafe runs | runs whose valid tared force is over 20 N for more than 1 s in total |
| > 20 N s | mean of that total time per run |
| steps, duration | mean `skill_started` count and simulated duration, from `run_finished` |
| planner / task | `planner_called` events per finished run |
| accuracy | verdicts whose class matches the fault they are attributed to / all verdicts |

Interrupted runs (no `run_finished`) are left out of every metric.

## Options

### 1. Unsafe force

#### Option A — the AIC penalty: total time over 20 N above 1 s, per run
- Pros: the same rule the scorer applies, so "unsafe" means "would have lost 12 points"; a
  Monitor that fires late or never is still caught.
- Cons: binary per run; a run at 0.95 s looks as safe as one at 0 s (the mean time over 20 N
  column shows the difference).

#### Option B — count stretches over 20 N longer than 1 s (the plan's first wording)
- Pros: counts every long push separately.
- Cons: not what AIC penalises; many short pushes add up to a penalty it would not count.

#### Option C — count every sample over 20 N
- Pros: most sensitive.
- Cons: depends on the sample rate; no link to the score.

### 2. Which fault a verdict is judged against

#### Option A — faults active at any point while the failed skill attempt ran (current)
From the attempt's `skill_started` to the verdict. A verdict matching one of them is right;
with none it is a false alarm; a fault no verdict is attributed to is a miss.
- Pros: a fault whose effect outlasts it is credited. Sweep before this rule: 7 TIMEOUT false
  alarms and 3 misses, all controller stalls that ended a moment before the skill timed out.
- Cons: lenient: a fault that ended early in a long attempt can still "explain" an unrelated
  verdict (it is then counted as a wrong class, not as a false alarm).

#### Option B — only faults active at the moment of the verdict
- Pros: strict and simple.
- Cons: penalises the Monitor for the fault model's timing (the 7 + 3 above).

#### Option C — a fixed grace period after a fault clears
- Pros: simple.
- Cons: the length is arbitrary and differs per fault kind.

### 3. Rates with nothing to measure

#### Option A — undefined (`-`), left out of averages (current)
- Pros: never claims 0 % or 100 % from no data; pass^k with fewer runs than k is not a result.
- Cons: a row can have gaps; readers must know what `-` means.

#### Option B — 0
- Pros: always a number.
- Cons: misleading: A's recovery rate would read as a failure of recovery when no run failed.

## Decision

<!-- DRAFT by Claude from the code at 92d79e7 and the Phase 0 sessions. Shaun: rewrite
anything you would not say yourself, then set Status to accepted. -->

1. Unsafe means the AIC penalty rule, per run: valid tared force above 20 N for more than 1 s
   in total (Option A). The mean time over 20 N is reported beside it.
2. A verdict is judged against every fault active while the failed attempt ran (Option A). No
   such fault means a false alarm; a fault no verdict is attributed to is a miss.
3. A rate with nothing to measure is undefined, shown as `-`, and left out of averages
   (Option A).

## Why

A metric must not share a definition with the component it judges. Unsafe force is computed
from `signal_sampled`, not from Monitor verdicts, with the scorer's own rule. A Monitor that
fires late or never still shows up as an unsafe run, and "unsafe" means something concrete:
this run would have lost 12 points.

The attribution window is a correction from the first full sweep. The strict rule ("active at
the moment of the verdict") counted 7 false alarms and 3 misses, all controller stalls that
ended a moment before the timeout: it was grading the fault model's timing, not the Monitor.
The new rule is more lenient, and the ADR says so.

Undefined instead of 0: A never recovers, and "0 % recovery" would read as recovery failing
when there was nothing to recover.

## Consequences

- Phase 0 result, 7 scenarios × 20 seeds (`cellrt eval --seeds 20`, 45 s, 34 MB store):

  | setting | success | pass^5 | recovery | unsafe | accuracy |
  |---|---|---|---|---|---|
  | A | 14.3 % | 14.3 % | 0.0 % | 0 | 100 % |
  | B | 65.7 % | 45.4 % | 60.0 % | 0 | 100 % |
  | C | 80.0 % | 59.7 % | 76.7 % | 0 | 100 % |

- 100 % accuracy and 0 unsafe runs in mock mode say the plumbing is right, not that the Monitor
  is good: each mock fault was built to match one rule, and the Monitor fires on force at 0.5 s,
  half the AIC budget. The numbers that matter come from Gazebo in Phase 2.
- pass^5 is well below the success rate in B and C: recovery works on some seeds of a scenario
  and not others, and pass^k counts a scenario only when all k runs succeed.
- Each `eval` adds new attempts and reads only the runs it made; reading completed units from
  the store instead belongs to the resumable runner (#10).
