# #28 run report: the 3 sample-config tasks in the AIC sim, 2026-11-09

Setup: GCP g2-standard-8 + 1x L4 (asia-northeast3-a; T4 was out of stock in every zone tried),
aic @ `3ab0653`, cell-runtime @ `85c2216`, eval image
`ghcr.io/intrinsic-dev/aic/aic_eval@sha256:9aa2ffdbb946d38edde1bac7b5f02a44cfbea26e3b04a9c74e09f14c97472923`,
`ground_truth:=true`, setting A, no injected faults. Four engine runs, each with a fresh aic_model:

| run | policy | session label | engine result |
|---|---|---|---|
| main | `adapters.aic.RuntimePolicy` | `sample_config` | 0/3, score 1 + 1 + 1 |
| control | `aic_example_policies.ros.CheatCode` | — | 3/3, score 92.5 + 92.5 + 91.9 |
| cancel | `adapters.aic.RuntimePolicy` | `sample_config_cancel` | 0/3 (goal 1 cancelled on purpose) |
| diagnostic | RuntimePolicy with the `stuck_under_force` and `insert_stalled` rules switched off (VM-local subclass, not committed; the config is in `run_started`) | `diag_norules` | 0/3 |

A first diagnostic with only the stuck rule off (`diag_nostuck`) was stopped during goal 1 once
the cancel run showed that `insert_stalled` would fire in the same place; its partial run is in
the store.

## 1. Result per task (main run), kept apart

"Scorer insertion" is the scorer's own `/scoring/insertion_event` topic in the trial's bag. The
scorer's YAML cannot tell: when `insert_cable` returns False the engine marks the task not
completed, and tiers 2 and 3 score 0 ("Task not completed") whether or not the plug is in.

| task | port | `run_finished` | policy return | scorer insertion | scorer total |
|---|---|---|---|---|---|
| 1 | SFP (nic_card_mount_0/sfp_port_0) | failed: insert_scripted INSERT_STUCK | False | **inserted** | 1 |
| 2 | SFP (nic_card_mount_1/sfp_port_0) | failed: insert_scripted INSERT_STUCK | False | **inserted** | 1 |
| 3 | SC (sc_port_1) | failed: insert_scripted INSERT_STUCK | False | not inserted | 1 |

So two of the three plugs were seated, and the runtime called both of them failures.

## 2. Time over 20 N against the scorer's force penalty

| task | ours (sum of `force_over_limit_s`) | max tared force | scorer "insertion force" |
|---|---|---|---|
| 1 | 0.000 s | 13.1 N | no excessive force |
| 2 | 0.000 s | 13.5 N | no excessive force |
| 3 | 0.000 s | 13.5 N | no excessive force |

They agree. The comparison is weak: no run came near 20 N. The diagnostic's task 2 was the only
one over the limit (0.020 s ours), and the scorer gave no penalty, as expected under its 1 s budget.

## 3. Monitor verdicts, as findings

| run / task | verdict | evidence | what the sim shows |
|---|---|---|---|
| main 1, 2; cancel 2 | INSERT_STUCK `stuck_under_force` at 0.55 s into insert_scripted | 12.4–12.7 N, progress ~0 over 0.5 s, xy 0.37–0.54 mm | **false positive.** The plug was already seated (scorer insertion event) and pressing on the seat |
| main 3 | INSERT_STUCK `stuck_under_force` | 13.1 N, progress ~0, xy 0.62 mm | **true positive.** The SC plug is jammed at the port entrance (height 13.46 mm; the SC entrance frame is 13.64 mm above `sc_port_link`) |
| cancel 3 | UNKNOWN `insert_stalled` (1.0 s window) | 7.5 N, torque 1.42 N·m, xy 0.45 mm | **false positive.** The plug was seated (height 0.07 mm; the scorer published insertion events) |
| diagnostic 3 | TIMEOUT `skill_timeout` in align (38 s) | — | align held at z_offset 0.01 waiting for xy < 1.0 mm, but xy stayed at 11 mm, blocked at the entrance height (13.46 mm) |

Why the false positives: the tip-offset quirk (below) makes **align** seat the plug, so
insert_scripted starts with a seated plug. The arm keeps pushing, the force settles at 12–13 N,
and depth does not change. A seated plug and a jammed plug give the same signals; neither rule
can tell them apart.

## 4. Real-time factor

Sim time over wall time per run, from `signal_sampled`: 0.42–0.51 in the main run, 0.22–0.40 in
the diagnostic. About 0.43 overall, so a 25 s task takes about 58 s of wall time; one engine run
of 3 trials, reset included, takes 3.5–8 min.

## 5. Seated depth, for `VERIFY_MIN_DEPTH_MM`

`verify` measures `depth = max(0, -height)` of the plug frame below the port frame, and needs
10 mm (a placeholder). In the sim, a seated plug sits **at** the port frame, not 10 mm below it:

- SFP: height −0.98 mm (depth 0.98): tasks 1 and 2 of the main run, task 1 of the diagnostic
- SC: height +0.07 mm (depth 0): task 3 of the cancel run

With 10 mm, `verify` fails every seated plug. The diagnostic showed it: task 1 was seated
(scorer insertion event), and `verify` said "not seated" at depth 0.979. A rule of the form
"height ≤ a small tolerance" fits both port types. The SFP entrance is 45.8 mm above
`sfp_port_link`, the SC entrance 13.64 mm above `sc_port_link`.

## 6. Checks from the PRs' Might miss lists

- **CheatCode tip-offset quirk** (`geometry.py:7-9` at `85c2216`): confirmed. It matters: at z_offset 0.01
  (align's end), the SFP plug is already at its seat (height −0.98 mm from t ≈ 14 s, during
  align). CheatCode has the same quirk and passes, because it never stops to judge the plug
  between its descent and its 5 s settle.
- **Monitor insertion rules near the end of the descent**: both rules fire on a seated plug
  (section 3).
- **align's exit condition**: unlike CheatCode, align holds at z_offset 0.01 until xy < 1.0 mm.
  When the plug jams at the entrance, xy never converges, and align runs to its 38 s timeout
  (diagnostic task 3).
- **10 s readiness wait**: no problem seen. aic_model logs "lifecycle is not in the active state"
  once per engine run, before goal 1. That is the engine's first probe, and the goal then runs.
- **Hold check (5 mm/s, 1.0 s)**: `hold_confirmed` in 11 of 12 runs (max 0.14–3.3 mm/s, 0.15 s).
  The exception is diagnostic task 3: `hold_unconfirmed`, 33 mm/s over the full 1.0 s, after
  align's TIMEOUT. The arm was still moving 1 s after HOLD. That path is worth a test.

## 7. Cancel check

`/cancel_task` was called 27 s of wall time after goal 1 started (sim t = 14.1 s, during align):

- `sample_config_cancel/goal1/task_1/A/0/a1`: `skill_cancelled(align, terminated)`, then
  `hold_checked: hold_confirmed` (max 1.9 mm/s), then `run_finished: aborted, goal_aborted`.
- aic_model logged "Exiting insert_cable execute loop due to cancel_task request".
- Goal 2 started 5.8 s (wall) after goal 1's `run_finished`, not before it.

## 8. Acceptance criteria of #28

- [x] aic_model loads RuntimePolicy without a GetState timeout ("policy loaded" 5 s after start)
- [ ] The 3 sample-config tasks succeed: **no.** 2 of 3 plugs are seated, but the runtime fails
  them (sections 3 and 5). The SC task jams at the entrance in 2 of 3 runs (CheatCode: 3/3).
- [x] A cancelled goal stops the arm (reason `goal_aborted`, `hold_confirmed`)
- [x] Time over 20 N compared with the scorer: they agree (0 s; no penalty), weakly tested
- [x] `cellrt replay --signals` prints a sim run step by step with the force curve (main task 1, 519 lines)
- [x] Runbook followed once from a fresh VM. Its fixes are folded into
  `docs/runbooks/phase1-sim-run.md` (accelerator image + `libnvidia-gl-580-server`, pixi 0.67.2
  with `--frozen`, git bundle, L4 / zone sweep, gcloud from PowerShell on Windows, image digest)
- [x] CLAUDE.md Status records this report
- [ ] The roadmap revision after this run: drafted in PR #42, not merged

## 9. Where the data is

- The results disk `cellrt-results` (kept; the VM is stopped): `aic*/scoring.yaml`, the bags,
  `cellrt/aic.sqlite`, `logs/<run>.{sim,policy}.log`
- A local copy without bags, outside the repo (the store, the scoring YAML, the logs)

## 10. Since this run

This report stays as it was measured. What changed because of it:

- **Seated by height (#58):** `insert_scripted` ends once the plug is in the seated band, and
  `verify` judges by the same band, which replaces `VERIFY_MIN_DEPTH_MM`. Both read the oracle
  height (ADR 015).
- **Ground truth's roles are written down** in ADR 015, which was prompted by this run.
- **Later sim runs:** #61 ran settings A, B and C under an injected command dropout, and a rerun
  followed a review fix (`docs/sim-results.md`).
- **Still open:** the SC jam at the port entrance, the align/insert boundary, and acting on
  `hold_unconfirmed`. The 3/3 criterion above has not been met yet.
- **A count that does not add up:** section 6's "11 of 12 runs" does not match the
  RuntimePolicy goals of the engine runs above: 3 each in the main, cancel and diagnostic runs
  (the cancel run's goal 1 cancelled on purpose) and `diag_nostuck`'s goal 1, stopped part-way,
  so 10. The store is on the VM's results disk and was not counted again. Read it as every
  hold check but one confirmed, with no number (#65 review F7).
