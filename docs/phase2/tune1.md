engine runs: 15; commit: 6e801278d2db7582556de1ca03c7bfb2dd4d0180; VM hours: 1.92; not ok: tune1-002 (results_disk_full), tune1-007 (interrupted)

## Discovery

| scene | family | A | B | A mixed |
|---|---|---|---|---|
| empty_rail_sfp_0 | empty_rail_sfp | adapter_abort | adapter_abort | - |
| empty_rail_sfp_1 | empty_rail_sfp | adapter_abort | adapter_abort | - |
| empty_rail_sfp_2 | empty_rail_sfp | adapter_abort | adapter_abort | - |
| empty_rail_sfp_3 | empty_rail_sfp | adapter_abort | adapter_abort | - |
| inrange_sc_0 | inrange_sc | compensated | compensated | - |
| inrange_sc_1 | inrange_sc | natural_failure_known | recovered | - |
| inrange_sc_2 | inrange_sc | compensated | compensated | - |
| inrange_sc_3 | inrange_sc | compensated | compensated | - |
| inrange_sfp_0 | inrange_sfp | compensated | compensated | - |
| inrange_sfp_1 | inrange_sfp | natural_failure_known | compensated | - |
| inrange_sfp_2 | inrange_sfp | compensated | natural_failure_known | - |
| inrange_sfp_3 | inrange_sfp | compensated | compensated | - |
| nominal_sc | nominal_sc | natural_failure_known, natural_failure_known, compensated, compensated, compensated, natural_failure_known | natural_failure_known, natural_failure_known, compensated, compensated, compensated, natural_failure_known | yes |
| nominal_sfp | nominal_sfp | compensated, compensated, compensated, compensated, compensated, compensated | compensated, compensated, compensated, compensated, compensated, compensated | no |
| tilt_sc_0 | tilt_sc | natural_failure_known | natural_failure_unknown | - |
| tilt_sc_1 | tilt_sc | natural_failure_known | recovered | - |
| tilt_sc_2 | tilt_sc | natural_failure_known | false_success | - |
| tilt_sc_3 | tilt_sc | natural_failure_known | natural_failure_known | - |
| tilt_sfp_0 | tilt_sfp | natural_failure_known | recovered | - |
| tilt_sfp_1 | tilt_sfp | natural_failure_known | natural_failure_known | - |
| tilt_sfp_2 | tilt_sfp | natural_failure_known | natural_failure_known | - |
| tilt_sfp_3 | tilt_sfp | natural_failure_known | natural_failure_known | - |
| yaw_edge_sfp_0 | yaw_edge_sfp | natural_failure_known | compensated | - |
| yaw_edge_sfp_1 | yaw_edge_sfp | natural_failure_known | compensated | - |
| yaw_edge_sfp_2 | yaw_edge_sfp | compensated | compensated | - |
| yaw_edge_sfp_3 | yaw_edge_sfp | false_success | compensated | - |

## Applied

| scene | family | change | requested | read back | tolerance | read | status |
|---|---|---|---|---|---|---|---|
| empty_rail_sfp_0 | empty_rail_sfp | - | - | - | - | 0/2 | not checked |
| empty_rail_sfp_1 | empty_rail_sfp | - | - | - | - | 0/2 | not checked |
| empty_rail_sfp_2 | empty_rail_sfp | - | - | - | - | 0/2 | not checked |
| empty_rail_sfp_3 | empty_rail_sfp | - | - | - | - | 0/2 | not checked |
| inrange_sc_0 | inrange_sc | - | - | - | - | 0/2 | not checked |
| inrange_sc_1 | inrange_sc | - | - | - | - | 0/2 | not checked |
| inrange_sc_2 | inrange_sc | - | - | - | - | 0/2 | not checked |
| inrange_sc_3 | inrange_sc | - | - | - | - | 0/2 | not checked |
| inrange_sfp_0 | inrange_sfp | - | - | - | - | 0/2 | not checked |
| inrange_sfp_1 | inrange_sfp | - | - | - | - | 0/2 | not checked |
| inrange_sfp_2 | inrange_sfp | - | - | - | - | 0/2 | not checked |
| inrange_sfp_3 | inrange_sfp | - | - | - | - | 0/2 | not checked |
| nominal_sc | nominal_sc | - | - | - | - | 0/12 | not checked |
| nominal_sfp | nominal_sfp | - | - | - | - | 0/12 | not checked |
| tilt_sc_0 | tilt_sc | tilt | 3.23 deg | 3.23 deg | 0.25 deg | 2/2 | applied |
| tilt_sc_1 | tilt_sc | tilt | 2.75 deg | 2.75 deg | 0.25 deg | 2/2 | applied |
| tilt_sc_2 | tilt_sc | tilt | 2.60 deg | 2.60 deg | 0.25 deg | 2/2 | applied |
| tilt_sc_3 | tilt_sc | tilt | 3.65 deg | 3.65 deg | 0.25 deg | 2/2 | applied |
| tilt_sfp_0 | tilt_sfp | tilt | 2.02 deg | 2.02 deg | 0.25 deg | 2/2 | applied |
| tilt_sfp_1 | tilt_sfp | tilt | 3.63 deg | 3.63 deg | 0.25 deg | 2/2 | applied |
| tilt_sfp_2 | tilt_sfp | tilt | 3.97 deg | 3.97 deg | 0.25 deg | 2/2 | applied |
| tilt_sfp_3 | tilt_sfp | tilt | 2.84 deg | 2.84 deg | 0.25 deg | 2/2 | applied |
| yaw_edge_sfp_0 | yaw_edge_sfp | yaw | 10.00 deg | 10.00 deg | 0.50 deg | 2/2 | applied |
| yaw_edge_sfp_1 | yaw_edge_sfp | yaw | 10.00 deg | 10.00 deg | 0.50 deg | 2/2 | applied |
| yaw_edge_sfp_2 | yaw_edge_sfp | yaw | 10.00 deg | 10.00 deg | 0.50 deg | 2/2 | applied |
| yaw_edge_sfp_3 | yaw_edge_sfp | yaw | 10.00 deg | 10.00 deg | 0.50 deg | 2/2 | applied |

- reference per port type: sc: tune-v1/nominal_sc/A/0/a1 (commit 6e801278d2db7582556de1ca03c7bfb2dd4d0180); sfp: tune-v1/nominal_sfp/A/0/a1 (commit 6e801278d2db7582556de1ca03c7bfb2dd4d0180)
- nominal spread, sc: 11 other trials; port 0.00 deg, 0.00 mm in the board's frame; plug 9.60 mm, 0.214 rad in the gripper's frame; wider than a tolerance
- nominal spread, sfp: 11 other trials; port 0.00 deg, 0.00 mm in the board's frame; plug 0.10 mm, 0.000 rad in the gripper's frame
- left out of every rate (not applied): none

## Reconciliation

| unit | setting | trial | run | outcome | insertion | agrees | >20N s | force penalty | contacts | contacts score | score | time | class |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| empty_rail_sfp_0_r0 | A | tune1-013/trial_5 | tune-v1/empty_rail_sfp_0/A/0/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_0_r0 | B | tune1-004/trial_5 | tune-v1/empty_rail_sfp_0/B/0/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_1_r0 | A | tune1-003/trial_3 | tune-v1/empty_rail_sfp_1/A/0/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_1_r0 | B | tune1-015/trial_1 | tune-v1/empty_rail_sfp_1/B/0/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_2_r0 | A | tune1-009/trial_4 | tune-v1/empty_rail_sfp_2/A/0/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_2_r0 | B | tune1-012/trial_4 | tune-v1/empty_rail_sfp_2/B/0/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_3_r0 | A | tune1-011/trial_6 | tune-v1/empty_rail_sfp_3/A/0/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_3_r0 | B | tune1-008/trial_5 | tune-v1/empty_rail_sfp_3/B/0/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| inrange_sc_0_r0 | A | tune1-009/trial_3 | tune-v1/inrange_sc_0/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.78 | mapped | compensated |
| inrange_sc_0_r0 | B | tune1-010/trial_1 | tune-v1/inrange_sc_0/B/0/a3 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.34 | mapped | compensated |
| inrange_sc_1_r0 | A | tune1-003/trial_1 | tune-v1/inrange_sc_1/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| inrange_sc_1_r0 | B | tune1-010/trial_2 | tune-v1/inrange_sc_1/B/0/a2 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 91.33 | mapped | recovered |
| inrange_sc_2_r0 | A | tune1-005/trial_4 | tune-v1/inrange_sc_2/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.72 | mapped | compensated |
| inrange_sc_2_r0 | B | tune1-014/trial_2 | tune-v1/inrange_sc_2/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.75 | mapped | compensated |
| inrange_sc_3_r0 | A | tune1-013/trial_4 | tune-v1/inrange_sc_3/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.99 | mapped | compensated |
| inrange_sc_3_r0 | B | tune1-014/trial_1 | tune-v1/inrange_sc_3/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.07 | mapped | compensated |
| inrange_sfp_0_r0 | A | tune1-005/trial_3 | tune-v1/inrange_sfp_0/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.01 | mapped | compensated |
| inrange_sfp_0_r0 | B | tune1-014/trial_4 | tune-v1/inrange_sfp_0/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.88 | mapped | compensated |
| inrange_sfp_1_r0 | A | tune1-009/trial_6 | tune-v1/inrange_sfp_1/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| inrange_sfp_1_r0 | B | tune1-015/trial_3 | tune-v1/inrange_sfp_1/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.83 | mapped | compensated |
| inrange_sfp_2_r0 | A | tune1-011/trial_5 | tune-v1/inrange_sfp_2/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.91 | mapped | compensated |
| inrange_sfp_2_r0 | B | tune1-010/trial_6 | tune-v1/inrange_sfp_2/B/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| inrange_sfp_3_r0 | A | tune1-005/trial_2 | tune-v1/inrange_sfp_3/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.7 | mapped | compensated |
| inrange_sfp_3_r0 | B | tune1-012/trial_1 | tune-v1/inrange_sfp_3/B/0/a1 | success | target | yes | 0.020 | 0.0 | 0 | 0.0 | 92.25 | mapped | compensated |
| nominal_sc_r0 | A | tune1-011/trial_4 | tune-v1/nominal_sc/A/0/a1 | failed | none | yes | 0.502 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| nominal_sc_r0 | B | tune1-006/trial_3 | tune-v1/nominal_sc/B/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| nominal_sc_r1 | A | tune1-009/trial_1 | tune-v1/nominal_sc/A/1/a2 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| nominal_sc_r1 | B | tune1-006/trial_5 | tune-v1/nominal_sc/B/1/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| nominal_sc_r2 | A | tune1-001/trial_4 | tune-v1/nominal_sc/A/2/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.65 | mapped | compensated |
| nominal_sc_r2 | B | tune1-004/trial_2 | tune-v1/nominal_sc/B/2/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.01 | mapped | compensated |
| nominal_sc_r3 | A | tune1-011/trial_2 | tune-v1/nominal_sc/A/3/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.64 | mapped | compensated |
| nominal_sc_r3 | B | tune1-014/trial_5 | tune-v1/nominal_sc/B/3/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.44 | mapped | compensated |
| nominal_sc_r4 | A | tune1-011/trial_3 | tune-v1/nominal_sc/A/4/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.66 | mapped | compensated |
| nominal_sc_r4 | B | tune1-015/trial_2 | tune-v1/nominal_sc/B/4/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.65 | mapped | compensated |
| nominal_sc_r5 | A | tune1-003/trial_6 | tune-v1/nominal_sc/A/5/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| nominal_sc_r5 | B | tune1-014/trial_6 | tune-v1/nominal_sc/B/5/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| nominal_sfp_r0 | A | tune1-005/trial_1 | tune-v1/nominal_sfp/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.35 | mapped | compensated |
| nominal_sfp_r0 | B | tune1-008/trial_4 | tune-v1/nominal_sfp/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.64 | mapped | compensated |
| nominal_sfp_r1 | A | tune1-009/trial_5 | tune-v1/nominal_sfp/A/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.54 | mapped | compensated |
| nominal_sfp_r1 | B | tune1-004/trial_1 | tune-v1/nominal_sfp/B/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.75 | mapped | compensated |
| nominal_sfp_r2 | A | tune1-005/trial_5 | tune-v1/nominal_sfp/A/2/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.38 | mapped | compensated |
| nominal_sfp_r2 | B | tune1-014/trial_3 | tune-v1/nominal_sfp/B/2/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.37 | mapped | compensated |
| nominal_sfp_r3 | A | tune1-001/trial_5 | tune-v1/nominal_sfp/A/3/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.11 | mapped | compensated |
| nominal_sfp_r3 | B | tune1-004/trial_4 | tune-v1/nominal_sfp/B/3/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.53 | mapped | compensated |
| nominal_sfp_r4 | A | tune1-001/trial_3 | tune-v1/nominal_sfp/A/4/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.36 | mapped | compensated |
| nominal_sfp_r4 | B | tune1-012/trial_5 | tune-v1/nominal_sfp/B/4/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.08 | mapped | compensated |
| nominal_sfp_r5 | A | tune1-003/trial_4 | tune-v1/nominal_sfp/A/5/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.51 | mapped | compensated |
| nominal_sfp_r5 | B | tune1-006/trial_2 | tune-v1/nominal_sfp/B/5/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.3 | mapped | compensated |
| tilt_sc_0_r0 | A | tune1-013/trial_3 | tune-v1/tilt_sc_0/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_0_r0 | B | tune1-012/trial_3 | tune-v1/tilt_sc_0/B/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_unknown |
| tilt_sc_1_r0 | A | tune1-009/trial_2 | tune-v1/tilt_sc_1/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_1_r0 | B | tune1-004/trial_6 | tune-v1/tilt_sc_1/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 91.04 | mapped | recovered |
| tilt_sc_2_r0 | A | tune1-001/trial_1 | tune-v1/tilt_sc_2/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_2_r0 | B | tune1-004/trial_3 | tune-v1/tilt_sc_2/B/0/a1 | success | none | no | 0.000 | 0.0 | 0 | 0.0 | 66.08 | - | false_success |
| tilt_sc_3_r0 | A | tune1-003/trial_5 | tune-v1/tilt_sc_3/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_3_r0 | B | tune1-008/trial_6 | tune-v1/tilt_sc_3/B/0/a1 | failed | none | yes | 0.640 | -12.0 | 0 | 0.0 | -11.0 | - | natural_failure_known |
| tilt_sfp_0_r0 | A | tune1-003/trial_2 | tune-v1/tilt_sfp_0/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_0_r0 | B | tune1-010/trial_4 | tune-v1/tilt_sfp_0/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 87.25 | mapped | recovered |
| tilt_sfp_1_r0 | A | tune1-011/trial_1 | tune-v1/tilt_sfp_1/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_1_r0 | B | tune1-006/trial_6 | tune-v1/tilt_sfp_1/B/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_2_r0 | A | tune1-013/trial_2 | tune-v1/tilt_sfp_2/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_2_r0 | B | tune1-010/trial_5 | tune-v1/tilt_sfp_2/B/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_3_r0 | A | tune1-001/trial_2 | tune-v1/tilt_sfp_3/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_3_r0 | B | tune1-012/trial_6 | tune-v1/tilt_sfp_3/B/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| yaw_edge_sfp_0_r0 | A | tune1-013/trial_6 | tune-v1/yaw_edge_sfp_0/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| yaw_edge_sfp_0_r0 | B | tune1-012/trial_2 | tune-v1/yaw_edge_sfp_0/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.02 | mapped | compensated |
| yaw_edge_sfp_1_r0 | A | tune1-005/trial_6 | tune-v1/yaw_edge_sfp_1/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| yaw_edge_sfp_1_r0 | B | tune1-006/trial_4 | tune-v1/yaw_edge_sfp_1/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.38 | mapped | compensated |
| yaw_edge_sfp_2_r0 | A | tune1-013/trial_1 | tune-v1/yaw_edge_sfp_2/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.32 | mapped | compensated |
| yaw_edge_sfp_2_r0 | B | tune1-008/trial_2 | tune-v1/yaw_edge_sfp_2/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.55 | mapped | compensated |
| yaw_edge_sfp_3_r0 | A | tune1-001/trial_6 | tune-v1/yaw_edge_sfp_3/A/0/a1 | success | none | no | 0.000 | 0.0 | 0 | 0.0 | 67.62 | - | false_success |
| yaw_edge_sfp_3_r0 | B | tune1-010/trial_3 | tune-v1/yaw_edge_sfp_3/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.1 | mapped | compensated |

## A/B

| family | setting | trials | inserted | Wilson 95% | runtime success | false success | recovered | unsafe | with contacts | mean s | infrastructure |
|---|---|---|---|---|---|---|---|---|---|---|---|
| empty_rail_sfp | A | 4 | 0 | [0.00, 0.49] | 0 | 0 | 0 | 0 | 0 | 9.8 | 0 |
| empty_rail_sfp | B | 4 | 0 | [0.00, 0.49] | 0 | 0 | 0 | 0 | 0 | 10.0 | 0 |
| inrange_sc | A | 4 | 3 | [0.30, 0.95] | 3 | 0 | 0 | 0 | 0 | 29.3 | 0 |
| inrange_sc | B | 4 | 4 | [0.51, 1.00] | 4 | 0 | 1 | 0 | 0 | 33.4 | 0 |
| inrange_sfp | A | 4 | 3 | [0.30, 0.95] | 3 | 0 | 0 | 0 | 0 | 31.2 | 0 |
| inrange_sfp | B | 4 | 3 | [0.30, 0.95] | 3 | 0 | 0 | 0 | 0 | 39.4 | 0 |
| nominal_sc | A | 6 | 3 | [0.19, 0.81] | 3 | 0 | 0 | 0 | 0 | 24.4 | 0 |
| nominal_sc | B | 6 | 3 | [0.19, 0.81] | 3 | 0 | 0 | 0 | 0 | 33.1 | 0 |
| nominal_sfp | A | 6 | 6 | [0.61, 1.00] | 6 | 0 | 0 | 0 | 0 | 31.3 | 0 |
| nominal_sfp | B | 6 | 6 | [0.61, 1.00] | 6 | 0 | 0 | 0 | 0 | 31.3 | 0 |
| tilt_sc | A | 4 | 0 | [0.00, 0.49] | 0 | 0 | 0 | 0 | 0 | 26.0 | 0 |
| tilt_sc | B | 4 | 1 | [0.05, 0.70] | 2 | 1 | 1 | 0 | 0 | 31.6 | 0 |
| tilt_sfp | A | 4 | 0 | [0.00, 0.49] | 0 | 0 | 0 | 0 | 0 | 27.2 | 0 |
| tilt_sfp | B | 4 | 1 | [0.05, 0.70] | 1 | 0 | 1 | 0 | 0 | 61.8 | 0 |
| yaw_edge_sfp | A | 4 | 1 | [0.05, 0.70] | 2 | 1 | 0 | 0 | 0 | 29.5 | 0 |
| yaw_edge_sfp | B | 4 | 4 | [0.51, 1.00] | 4 | 0 | 0 | 0 | 0 | 31.5 | 0 |
| all | A | 36 | 16 | [0.30, 0.60] | 17 | 1 | 0 | 0 | 0 | 26.3 | 0 |
| all | B | 36 | 22 | [0.45, 0.75] | 23 | 1 | 3 | 0 | 0 | 33.8 | 0 |

claim: none; ADR 030 makes it on the held-out manifest only (descriptive table)

## Confusion

| setting | verdict class | verdicts | false alarm | tie | late | recovered | real failure | unconfirmed |
|---|---|---|---|---|---|---|---|---|
| A | FORCE_LIMIT | 1 | 0 | 0 | 0 | 0 | 1 | 1 |
| A | INSERT_STUCK | 14 | 0 | 0 | 0 | 0 | 14 | 14 |
| B | FORCE_LIMIT | 2 | 0 | 0 | 0 | 0 | 2 | 1 |
| B | INSERT_STUCK | 29 | 0 | 0 | 0 | 6 | 23 | 7 |
| B | UNKNOWN | 1 | 0 | 0 | 0 | 0 | 1 | 1 |

- false alarms: 0; without the 0 trials judged as a whole: 0
- judged as a whole (no time mapping): none
- unconfirmed: a real failure the run stopped at, so the false-alarm count is a lower bound (ADR 029)

## Calibration

| port type | trials | samples | period ms | not fresh | dropped | free-space N | at rest N, approach | at rest N, align | INSERT_STUCK | FORCE_LIMIT |
|---|---|---|---|---|---|---|---|---|---|---|
| sc | 28 | 16404 | mean 50.4, sd 1.2, p95 52.0, max 72.0 | 0 | 0 | mean 1.80, sd 3.42, p95 8.11, max 24.68 | undefined | undefined | 22 verdicts, 0 repeats, 0 untimed; rise mean 0.47 s, max 0.50 s | 3 verdicts, 1 repeats, 0 untimed; rise mean 6.47 s, max 12.39 s; over the limit at it mean 0.52 s, max 0.54 s |
| sfp | 44 | 25179 | mean 50.3, sd 3.0, p95 52.0, max 312.0 | 0 | 0 | mean 0.89, sd 0.32, p95 1.45, max 4.36 | mean 0.76, sd 0.20, p95 1.07, max 1.14 | mean 0.78, sd 0.18, p95 1.06, max 1.06 | 21 verdicts, 0 repeats, 0 untimed; rise mean 0.50 s, max 0.52 s | none |
| all | 72 | 41583 | mean 50.3, sd 2.4, p95 52.0, max 312.0 | 0 | 0 | mean 1.28, sd 2.29, p95 1.60, max 24.68 | mean 0.76, sd 0.20, p95 1.07, max 1.14 | mean 0.78, sd 0.18, p95 1.06, max 1.06 | 43 verdicts, 0 repeats, 0 untimed; rise mean 0.49 s, max 0.52 s | 3 verdicts, 1 repeats, 0 untimed; rise mean 6.47 s, max 12.39 s; over the limit at it mean 0.52 s, max 0.54 s |

- a distribution is mean, sd, nearest-rank p95 and max; undefined under 10 values
- free space: every fresh sample of approach; at rest: a fresh sample after the plug moved under 0.05 mm in each of 5 steps, over 2.0 mm above the port's entrance
- rise: INSERT_STUCK from the first of the consecutive fresh samples above 10 N within the skill; FORCE_LIMIT from the start of the task's first step that added to the Monitor's sum over 20 N, shown with the verdict's sum (over the limit) against the Monitor's trigger; only the first verdict of a rise, or of the task for FORCE_LIMIT, is timed
- measured only: a threshold changes under ADR 030's D5, with ADR 032
