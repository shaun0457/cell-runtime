engine runs: 25; commit: 6e801278d2db7582556de1ca03c7bfb2dd4d0180; VM hours: 3.66; not ok: heldout1-001 (results_disk_full)

## Discovery

| scene | family | A | B | A mixed |
|---|---|---|---|---|
| empty_rail_sfp_0 | empty_rail_sfp | adapter_abort, adapter_abort, adapter_abort | adapter_abort, adapter_abort, adapter_abort | no |
| empty_rail_sfp_1 | empty_rail_sfp | adapter_abort, adapter_abort, adapter_abort | adapter_abort, adapter_abort, adapter_abort | no |
| empty_rail_sfp_2 | empty_rail_sfp | adapter_abort, adapter_abort, adapter_abort | adapter_abort, adapter_abort, adapter_abort | no |
| empty_rail_sfp_3 | empty_rail_sfp | adapter_abort, adapter_abort, adapter_abort | adapter_abort, adapter_abort, adapter_abort | no |
| inrange_sc_0 | inrange_sc | compensated, compensated, natural_failure_known | recovered, compensated, compensated | yes |
| inrange_sc_1 | inrange_sc | natural_failure_known, natural_failure_known, natural_failure_known | recovered, natural_failure_known, natural_failure_known | no |
| inrange_sc_2 | inrange_sc | compensated, compensated, compensated | natural_failure_known, compensated, compensated | no |
| inrange_sc_3 | inrange_sc | natural_failure_known, compensated, compensated | recovered, recovered, compensated | yes |
| inrange_sfp_0 | inrange_sfp | compensated, natural_failure_known, compensated | natural_failure_known, natural_failure_known, compensated | yes |
| inrange_sfp_1 | inrange_sfp | natural_failure_known, compensated, natural_failure_known | natural_failure_known, compensated, compensated | yes |
| inrange_sfp_2 | inrange_sfp | compensated, compensated, compensated | compensated, compensated, compensated | no |
| inrange_sfp_3 | inrange_sfp | compensated, compensated, compensated | compensated, compensated, compensated | no |
| tilt_sc_0 | tilt_sc | natural_failure_known, natural_failure_known, natural_failure_known | natural_failure_known, natural_failure_known, natural_failure_known | no |
| tilt_sc_1 | tilt_sc | natural_failure_known, natural_failure_known, natural_failure_known | natural_failure_known, recovered, natural_failure_known | no |
| tilt_sc_2 | tilt_sc | natural_failure_known, natural_failure_known, natural_failure_known | natural_failure_known, natural_failure_known, natural_failure_known | no |
| tilt_sc_3 | tilt_sc | natural_failure_known, natural_failure_known, natural_failure_known | natural_failure_known, recovered, compensated | no |
| tilt_sfp_0 | tilt_sfp | natural_failure_known, natural_failure_known, natural_failure_known | natural_failure_known, natural_failure_known, natural_failure_known | no |
| tilt_sfp_1 | tilt_sfp | natural_failure_known, natural_failure_known, natural_failure_known | natural_failure_known, natural_failure_known, natural_failure_known | no |
| tilt_sfp_2 | tilt_sfp | natural_failure_known, natural_failure_known, natural_failure_known | natural_failure_known, natural_failure_known, natural_failure_known | no |
| tilt_sfp_3 | tilt_sfp | natural_failure_known, natural_failure_known, natural_failure_known | recovered, recovered, natural_failure_known | no |
| yaw_edge_sfp_0 | yaw_edge_sfp | compensated, compensated, compensated | compensated, recovered, recovered | no |
| yaw_edge_sfp_1 | yaw_edge_sfp | compensated, compensated, compensated | compensated, compensated, compensated | no |
| yaw_edge_sfp_2 | yaw_edge_sfp | compensated, compensated, compensated | compensated, natural_failure_known, compensated | no |
| yaw_edge_sfp_3 | yaw_edge_sfp | compensated, compensated, compensated | compensated, compensated, compensated | no |

## Applied

| scene | family | change | requested | read back | tolerance | read | status |
|---|---|---|---|---|---|---|---|
| empty_rail_sfp_0 | empty_rail_sfp | - | - | - | - | 0/6 | not checked |
| empty_rail_sfp_1 | empty_rail_sfp | - | - | - | - | 0/6 | not checked |
| empty_rail_sfp_2 | empty_rail_sfp | - | - | - | - | 0/6 | not checked |
| empty_rail_sfp_3 | empty_rail_sfp | - | - | - | - | 0/6 | not checked |
| inrange_sc_0 | inrange_sc | - | - | - | - | 0/6 | not checked |
| inrange_sc_1 | inrange_sc | - | - | - | - | 0/6 | not checked |
| inrange_sc_2 | inrange_sc | - | - | - | - | 0/6 | not checked |
| inrange_sc_3 | inrange_sc | - | - | - | - | 0/6 | not checked |
| inrange_sfp_0 | inrange_sfp | - | - | - | - | 0/6 | not checked |
| inrange_sfp_1 | inrange_sfp | - | - | - | - | 0/6 | not checked |
| inrange_sfp_2 | inrange_sfp | - | - | - | - | 0/6 | not checked |
| inrange_sfp_3 | inrange_sfp | - | - | - | - | 0/6 | not checked |
| tilt_sc_0 | tilt_sc | tilt | 2.81 deg | 2.81 deg | 0.25 deg | 6/6 | applied |
| tilt_sc_1 | tilt_sc | tilt | 3.31 deg | 3.31 deg | 0.25 deg | 6/6 | applied |
| tilt_sc_2 | tilt_sc | tilt | 3.99 deg | 3.99 deg | 0.25 deg | 6/6 | applied |
| tilt_sc_3 | tilt_sc | tilt | 3.79 deg | 3.79 deg | 0.25 deg | 6/6 | applied |
| tilt_sfp_0 | tilt_sfp | tilt | 3.56 deg | 3.56 deg | 0.25 deg | 6/6 | applied |
| tilt_sfp_1 | tilt_sfp | tilt | 3.63 deg | 3.63 deg | 0.25 deg | 6/6 | applied |
| tilt_sfp_2 | tilt_sfp | tilt | 3.91 deg | 3.91 deg | 0.25 deg | 6/6 | applied |
| tilt_sfp_3 | tilt_sfp | tilt | 2.05 deg | 2.05 deg | 0.25 deg | 6/6 | applied |
| yaw_edge_sfp_0 | yaw_edge_sfp | yaw | 10.00 deg | 10.00 deg | 0.50 deg | 6/6 | applied |
| yaw_edge_sfp_1 | yaw_edge_sfp | yaw | 10.00 deg | 10.00 deg | 0.50 deg | 6/6 | applied |
| yaw_edge_sfp_2 | yaw_edge_sfp | yaw | 10.00 deg | 10.00 deg | 0.50 deg | 6/6 | applied |
| yaw_edge_sfp_3 | yaw_edge_sfp | yaw | 10.00 deg | 10.00 deg | 0.50 deg | 6/6 | applied |

- reference per port type: sc: tune-v1/nominal_sc/A/0/a1 (commit 6e801278d2db7582556de1ca03c7bfb2dd4d0180); sfp: tune-v1/nominal_sfp/A/0/a1 (commit 6e801278d2db7582556de1ca03c7bfb2dd4d0180)
- nominal spread, sc: 11 other trials; port 0.00 deg, 0.00 mm in the board's frame; plug 9.60 mm, 0.214 rad in the gripper's frame; wider than a tolerance
- nominal spread, sfp: 11 other trials; port 0.00 deg, 0.00 mm in the board's frame; plug 0.10 mm, 0.000 rad in the gripper's frame
- left out of every rate (not applied): none

## Reconciliation

| unit | setting | trial | run | outcome | insertion | agrees | >20N s | force penalty | contacts | contacts score | score | time | class |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| empty_rail_sfp_0_r0 | A | heldout1-003/trial_4 | heldout-v1/empty_rail_sfp_0/A/0/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_0_r0 | B | heldout1-010/trial_4 | heldout-v1/empty_rail_sfp_0/B/0/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_0_r1 | A | heldout1-011/trial_3 | heldout-v1/empty_rail_sfp_0/A/1/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_0_r1 | B | heldout1-002/trial_4 | heldout-v1/empty_rail_sfp_0/B/1/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_0_r2 | A | heldout1-017/trial_3 | heldout-v1/empty_rail_sfp_0/A/2/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_0_r2 | B | heldout1-016/trial_6 | heldout-v1/empty_rail_sfp_0/B/2/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_1_r0 | A | heldout1-003/trial_1 | heldout-v1/empty_rail_sfp_1/A/0/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_1_r0 | B | heldout1-004/trial_1 | heldout-v1/empty_rail_sfp_1/B/0/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_1_r1 | A | heldout1-013/trial_3 | heldout-v1/empty_rail_sfp_1/A/1/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_1_r1 | B | heldout1-022/trial_2 | heldout-v1/empty_rail_sfp_1/B/1/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_1_r2 | A | heldout1-009/trial_3 | heldout-v1/empty_rail_sfp_1/A/2/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_1_r2 | B | heldout1-014/trial_4 | heldout-v1/empty_rail_sfp_1/B/2/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_2_r0 | A | heldout1-011/trial_2 | heldout-v1/empty_rail_sfp_2/A/0/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_2_r0 | B | heldout1-006/trial_2 | heldout-v1/empty_rail_sfp_2/B/0/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_2_r1 | A | heldout1-011/trial_4 | heldout-v1/empty_rail_sfp_2/A/1/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_2_r1 | B | heldout1-002/trial_5 | heldout-v1/empty_rail_sfp_2/B/1/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_2_r2 | A | heldout1-015/trial_6 | heldout-v1/empty_rail_sfp_2/A/2/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_2_r2 | B | heldout1-014/trial_2 | heldout-v1/empty_rail_sfp_2/B/2/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_3_r0 | A | heldout1-019/trial_4 | heldout-v1/empty_rail_sfp_3/A/0/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_3_r0 | B | heldout1-024/trial_5 | heldout-v1/empty_rail_sfp_3/B/0/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_3_r1 | A | heldout1-005/trial_4 | heldout-v1/empty_rail_sfp_3/A/1/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_3_r1 | B | heldout1-010/trial_5 | heldout-v1/empty_rail_sfp_3/B/1/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_3_r2 | A | heldout1-007/trial_1 | heldout-v1/empty_rail_sfp_3/A/2/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_3_r2 | B | heldout1-018/trial_6 | heldout-v1/empty_rail_sfp_3/B/2/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| inrange_sc_0_r0 | A | heldout1-011/trial_1 | heldout-v1/inrange_sc_0/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.1 | mapped | compensated |
| inrange_sc_0_r0 | B | heldout1-010/trial_3 | heldout-v1/inrange_sc_0/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.13 | mapped | recovered |
| inrange_sc_0_r1 | A | heldout1-025/trial_1 | heldout-v1/inrange_sc_0/A/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.1 | mapped | compensated |
| inrange_sc_0_r1 | B | heldout1-008/trial_2 | heldout-v1/inrange_sc_0/B/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.47 | mapped | compensated |
| inrange_sc_0_r2 | A | heldout1-023/trial_6 | heldout-v1/inrange_sc_0/A/2/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| inrange_sc_0_r2 | B | heldout1-018/trial_1 | heldout-v1/inrange_sc_0/B/2/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.57 | mapped | compensated |
| inrange_sc_1_r0 | A | heldout1-017/trial_1 | heldout-v1/inrange_sc_1/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| inrange_sc_1_r0 | B | heldout1-002/trial_1 | heldout-v1/inrange_sc_1/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.69 | mapped | recovered |
| inrange_sc_1_r1 | A | heldout1-007/trial_2 | heldout-v1/inrange_sc_1/A/1/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| inrange_sc_1_r1 | B | heldout1-014/trial_3 | heldout-v1/inrange_sc_1/B/1/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| inrange_sc_1_r2 | A | heldout1-007/trial_5 | heldout-v1/inrange_sc_1/A/2/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| inrange_sc_1_r2 | B | heldout1-016/trial_3 | heldout-v1/inrange_sc_1/B/2/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| inrange_sc_2_r0 | A | heldout1-003/trial_2 | heldout-v1/inrange_sc_2/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.75 | mapped | compensated |
| inrange_sc_2_r0 | B | heldout1-024/trial_6 | heldout-v1/inrange_sc_2/B/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| inrange_sc_2_r1 | A | heldout1-025/trial_6 | heldout-v1/inrange_sc_2/A/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.62 | mapped | compensated |
| inrange_sc_2_r1 | B | heldout1-008/trial_5 | heldout-v1/inrange_sc_2/B/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.31 | mapped | compensated |
| inrange_sc_2_r2 | A | heldout1-005/trial_3 | heldout-v1/inrange_sc_2/A/2/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.57 | mapped | compensated |
| inrange_sc_2_r2 | B | heldout1-002/trial_2 | heldout-v1/inrange_sc_2/B/2/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.8 | mapped | compensated |
| inrange_sc_3_r0 | A | heldout1-015/trial_1 | heldout-v1/inrange_sc_3/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| inrange_sc_3_r0 | B | heldout1-008/trial_4 | heldout-v1/inrange_sc_3/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 91.5 | mapped | recovered |
| inrange_sc_3_r1 | A | heldout1-015/trial_2 | heldout-v1/inrange_sc_3/A/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.78 | mapped | compensated |
| inrange_sc_3_r1 | B | heldout1-020/trial_2 | heldout-v1/inrange_sc_3/B/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.36 | mapped | recovered |
| inrange_sc_3_r2 | A | heldout1-023/trial_2 | heldout-v1/inrange_sc_3/A/2/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.03 | mapped | compensated |
| inrange_sc_3_r2 | B | heldout1-022/trial_1 | heldout-v1/inrange_sc_3/B/2/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.64 | mapped | compensated |
| inrange_sfp_0_r0 | A | heldout1-013/trial_4 | heldout-v1/inrange_sfp_0/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.92 | mapped | compensated |
| inrange_sfp_0_r0 | B | heldout1-008/trial_3 | heldout-v1/inrange_sfp_0/B/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| inrange_sfp_0_r1 | A | heldout1-019/trial_6 | heldout-v1/inrange_sfp_0/A/1/a1 | failed | none | yes | 0.520 | -12.0 | 0 | 0.0 | -11.0 | - | natural_failure_known |
| inrange_sfp_0_r1 | B | heldout1-004/trial_3 | heldout-v1/inrange_sfp_0/B/1/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| inrange_sfp_0_r2 | A | heldout1-009/trial_1 | heldout-v1/inrange_sfp_0/A/2/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.71 | mapped | compensated |
| inrange_sfp_0_r2 | B | heldout1-020/trial_3 | heldout-v1/inrange_sfp_0/B/2/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.87 | mapped | compensated |
| inrange_sfp_1_r0 | A | heldout1-021/trial_3 | heldout-v1/inrange_sfp_1/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| inrange_sfp_1_r0 | B | heldout1-012/trial_1 | heldout-v1/inrange_sfp_1/B/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| inrange_sfp_1_r1 | A | heldout1-021/trial_4 | heldout-v1/inrange_sfp_1/A/1/a1 | success | target | yes | 0.020 | 0.0 | 0 | 0.0 | 92.87 | mapped | compensated |
| inrange_sfp_1_r1 | B | heldout1-012/trial_2 | heldout-v1/inrange_sfp_1/B/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.98 | mapped | compensated |
| inrange_sfp_1_r2 | A | heldout1-007/trial_6 | heldout-v1/inrange_sfp_1/A/2/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| inrange_sfp_1_r2 | B | heldout1-016/trial_1 | heldout-v1/inrange_sfp_1/B/2/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.9 | mapped | compensated |
| inrange_sfp_2_r0 | A | heldout1-011/trial_6 | heldout-v1/inrange_sfp_2/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.23 | mapped | compensated |
| inrange_sfp_2_r0 | B | heldout1-004/trial_2 | heldout-v1/inrange_sfp_2/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.34 | mapped | compensated |
| inrange_sfp_2_r1 | A | heldout1-015/trial_3 | heldout-v1/inrange_sfp_2/A/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.56 | mapped | compensated |
| inrange_sfp_2_r1 | B | heldout1-018/trial_5 | heldout-v1/inrange_sfp_2/B/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 91.94 | mapped | compensated |
| inrange_sfp_2_r2 | A | heldout1-023/trial_4 | heldout-v1/inrange_sfp_2/A/2/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.72 | mapped | compensated |
| inrange_sfp_2_r2 | B | heldout1-004/trial_5 | heldout-v1/inrange_sfp_2/B/2/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.97 | mapped | compensated |
| inrange_sfp_3_r0 | A | heldout1-023/trial_1 | heldout-v1/inrange_sfp_3/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.46 | mapped | compensated |
| inrange_sfp_3_r0 | B | heldout1-022/trial_5 | heldout-v1/inrange_sfp_3/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.73 | mapped | compensated |
| inrange_sfp_3_r1 | A | heldout1-003/trial_3 | heldout-v1/inrange_sfp_3/A/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.37 | mapped | compensated |
| inrange_sfp_3_r1 | B | heldout1-020/trial_1 | heldout-v1/inrange_sfp_3/B/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.54 | mapped | compensated |
| inrange_sfp_3_r2 | A | heldout1-007/trial_4 | heldout-v1/inrange_sfp_3/A/2/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.49 | mapped | compensated |
| inrange_sfp_3_r2 | B | heldout1-004/trial_4 | heldout-v1/inrange_sfp_3/B/2/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.52 | mapped | compensated |
| tilt_sc_0_r0 | A | heldout1-023/trial_5 | heldout-v1/tilt_sc_0/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_0_r0 | B | heldout1-004/trial_6 | heldout-v1/tilt_sc_0/B/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_0_r1 | A | heldout1-005/trial_1 | heldout-v1/tilt_sc_0/A/1/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_0_r1 | B | heldout1-012/trial_6 | heldout-v1/tilt_sc_0/B/1/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_0_r2 | A | heldout1-025/trial_2 | heldout-v1/tilt_sc_0/A/2/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_0_r2 | B | heldout1-018/trial_4 | heldout-v1/tilt_sc_0/B/2/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_1_r0 | A | heldout1-017/trial_6 | heldout-v1/tilt_sc_1/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_1_r0 | B | heldout1-024/trial_4 | heldout-v1/tilt_sc_1/B/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_1_r1 | A | heldout1-025/trial_3 | heldout-v1/tilt_sc_1/A/1/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_1_r1 | B | heldout1-020/trial_6 | heldout-v1/tilt_sc_1/B/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 90.98 | mapped | recovered |
| tilt_sc_1_r2 | A | heldout1-017/trial_2 | heldout-v1/tilt_sc_1/A/2/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_1_r2 | B | heldout1-020/trial_4 | heldout-v1/tilt_sc_1/B/2/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_2_r0 | A | heldout1-013/trial_2 | heldout-v1/tilt_sc_2/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_2_r0 | B | heldout1-022/trial_3 | heldout-v1/tilt_sc_2/B/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_2_r1 | A | heldout1-025/trial_5 | heldout-v1/tilt_sc_2/A/1/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_2_r1 | B | heldout1-024/trial_2 | heldout-v1/tilt_sc_2/B/1/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_2_r2 | A | heldout1-025/trial_4 | heldout-v1/tilt_sc_2/A/2/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_2_r2 | B | heldout1-022/trial_4 | heldout-v1/tilt_sc_2/B/2/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_3_r0 | A | heldout1-021/trial_2 | heldout-v1/tilt_sc_3/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_3_r0 | B | heldout1-002/trial_3 | heldout-v1/tilt_sc_3/B/0/a1 | failed | none | yes | 0.020 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_3_r1 | A | heldout1-019/trial_5 | heldout-v1/tilt_sc_3/A/1/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_3_r1 | B | heldout1-006/trial_1 | heldout-v1/tilt_sc_3/B/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 91.17 | mapped | recovered |
| tilt_sc_3_r2 | A | heldout1-009/trial_4 | heldout-v1/tilt_sc_3/A/2/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_3_r2 | B | heldout1-010/trial_1 | heldout-v1/tilt_sc_3/B/2/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.2 | mapped | compensated |
| tilt_sfp_0_r0 | A | heldout1-005/trial_2 | heldout-v1/tilt_sfp_0/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_0_r0 | B | heldout1-024/trial_1 | heldout-v1/tilt_sfp_0/B/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_0_r1 | A | heldout1-011/trial_5 | heldout-v1/tilt_sfp_0/A/1/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_0_r1 | B | heldout1-012/trial_4 | heldout-v1/tilt_sfp_0/B/1/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_0_r2 | A | heldout1-017/trial_5 | heldout-v1/tilt_sfp_0/A/2/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_0_r2 | B | heldout1-024/trial_3 | heldout-v1/tilt_sfp_0/B/2/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_1_r0 | A | heldout1-019/trial_1 | heldout-v1/tilt_sfp_1/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_1_r0 | B | heldout1-008/trial_1 | heldout-v1/tilt_sfp_1/B/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_1_r1 | A | heldout1-015/trial_5 | heldout-v1/tilt_sfp_1/A/1/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_1_r1 | B | heldout1-002/trial_6 | heldout-v1/tilt_sfp_1/B/1/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_1_r2 | A | heldout1-013/trial_6 | heldout-v1/tilt_sfp_1/A/2/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_1_r2 | B | heldout1-014/trial_1 | heldout-v1/tilt_sfp_1/B/2/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_2_r0 | A | heldout1-009/trial_5 | heldout-v1/tilt_sfp_2/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_2_r0 | B | heldout1-014/trial_6 | heldout-v1/tilt_sfp_2/B/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_2_r1 | A | heldout1-021/trial_6 | heldout-v1/tilt_sfp_2/A/1/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_2_r1 | B | heldout1-016/trial_4 | heldout-v1/tilt_sfp_2/B/1/a1 | failed | none | yes | 0.520 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_2_r2 | A | heldout1-021/trial_5 | heldout-v1/tilt_sfp_2/A/2/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_2_r2 | B | heldout1-006/trial_5 | heldout-v1/tilt_sfp_2/B/2/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_3_r0 | A | heldout1-003/trial_6 | heldout-v1/tilt_sfp_3/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_3_r0 | B | heldout1-012/trial_3 | heldout-v1/tilt_sfp_3/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 89.63 | mapped | recovered |
| tilt_sfp_3_r1 | A | heldout1-017/trial_4 | heldout-v1/tilt_sfp_3/A/1/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_3_r1 | B | heldout1-010/trial_6 | heldout-v1/tilt_sfp_3/B/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 89.81 | mapped | recovered |
| tilt_sfp_3_r2 | A | heldout1-003/trial_5 | heldout-v1/tilt_sfp_3/A/2/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_3_r2 | B | heldout1-014/trial_5 | heldout-v1/tilt_sfp_3/B/2/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| yaw_edge_sfp_0_r0 | A | heldout1-013/trial_1 | heldout-v1/yaw_edge_sfp_0/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.88 | mapped | compensated |
| yaw_edge_sfp_0_r0 | B | heldout1-016/trial_5 | heldout-v1/yaw_edge_sfp_0/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.91 | mapped | compensated |
| yaw_edge_sfp_0_r1 | A | heldout1-007/trial_3 | heldout-v1/yaw_edge_sfp_0/A/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.01 | mapped | compensated |
| yaw_edge_sfp_0_r1 | B | heldout1-020/trial_5 | heldout-v1/yaw_edge_sfp_0/B/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 86.96 | mapped | recovered |
| yaw_edge_sfp_0_r2 | A | heldout1-021/trial_1 | heldout-v1/yaw_edge_sfp_0/A/2/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.03 | mapped | compensated |
| yaw_edge_sfp_0_r2 | B | heldout1-006/trial_4 | heldout-v1/yaw_edge_sfp_0/B/2/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 89.21 | mapped | recovered |
| yaw_edge_sfp_1_r0 | A | heldout1-009/trial_6 | heldout-v1/yaw_edge_sfp_1/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.53 | mapped | compensated |
| yaw_edge_sfp_1_r0 | B | heldout1-018/trial_3 | heldout-v1/yaw_edge_sfp_1/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.78 | mapped | compensated |
| yaw_edge_sfp_1_r1 | A | heldout1-019/trial_2 | heldout-v1/yaw_edge_sfp_1/A/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.93 | mapped | compensated |
| yaw_edge_sfp_1_r1 | B | heldout1-006/trial_3 | heldout-v1/yaw_edge_sfp_1/B/1/a1 | success | target | yes | 0.040 | 0.0 | 0 | 0.0 | 92.62 | mapped | compensated |
| yaw_edge_sfp_1_r2 | A | heldout1-019/trial_3 | heldout-v1/yaw_edge_sfp_1/A/2/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.04 | mapped | compensated |
| yaw_edge_sfp_1_r2 | B | heldout1-018/trial_2 | heldout-v1/yaw_edge_sfp_1/B/2/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.09 | mapped | compensated |
| yaw_edge_sfp_2_r0 | A | heldout1-005/trial_6 | heldout-v1/yaw_edge_sfp_2/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.18 | mapped | compensated |
| yaw_edge_sfp_2_r0 | B | heldout1-010/trial_2 | heldout-v1/yaw_edge_sfp_2/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.4 | mapped | compensated |
| yaw_edge_sfp_2_r1 | A | heldout1-015/trial_4 | heldout-v1/yaw_edge_sfp_2/A/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.32 | mapped | compensated |
| yaw_edge_sfp_2_r1 | B | heldout1-022/trial_6 | heldout-v1/yaw_edge_sfp_2/B/1/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| yaw_edge_sfp_2_r2 | A | heldout1-023/trial_3 | heldout-v1/yaw_edge_sfp_2/A/2/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.51 | mapped | compensated |
| yaw_edge_sfp_2_r2 | B | heldout1-016/trial_2 | heldout-v1/yaw_edge_sfp_2/B/2/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.62 | mapped | compensated |
| yaw_edge_sfp_3_r0 | A | heldout1-009/trial_2 | heldout-v1/yaw_edge_sfp_3/A/0/a1 | success | target | yes | 0.020 | 0.0 | 0 | 0.0 | 93.07 | mapped | compensated |
| yaw_edge_sfp_3_r0 | B | heldout1-012/trial_5 | heldout-v1/yaw_edge_sfp_3/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.94 | mapped | compensated |
| yaw_edge_sfp_3_r1 | A | heldout1-005/trial_5 | heldout-v1/yaw_edge_sfp_3/A/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.15 | mapped | compensated |
| yaw_edge_sfp_3_r1 | B | heldout1-006/trial_6 | heldout-v1/yaw_edge_sfp_3/B/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.16 | mapped | compensated |
| yaw_edge_sfp_3_r2 | A | heldout1-013/trial_5 | heldout-v1/yaw_edge_sfp_3/A/2/a1 | success | target | yes | 0.020 | 0.0 | 0 | 0.0 | 92.39 | mapped | compensated |
| yaw_edge_sfp_3_r2 | B | heldout1-008/trial_6 | heldout-v1/yaw_edge_sfp_3/B/2/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.43 | mapped | compensated |

## A/B

| family | setting | trials | inserted | Wilson 95% | runtime success | false success | recovered | unsafe | with contacts | mean s | infrastructure |
|---|---|---|---|---|---|---|---|---|---|---|---|
| empty_rail_sfp | A | 12 | 0 | [0.00, 0.24] | 0 | 0 | 0 | 0 | 0 | 10.0 | 0 |
| empty_rail_sfp | B | 12 | 0 | [0.00, 0.24] | 0 | 0 | 0 | 0 | 0 | 10.0 | 0 |
| inrange_sc | A | 12 | 7 | [0.32, 0.81] | 7 | 0 | 0 | 0 | 0 | 28.7 | 0 |
| inrange_sc | B | 12 | 9 | [0.47, 0.91] | 9 | 0 | 4 | 0 | 0 | 33.8 | 0 |
| inrange_sfp | A | 12 | 9 | [0.47, 0.91] | 9 | 0 | 0 | 0 | 0 | 28.7 | 0 |
| inrange_sfp | B | 12 | 9 | [0.47, 0.91] | 9 | 0 | 0 | 0 | 0 | 39.1 | 0 |
| tilt_sc | A | 12 | 0 | [0.00, 0.24] | 0 | 0 | 0 | 0 | 0 | 26.2 | 0 |
| tilt_sc | B | 12 | 3 | [0.09, 0.53] | 3 | 0 | 2 | 0 | 0 | 48.9 | 0 |
| tilt_sfp | A | 12 | 0 | [0.00, 0.24] | 0 | 0 | 0 | 0 | 0 | 27.3 | 0 |
| tilt_sfp | B | 12 | 2 | [0.05, 0.45] | 2 | 0 | 2 | 0 | 0 | 54.0 | 0 |
| yaw_edge_sfp | A | 12 | 12 | [0.76, 1.00] | 12 | 0 | 0 | 0 | 0 | 32.2 | 0 |
| yaw_edge_sfp | B | 12 | 11 | [0.65, 0.99] | 11 | 0 | 2 | 0 | 0 | 37.1 | 0 |
| all | A | 72 | 28 | [0.28, 0.50] | 28 | 0 | 0 | 0 | 0 | 25.5 | 0 |
| all | B | 72 | 34 | [0.36, 0.59] | 34 | 0 | 10 | 0 | 0 | 37.1 | 0 |

claim (pooled, ADR 030): A [0.28, 0.50] (28/72), B [0.36, 0.59] (34/72); scenes: B better 7, A better 3, tied 14; sign test p = 0.3438: no detectable gain at this n

## Confusion

| setting | verdict class | verdicts | false alarm | tie | late | recovered | real failure | unconfirmed |
|---|---|---|---|---|---|---|---|---|
| A | FORCE_LIMIT | 1 | 0 | 0 | 0 | 0 | 1 | 1 |
| A | INSERT_STUCK | 30 | 0 | 0 | 0 | 0 | 30 | 30 |
| A | POSE_ERROR | 1 | 0 | 0 | 0 | 0 | 1 | 1 |
| B | FORCE_LIMIT | 2 | 0 | 0 | 0 | 0 | 2 | 1 |
| B | INSERT_STUCK | 87 | 0 | 0 | 0 | 14 | 73 | 23 |
| B | TIMEOUT | 4 | 0 | 0 | 0 | 0 | 4 | 2 |

- false alarms: 0; without the 0 trials judged as a whole: 0
- judged as a whole (no time mapping): none
- unconfirmed: a real failure the run stopped at, so the false-alarm count is a lower bound (ADR 029)

## Calibration

| port type | trials | samples | period ms | not fresh | dropped | free-space N | at rest N, approach | at rest N, align | INSERT_STUCK | FORCE_LIMIT |
|---|---|---|---|---|---|---|---|---|---|---|
| sc | 48 | 32745 | mean 50.4, sd 2.3, p95 52.0, max 218.0 | 0 | 0 | mean 0.97, sd 0.33, p95 1.53, max 2.25 | undefined | undefined | 60 verdicts, 0 repeats, 0 untimed; rise mean 0.48 s, max 0.51 s | none |
| sfp | 96 | 52100 | mean 50.3, sd 2.8, p95 52.0, max 516.0 | 0 | 0 | mean 1.27, sd 3.27, p95 2.65, max 127.40 | undefined | mean 3.46, sd 1.63, p95 4.87, max 4.88 | 57 verdicts, 0 repeats, 0 untimed; rise mean 0.50 s, max 0.51 s | 3 verdicts, 1 repeats, 0 untimed; rise mean 1.69 s, max 2.78 s; over the limit at it mean 0.51 s, max 0.52 s |
| all | 144 | 84845 | mean 50.3, sd 2.6, p95 52.0, max 516.0 | 0 | 0 | mean 1.15, sd 2.54, p95 1.64, max 127.40 | undefined | mean 3.46, sd 1.63, p95 4.87, max 4.88 | 117 verdicts, 0 repeats, 0 untimed; rise mean 0.49 s, max 0.51 s | 3 verdicts, 1 repeats, 0 untimed; rise mean 1.69 s, max 2.78 s; over the limit at it mean 0.51 s, max 0.52 s |

- a distribution is mean, sd, nearest-rank p95 and max; undefined under 10 values
- free space: every fresh sample of approach; at rest: a fresh sample after the plug moved under 0.05 mm in each of 5 steps, over 2.0 mm above the port's entrance
- rise: INSERT_STUCK from the first of the consecutive fresh samples above 10 N within the skill; FORCE_LIMIT from the start of the task's first step that added to the Monitor's sum over 20 N, shown with the verdict's sum (over the limit) against the Monitor's trigger; only the first verdict of a rise, or of the task for FORCE_LIMIT, is timed
- measured only: a threshold changes under ADR 030's D5, with ADR 032
