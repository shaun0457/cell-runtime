# 029: Referee data, and counting each verdict in time

Date: 2026-11-11
Status: accepted

**This decision changes what eval results mean.** It changes how a verdict on a sim run is
counted, and it amends ADR 015.

## Context

ADR 015 made the scorer the referee in the sim and fixed the counting for runs without an
injected fault:
- a verdict on a trial the scorer saw inserted is a false alarm;
- a verdict on a trial it did not see inserted is a natural failure.

That rule ignores **when** the insertion happened (#13 comment, #65 F1). Take a B run:
1. the plug jams on the rim, and the Monitor says INSERT_STUCK, correctly;
2. `retract_realign` frees it, and the plug goes in.

The scorer saw an insertion, so ADR 015 counts the correct verdict as a false alarm. The more
often B recovers, the worse the Monitor would look.

**What the referee data holds.**

| Data | Where it is | Time it carries |
|---|---|---|
| Insertion: `/scoring/insertion_event`, a `std_msgs/String` naming the port. The scorer stores each one it receives, so the last one counts; its own comment assumes one per task (`ScoringTier2.cc:444-447`). It splits the name on `/`, drops empty parts, and scores +75 when the first two parts are the target module and port, -12 otherwise (`:812-837`) | The trial's bag only; the YAML hides it when the policy returns False (#28) | The bag's receive time only: the message has no header |
| Off-limit contacts: `/aic/gazebo/contacts/off_limit` | The bag (`sample_config.yaml:22-24`) | Receive time |
| Force penalty, tier scores | `scoring.yaml`, per trial | None |
| Wrench and joint states | The bag | A header stamp in sim time, plus the receive time |

The bag writes each message with its receive and source timestamps (`ScoringTier2.cc:115-117`).
Both are wall-clock times from the middleware, while our events are on the run clock, which
follows sim time (ADR 011). The two clocks differ by the real-time factor (about 0.43), and that
factor drifts.

## Options

### 1. How a verdict is counted

#### Option A: per trial (ADR 015 as it is)
- Pros: needs no times.
- Cons: a recovered verdict counts as a false alarm.

#### Option B: per verdict, against the insertion time (D1)
- Pros: separates a Monitor that cried wolf from one that was right and was then rescued.
- Cons: needs the insertion time on our clock, and a rule for when that time is missing.

#### Option C: no false-alarm metric in the sim
- Pros: avoids the problem.
- Cons: drops the Monitor's main error rate exactly where it matters.

### 2. Where the insertion time on our clock comes from

#### Option A: the bag's receive time, mapped to sim time through header-stamped messages in the same bag
- Pros: independent of the runtime; every bag already has the wrench stream.
- Cons: the mapping has the resolution of that stream, plus the receive jitter.

#### Option B: our own height signal at the moment of seating
- Pros: precise, and already on our clock.
- Cons: it is the runtime's oracle geometry. The referee must be something the runtime cannot
  read (ADR 015).

#### Option C: the YAML
- Pros: none here.
- Cons: it carries no time.

### 3. A verdict and an insertion at the same moment

#### Option A: a false alarm
- Pros: conservative against the Monitor. The likeliest cause is the seating force itself,
  read as a failure.
- Cons: may count a few correct verdicts against it.

#### Option B: a class of its own
- Pros: no guess.
- Cons: one more row, for a case that should be rare.

## Decision

<!-- Decision and Why drafted by Claude from the option chosen in plan mode. Shaun delegated these
sections to Claude on 2026-11-09 and accepts them by merging; he may rewrite any of it. -->

Option B for counting, Option A for the time, and Option A for ties, with the number of ties
reported.

**The referee per trial** (`RefereeTrial`, built by `adapters/aic/scoring.py` from the trial's
`scoring.yaml` entry and its bag JSON):
- whether the plug was inserted, and into which port;
- the insertion time on the run clock, or none if it cannot be mapped;
- the off-limit contact count;
- the force penalty and the tier scores;
- whether the time mapping held.

**Mapping a time.**
1. Each `/fts_broadcaster/wrench` message in the bag gives a pair: its receive time (wall) and
   its header stamp (sim).
2. The insertion's receive time is converted to sim time by interpolating between the pair just
   before it and the pair just after it.
3. The run clock is that sim time minus the run's `sim_origin_s`, recorded in `world_ready`
   (#79).
4. The tolerance τ is the larger of 0.25 s and three times the sim-time gap between the two
   pairs used.
5. If either pair is more than 1 s of wall time from the insertion, the mapping fails for that
   trial.

**Counting**, for each verdict in a trial (a Monitor verdict, or a skill's own failed result) at
run-clock time t:

| The scorer | The verdict counts as |
|---|---|
| Inserted at or before t + τ | A **false alarm**. A tie within τ counts here, and ties are counted |
| Inserted after t + τ, with a `recovery_started` after the verdict and before the insertion | **Recovered**: the verdict was right, and what came after it fixed the failure |
| Inserted after t + τ, with no recovery in between | A **false alarm**, also counted as a **late insertion**: nothing acted on the verdict, and the plug went in anyway |
| Never inserted, or inserted into the wrong port | A **real failure**. When A stopped at that verdict, it is marked **unconfirmed** |

- "Inserted" means into the target port, here and in ADR 026's classes. A wrong-port insertion
  is never "inserted", and the report names it.
- **Late insertion.** The typical case is A: it holds the arm after its verdict, and the plug
  slides in during the hold. Without the rule, A would show recoveries it cannot make. Shaun
  decided on 2026-11-11 (gate review of this ADR, F1) to count it against the Monitor, like a
  tie. A scene whose A trial ends this way gets no B run in the pilot (D2).
- **Unconfirmed.** A stops at its first verdict, so whether the plug would have gone in is never
  observed when it did not. The false-alarm count is therefore a lower bound, and the report
  says so.
- **Fallback, when the time mapping fails** (D1): the trial is judged as a whole, by ADR 015's
  rule. Every verdict in a trial the scorer saw inserted counts as a false alarm; every verdict
  in a trial it did not counts as a real failure, unconfirmed in A as above. This is
  conservative against the Monitor: it counts a recovered verdict as a false alarm. The report
  lists every trial that fell back, and gives the false-alarm count with and without them.
- **Trials with no verdict:**
  - runtime success with an insertion: success or compensated (ADR 026);
  - runtime success without one: a **false success**;
  - any other end with no verdict (an adapter stop such as `ground_truth_unavailable`, or a
    cancel): an **adapter abort**.
- **Per trial**, ADR 026's class table gives the class: its first rows (infrastructure, false
  success, compensated, adapter abort), then the trial's first verdict. The per-verdict counts
  above feed the Monitor's rates.
- **The mock keeps its own rule.** It has no scorer. Its metrics and the committed baseline do
  not change.

**Where the code goes.**
- `cellrt/eval/referee.py` is pure, imports nothing from ROS or `adapters/`, and holds
  `judge(events, referee)`.
- `adapters/aic/scoring.py` parses the scorer's files.
- `tools/sim/in_container/bag_dump.py` writes the bag JSON: insertion events, contact messages,
  and wrench time pairs. It keeps at most 100,000 pairs per trial and refuses a bag with more:
  the broadcaster publishes at 50 Hz (`aic_bringup/config/aic_ros2_controllers.yaml:16-20`), so
  a 180 s trial gives about 9,000.
- The judge splits an insertion's port name as the scorer does, so a leading `/` does not
  matter.

## Why

- **A verdict is a claim about a moment, so it is judged at that moment.** "The plug is stuck
  now" is right or wrong at the time it is made. What B does afterwards is a different
  question, recovery, and it gets its own count. Counting it under the Monitor's error rate
  would punish a good Monitor for having a good recovery behind it.
- **The scorer's clock, not ours.** The runtime's height signal would give a cleaner time, but
  it is the thing being judged. The wrench stream ties the bag's wall clock to sim time without
  trusting anything the runtime computed. The weakness to name first: the mapping is only as
  good as the receive jitter, so it is checked per trial, and a trial without a good mapping
  falls back to a coarser rule instead of a guessed time.
- **Ties and late insertions against the Monitor**, because a reported improvement must not
  depend on the benefit of the doubt.

## Consequences

- ADR 015's counting rule for sim runs is replaced by this one. Its roles (referee, oracle,
  measured) stand.
- The reconciliation table (#82) shows, per trial, our outcome against the insertion, our time
  over 20 N against the scorer's force penalty, and the bag's contact messages against the
  scorer's contact score.
- Tests (#82): one per class, a tie within τ, a late insertion with and without a recovery, a
  failed mapping, and a wrong-port insertion, with and without a leading `/`.
- A run without `world_ready` (any run recorded before #79) can only be counted by the
  fallback.
- To revisit: if the scorer ever stamps its insertion event, take that time directly.
