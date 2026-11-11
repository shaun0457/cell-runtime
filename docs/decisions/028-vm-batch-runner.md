# 028: The VM batch runner, outside the hashed code

Date: 2026-11-11
Status: accepted

## Context

Phase 1's sim runs were driven by shell scripts kept outside the repository
(`cell-runtime-media/sim-close/scripts/vmc_run.sh`, `close_batch.sh`). They were never reviewed,
never tested, and they use the sample config. Phase 2 needs a runner that:
- writes engine configs from a queue;
- starts the sim and the policy;
- collects the store, `scoring.yaml` and the bags;
- resumes after the VM stops (CLAUDE.md, "Spot VMs can be preempted").

**Facts the runner must respect.**

| Fact | Where |
|---|---|
| `config_hash` covers every `.py` under `cellrt/` and `adapters/` and `uv.lock`; any edit there makes every finished unit stale | `cellrt/eval/runner.py:419-450`, `:527-531`; ADR 010 |
| One malformed trial stops the engine at parse: "Failed to parse trial" | `aic_engine.cpp:414-424` |
| The engine can also log "Engine failed to initialize" | `:352` |
| The engine can also log "Engine Stopped with Errors" | `:654` |
| `scoring.yaml` is written once, after the trial loop, so an engine run killed part-way leaves none | `:598-621`, `:2008-2013` |
| Each trial has its own bag, `bag_<trial id>_<timestamp>`, closed when that trial is scored | `:1304-1315`, `:1995-1998` |
| A trial's wall time with CheatCode-like skills is about 75 s at a real-time factor of 0.43 | CLAUDE.md, #28 |
| An engine start (sim, model node) takes about 2 min | the Phase 1 runs |
| The results disk is not remounted after a VM stop; files written then land on the boot disk, hidden under the mount | the F1 rerun on the VM |

The VM costs about US$0.9 an hour. Shaun set the ceilings: US$6 for the pilot and US$25 for the
phase, with a guard power-off for each session (D8).

## Options

### 1. Where the runner lives

#### Option A: `cellrt/eval/`, beside the mock runner
- Pros: one place for runners.
- Cons: it is hashed, so fixing a runner bug mid-campaign reruns every finished unit; it pulls
  subprocess and VM concerns into the ROS-free core.

#### Option B: `adapters/aic/`
- Pros: next to the code it drives.
- Cons: hashed too.

#### Option C: `tools/sim/`, outside the hashed trees
- Pros: the runner can be fixed without touching any result; the core stays free of process
  management.
- Cons: a runner change that does alter what Gazebo spawns would not show in the hash. The
  scene digest closes that gap (below).

#### Option D: shell scripts, as in Phase 1
- Pros: already exist.
- Cons: untested, and do not run on Windows, where the code is developed.

### 2. Trials per engine run

Each engine start costs about 2 min. A setting-A trial costs about 1.6 min.

| Trials per run | Overhead | Lost if the VM stops mid-run |
|---|---|---|
| 1 | 125% | 1 trial, about 2 min |
| 3 | 42% | 3 trials |
| 6 | 21% | 6 trials, about 10 min (US$0.15) |
| 12 | 10% | 12 trials, about 20 min |
| All | lowest | everything without a `scoring.yaml` |

### 3. When a sim unit is finished

#### Option A: ADR 010's rule only (a finished attempt with the current `config_hash`)
- Pros: one rule for mock and sim.
- Cons: a trial whose engine run died before `scoring.yaml` was written is "finished" but has no
  referee, so it can never be counted.

#### Option B: ADR 010's rule plus the trial's referee data (its `scoring.yaml` entry and its bag dump)
- Pros: every counted unit can be judged (ADR 029).
- Cons: for the sim, the counted attempt becomes the first finished attempt **with referee
  data**, not the first finished attempt.

## Decision

<!-- Decision and Why drafted by Claude from the option chosen in plan mode. Shaun delegated these
sections to Claude on 2026-11-09 and accepts them by merging; he may rewrite any of it. -->

Option C for the place, 6 trials per engine run, and Option B for completion.

**Place.**
- `tools/sim/` holds `scenes.py`, `batch.py`, `report.py`, and `in_container/bag_dump.py`. The
  bag reader runs with the eval container's Python and `rosbag2_py`, so the repository gains no
  dependency.
- Nothing under `cellrt/` or `adapters/` imports from `tools/`.
- What Gazebo spawns is covered by the scene digest, which is taken over the rendered trial
  (ADR 027). A runner change that alters the rendering changes the digest, and so the
  configuration.

**One engine run** (`run_engine(plan, runner)`, behind a `Runner` protocol so that tests use a
fake process runner):
1. **Guards.** `~/results` must be a mount point (`os.path.ismount`) with at least 10 GB free
   (`shutil.disk_usage`); otherwise the run is refused.
2. **Config.** Write the engine config for up to 6 unfinished units of one setting, and
   validate it (ADR 026).
3. **Sim.** Start the sim with `aic_engine_config_file:=<abs path>` and the env for the policy
   (`CELLRT_SETTING`, `CELLRT_MANIFEST`, `CELLRT_STORE`).
4. **Wait, with a bound for each wait.**

   | Wait | Bound |
   |---|---|
   | Sim ready | 300 s |
   | Policy ready | 180 s |
   | All trials processed | trials × (time_limit / 0.43 + 60 s) |

   With the 180 s limit, the last bound is 479 s per trial, about 48 min for 6 trials.
5. **Fail fast.** Any of the three engine error lines ends the run at once, with a reason code.
6. **Teardown.** On every exit path, stop the policy and the sim.
7. **Snapshot.** Copy the store (`.sqlite`, `-wal`, `-shm`) once no writer is running, and dump
   each trial's bag to JSON. Reports read only the copies, through
   `EventStore(read_only=True)`.
8. **Ledger.** Append one row to `ledger.jsonl`: the start and end times, the units, the exit
   code and the VM hours.

**The queue** (#81):
- The unit is one trial (ADR 027). The next engine config is built from the units still
  unfinished.
- A unit is finished when it has a finished attempt with the current `config_hash` **and** its
  referee data: its `scoring.yaml` entry and its bag dump. The report counts the first attempt
  that has both.
- A unit gets at most 3 attempts (ADR 010). One that runs out is reported as infrastructure.

**Cost guards.**
- The guard power-off is opt-in: `shutdown -h +N` at the start of the session, with N the
  smaller of the session's ceiling in minutes at US$0.9 an hour, and twice the estimate.
  For the pilot that is min(400, 300) = 300 minutes.
- Before each engine run, the runner adds the hours already spent (from the ledger) to that
  run's worst-case wait. If the sum would pass the session's ceiling, it stops the queue.
- C-LLM units stop once the session's snapshots count 50 model calls (D6).
- With the opt-in flag, the VM powers off when the queue is empty (CLAUDE.md).

## Why

- **Outside the hash, because the runner is plumbing, not part of the system under test.**
  The hash exists so that a changed Monitor rule can never reuse old results (ADR 010). A fixed
  typo in a log path should not cost a rerun of every finished trial. The weakness to name
  first: a runner change can still alter results by changing what is spawned. That is why the
  digest is taken over the rendered trial, not over the parameters.
- **Six trials** keeps the start overhead near a fifth while bounding the loss from a stop to
  about ten minutes. More trials per run would also make one malformed trial, which stops the
  engine at parse, cost more.
- **Referee data as part of "finished"**, because a trial that cannot be judged is not data.
  Losing it depends on when the VM stopped, not on whether the plug went in, so taking the
  first judged attempt does not bias the count. The exception is a scene that crashes the sim
  itself; it runs out of attempts and is reported, never silently dropped.
- **Every wait bounded, every exit torn down**: the session runs unattended, and the only
  guard against a hung engine burning money all night is the bound (D8).

## Consequences

- The out-of-repo scripts are retired once the tracer run (#78) passes on the VM.
- The batch runner can be fixed during the campaign without rerunning anything. The tag
  `phase2-heldout` (ADR 030) still freezes it for the held-out session.
- Every test of a bound uses the fake runner: a sim that never becomes ready, a policy that
  never connects, an engine that never finishes, each error line, a missing mount, a full disk.
- A VM stop mid-run costs at most one engine run of trials, and the ledger shows it.
- To revisit: a real Spot preemption test. It is out of scope this phase; an on-demand forced
  stop mid-queue stands in for it (#85).
