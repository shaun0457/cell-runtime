engine runs: 12; commit: 6e801278d2db7582556de1ca03c7bfb2dd4d0180; VM hours: 1.41

## Discovery

| scene | family | A | B | A mixed |
|---|---|---|---|---|
| beyond_sc_0 | beyond_sc | natural_failure_known, compensated | - | yes |
| beyond_sfp_0 | beyond_sfp | natural_failure_known, compensated | compensated | yes |
| beyond_sfp_1 | beyond_sfp | compensated, compensated | - | no |
| beyond_sfp_2 | beyond_sfp | compensated, compensated | - | no |
| empty_rail_sfp_0 | empty_rail_sfp | adapter_abort, adapter_abort | - | no |
| inrange_sc_0 | inrange_sc | natural_failure_known, compensated | recovered | yes |
| inrange_sc_1 | inrange_sc | natural_failure_known, natural_failure_known | compensated | no |
| inrange_sc_2 | inrange_sc | natural_failure_known, compensated | compensated | yes |
| inrange_sc_3 | inrange_sc | natural_failure_known, compensated | compensated | yes |
| inrange_sfp_0 | inrange_sfp | compensated, natural_failure_known | compensated | yes |
| inrange_sfp_1 | inrange_sfp | compensated, compensated | - | no |
| inrange_sfp_2 | inrange_sfp | compensated, natural_failure_known | compensated | yes |
| inrange_sfp_3 | inrange_sfp | compensated, compensated | - | no |
| nominal_sc | nominal_sc | compensated, natural_failure_unknown | compensated | yes |
| nominal_sfp | nominal_sfp | compensated, compensated | - | no |
| reversed_sfp_0 | reversed_sfp | infrastructure | - | - |
| tilt_sc_0 | tilt_sc | natural_failure_known, natural_failure_known | natural_failure_known | no |
| tilt_sc_1 | tilt_sc | natural_failure_known, natural_failure_known | natural_failure_known | no |
| tilt_sfp_0 | tilt_sfp | natural_failure_known, natural_failure_known | natural_failure_known | no |
| trans_edge_sc_0 | trans_edge_sc | compensated, compensated | - | no |
| trans_edge_sfp_0 | trans_edge_sfp | compensated, compensated | - | no |
| yaw_edge_sfp_0 | yaw_edge_sfp | compensated, compensated | - | no |
| yaw_edge_sfp_1 | yaw_edge_sfp | natural_failure_known, compensated | recovered | yes |

## Applied

| scene | family | change | requested | read back | tolerance | read | status |
|---|---|---|---|---|---|---|---|
| beyond_sc_0 | beyond_sc | grasp shift | 5.00 mm | 10.30 mm, turned 0.195 rad | 1.00 mm; turn 0.020 rad | 2/2 | not applied |
| beyond_sfp_0 | beyond_sfp | yaw | 20.00 deg | 20.00 deg | 0.50 deg | 3/3 | applied |
| beyond_sfp_1 | beyond_sfp | grasp shift | 5.00 mm | 5.00 mm, turned 0.000 rad | 1.00 mm; turn 0.020 rad | 2/2 | applied |
| beyond_sfp_2 | beyond_sfp | grasp turn | 0.120 rad | 0.120 rad | 0.020 rad | 2/2 | applied |
| empty_rail_sfp_0 | empty_rail_sfp | - | - | - | - | 0/2 | not checked |
| inrange_sc_0 | inrange_sc | - | - | - | - | 0/3 | not checked |
| inrange_sc_1 | inrange_sc | - | - | - | - | 0/3 | not checked |
| inrange_sc_2 | inrange_sc | - | - | - | - | 0/3 | not checked |
| inrange_sc_3 | inrange_sc | - | - | - | - | 0/3 | not checked |
| inrange_sfp_0 | inrange_sfp | - | - | - | - | 0/3 | not checked |
| inrange_sfp_1 | inrange_sfp | - | - | - | - | 0/2 | not checked |
| inrange_sfp_2 | inrange_sfp | - | - | - | - | 0/3 | not checked |
| inrange_sfp_3 | inrange_sfp | - | - | - | - | 0/2 | not checked |
| nominal_sc | nominal_sc | - | - | - | - | 0/3 | not checked |
| nominal_sfp | nominal_sfp | - | - | - | - | 0/2 | not checked |
| reversed_sfp_0 | reversed_sfp | - | - | - | - | 0/0 | not checked |
| tilt_sc_0 | tilt_sc | tilt | 2.00 deg | 2.00 deg | 0.25 deg | 3/3 | applied |
| tilt_sc_1 | tilt_sc | tilt | 4.00 deg | 4.00 deg | 0.25 deg | 3/3 | applied |
| tilt_sfp_0 | tilt_sfp | tilt | 2.00 deg | 2.00 deg | 0.25 deg | 3/3 | applied |
| trans_edge_sc_0 | trans_edge_sc | shift | 110.00 mm | 110.00 mm | 1.00 mm | 2/2 | applied |
| trans_edge_sfp_0 | trans_edge_sfp | shift | 84.00 mm | 84.00 mm | 1.00 mm | 2/2 | applied |
| yaw_edge_sfp_0 | yaw_edge_sfp | yaw | 10.00 deg | 10.00 deg | 0.50 deg | 2/2 | applied |
| yaw_edge_sfp_1 | yaw_edge_sfp | yaw | 10.00 deg | 10.00 deg | 0.50 deg | 3/3 | applied |

- reference per port type: sc: pilot-v1/nominal_sc/A/0/a1 (commit 6e801278d2db7582556de1ca03c7bfb2dd4d0180); sfp: pilot-v1/nominal_sfp/A/0/a1 (commit 6e801278d2db7582556de1ca03c7bfb2dd4d0180)
- nominal spread, sc: 2 other trials; port 0.00 deg, 0.00 mm in the board's frame; plug 11.82 mm, 0.190 rad in the gripper's frame; wider than a tolerance
- nominal spread, sfp: 1 other trials; port 0.00 deg, 0.00 mm in the board's frame; plug 0.01 mm, 0.000 rad in the gripper's frame
- left out of every rate (not applied): beyond_sc_0

## Reconciliation

| unit | setting | trial | run | outcome | insertion | agrees | >20N s | force penalty | contacts | contacts score | score | time | class |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| beyond_sc_0_r0 | A | pilot1-003/trial_6 | pilot-v1/beyond_sc_0/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| beyond_sc_0_r1 | A | pilot1-007/trial_4 | pilot-v1/beyond_sc_0/A/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.65 | mapped | compensated |
| beyond_sfp_0_r0 | A | pilot1-003/trial_3 | pilot-v1/beyond_sfp_0/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| beyond_sfp_0_r0 | B | pilot1-010/trial_6 | pilot-v1/beyond_sfp_0/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.07 | mapped | compensated |
| beyond_sfp_0_r1 | A | pilot1-007/trial_1 | pilot-v1/beyond_sfp_0/A/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.04 | mapped | compensated |
| beyond_sfp_1_r0 | A | pilot1-003/trial_4 | pilot-v1/beyond_sfp_1/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.2 | mapped | compensated |
| beyond_sfp_1_r1 | A | pilot1-007/trial_2 | pilot-v1/beyond_sfp_1/A/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.6 | mapped | compensated |
| beyond_sfp_2_r0 | A | pilot1-003/trial_5 | pilot-v1/beyond_sfp_2/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.07 | mapped | compensated |
| beyond_sfp_2_r1 | A | pilot1-007/trial_3 | pilot-v1/beyond_sfp_2/A/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.12 | mapped | compensated |
| empty_rail_sfp_0_r0 | A | pilot1-004/trial_4 | pilot-v1/empty_rail_sfp_0/A/0/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| empty_rail_sfp_0_r1 | A | pilot1-008/trial_2 | pilot-v1/empty_rail_sfp_0/A/1/a1 | aborted | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | adapter_abort |
| inrange_sc_0_r0 | A | pilot1-002/trial_1 | pilot-v1/inrange_sc_0/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| inrange_sc_0_r0 | B | pilot1-010/trial_2 | pilot-v1/inrange_sc_0/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.44 | mapped | recovered |
| inrange_sc_0_r1 | A | pilot1-005/trial_5 | pilot-v1/inrange_sc_0/A/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.78 | mapped | compensated |
| inrange_sc_1_r0 | A | pilot1-002/trial_2 | pilot-v1/inrange_sc_1/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| inrange_sc_1_r0 | B | pilot1-009/trial_1 | pilot-v1/inrange_sc_1/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.74 | mapped | compensated |
| inrange_sc_1_r1 | A | pilot1-005/trial_6 | pilot-v1/inrange_sc_1/A/1/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| inrange_sc_2_r0 | A | pilot1-002/trial_3 | pilot-v1/inrange_sc_2/A/0/a1 | failed | none | yes | 0.504 | -12.0 | 0 | 0.0 | -11.0 | - | natural_failure_known |
| inrange_sc_2_r0 | B | pilot1-010/trial_3 | pilot-v1/inrange_sc_2/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.97 | mapped | compensated |
| inrange_sc_2_r1 | A | pilot1-006/trial_1 | pilot-v1/inrange_sc_2/A/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.27 | mapped | compensated |
| inrange_sc_3_r0 | A | pilot1-002/trial_4 | pilot-v1/inrange_sc_3/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| inrange_sc_3_r0 | B | pilot1-010/trial_4 | pilot-v1/inrange_sc_3/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 91.35 | mapped | compensated |
| inrange_sc_3_r1 | A | pilot1-006/trial_2 | pilot-v1/inrange_sc_3/A/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.72 | mapped | compensated |
| inrange_sfp_0_r0 | A | pilot1-001/trial_3 | pilot-v1/inrange_sfp_0/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.85 | mapped | compensated |
| inrange_sfp_0_r0 | B | pilot1-009/trial_6 | pilot-v1/inrange_sfp_0/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.91 | mapped | compensated |
| inrange_sfp_0_r1 | A | pilot1-005/trial_1 | pilot-v1/inrange_sfp_0/A/1/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| inrange_sfp_1_r0 | A | pilot1-001/trial_4 | pilot-v1/inrange_sfp_1/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.97 | mapped | compensated |
| inrange_sfp_1_r1 | A | pilot1-005/trial_2 | pilot-v1/inrange_sfp_1/A/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.98 | mapped | compensated |
| inrange_sfp_2_r0 | A | pilot1-001/trial_5 | pilot-v1/inrange_sfp_2/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.1 | mapped | compensated |
| inrange_sfp_2_r0 | B | pilot1-010/trial_1 | pilot-v1/inrange_sfp_2/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.0 | mapped | compensated |
| inrange_sfp_2_r1 | A | pilot1-005/trial_3 | pilot-v1/inrange_sfp_2/A/1/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| inrange_sfp_3_r0 | A | pilot1-001/trial_6 | pilot-v1/inrange_sfp_3/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.49 | mapped | compensated |
| inrange_sfp_3_r1 | A | pilot1-005/trial_4 | pilot-v1/inrange_sfp_3/A/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.92 | mapped | compensated |
| nominal_sc_r0 | A | pilot1-001/trial_2 | pilot-v1/nominal_sc/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.0 | mapped | compensated |
| nominal_sc_r0 | B | pilot1-009/trial_5 | pilot-v1/nominal_sc/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.65 | mapped | compensated |
| nominal_sc_r1 | A | pilot1-004/trial_6 | pilot-v1/nominal_sc/A/1/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_unknown |
| nominal_sfp_r0 | A | pilot1-001/trial_1 | pilot-v1/nominal_sfp/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.1 | mapped | compensated |
| nominal_sfp_r1 | A | pilot1-004/trial_5 | pilot-v1/nominal_sfp/A/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.53 | mapped | compensated |
| reversed_sfp_0_r0 | A | - | - | - | - | - | - | - | - | - | - | - | infrastructure (2 runs) |
| tilt_sc_0_r0 | A | pilot1-004/trial_2 | pilot-v1/tilt_sc_0/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_0_r0 | B | pilot1-009/trial_3 | pilot-v1/tilt_sc_0/B/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_0_r1 | A | pilot1-007/trial_6 | pilot-v1/tilt_sc_0/A/1/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_1_r0 | A | pilot1-004/trial_3 | pilot-v1/tilt_sc_1/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_1_r0 | B | pilot1-009/trial_4 | pilot-v1/tilt_sc_1/B/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sc_1_r1 | A | pilot1-008/trial_1 | pilot-v1/tilt_sc_1/A/1/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_0_r0 | A | pilot1-004/trial_1 | pilot-v1/tilt_sfp_0/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_0_r0 | B | pilot1-009/trial_2 | pilot-v1/tilt_sfp_0/B/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| tilt_sfp_0_r1 | A | pilot1-007/trial_5 | pilot-v1/tilt_sfp_0/A/1/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| trans_edge_sc_0_r0 | A | pilot1-003/trial_2 | pilot-v1/trans_edge_sc_0/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.07 | mapped | compensated |
| trans_edge_sc_0_r1 | A | pilot1-006/trial_6 | pilot-v1/trans_edge_sc_0/A/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.79 | mapped | compensated |
| trans_edge_sfp_0_r0 | A | pilot1-003/trial_1 | pilot-v1/trans_edge_sfp_0/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.76 | mapped | compensated |
| trans_edge_sfp_0_r1 | A | pilot1-006/trial_5 | pilot-v1/trans_edge_sfp_0/A/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 92.97 | mapped | compensated |
| yaw_edge_sfp_0_r0 | A | pilot1-002/trial_5 | pilot-v1/yaw_edge_sfp_0/A/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.4 | mapped | compensated |
| yaw_edge_sfp_0_r1 | A | pilot1-006/trial_3 | pilot-v1/yaw_edge_sfp_0/A/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.52 | mapped | compensated |
| yaw_edge_sfp_1_r0 | A | pilot1-002/trial_6 | pilot-v1/yaw_edge_sfp_1/A/0/a1 | failed | none | yes | 0.000 | 0.0 | 0 | 0.0 | 1.0 | - | natural_failure_known |
| yaw_edge_sfp_1_r0 | B | pilot1-010/trial_5 | pilot-v1/yaw_edge_sfp_1/B/0/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 89.23 | mapped | recovered |
| yaw_edge_sfp_1_r1 | A | pilot1-006/trial_4 | pilot-v1/yaw_edge_sfp_1/A/1/a1 | success | target | yes | 0.000 | 0.0 | 0 | 0.0 | 93.07 | mapped | compensated |

## A/B

| family | setting | trials | inserted | Wilson 95% | runtime success | false success | recovered | unsafe | with contacts | mean s | infrastructure |
|---|---|---|---|---|---|---|---|---|---|---|---|
| beyond_sfp | A | 6 | 5 | [0.44, 0.97] | 5 | 0 | 0 | 0 | 0 | 31.3 | 0 |
| beyond_sfp | B | 1 | 1 | [0.21, 1.00] | 1 | 0 | 0 | 0 | 0 | 32.9 | 0 |
| empty_rail_sfp | A | 2 | 0 | [0.00, 0.66] | 0 | 0 | 0 | 0 | 0 | 10.0 | 0 |
| inrange_sc | A | 8 | 3 | [0.14, 0.69] | 3 | 0 | 0 | 0 | 0 | 24.7 | 0 |
| inrange_sc | B | 4 | 4 | [0.51, 1.00] | 4 | 0 | 1 | 0 | 0 | 32.6 | 0 |
| inrange_sfp | A | 8 | 6 | [0.41, 0.93] | 6 | 0 | 0 | 0 | 0 | 31.0 | 0 |
| inrange_sfp | B | 2 | 2 | [0.34, 1.00] | 2 | 0 | 0 | 0 | 0 | 32.5 | 0 |
| nominal_sc | A | 2 | 1 | [0.09, 0.91] | 1 | 0 | 0 | 0 | 0 | 29.9 | 0 |
| nominal_sc | B | 1 | 1 | [0.21, 1.00] | 1 | 0 | 0 | 0 | 0 | 29.9 | 0 |
| nominal_sfp | A | 2 | 2 | [0.34, 1.00] | 2 | 0 | 0 | 0 | 0 | 31.1 | 0 |
| reversed_sfp | A | 0 | 0 | - | 0 | 0 | 0 | 0 | 0 | - | 1 |
| tilt_sc | A | 4 | 0 | [0.00, 0.49] | 0 | 0 | 0 | 0 | 0 | 26.8 | 0 |
| tilt_sc | B | 2 | 0 | [0.00, 0.66] | 0 | 0 | 0 | 0 | 0 | 36.3 | 0 |
| tilt_sfp | A | 2 | 0 | [0.00, 0.66] | 0 | 0 | 0 | 0 | 0 | 26.4 | 0 |
| tilt_sfp | B | 1 | 0 | [0.00, 0.79] | 0 | 0 | 0 | 0 | 0 | 58.9 | 0 |
| trans_edge_sc | A | 2 | 2 | [0.34, 1.00] | 2 | 0 | 0 | 0 | 0 | 31.0 | 0 |
| trans_edge_sfp | A | 2 | 2 | [0.34, 1.00] | 2 | 0 | 0 | 0 | 0 | 32.8 | 0 |
| yaw_edge_sfp | A | 4 | 3 | [0.30, 0.95] | 3 | 0 | 0 | 0 | 0 | 30.2 | 0 |
| yaw_edge_sfp | B | 1 | 1 | [0.21, 1.00] | 1 | 0 | 1 | 0 | 0 | 47.3 | 0 |
| all | A | 42 | 24 | [0.42, 0.71] | 24 | 0 | 0 | 0 | 0 | 28.2 | 1 |
| all | B | 12 | 9 | [0.47, 0.91] | 9 | 0 | 2 | 0 | 0 | 36.4 | 0 |

claim: none; ADR 030 makes it on the held-out manifest only (descriptive table)

## Confusion

| setting | verdict class | verdicts | false alarm | tie | late | recovered | real failure | unconfirmed |
|---|---|---|---|---|---|---|---|---|
| A | FORCE_LIMIT | 1 | 0 | 0 | 0 | 0 | 1 | 1 |
| A | INSERT_STUCK | 14 | 0 | 0 | 0 | 0 | 14 | 14 |
| A | UNKNOWN | 1 | 0 | 0 | 0 | 0 | 1 | 1 |
| B | INSERT_STUCK | 11 | 0 | 0 | 0 | 2 | 9 | 3 |

- false alarms: 0; without the 0 trials judged as a whole: 0
- judged as a whole (no time mapping): none
- unconfirmed: a real failure the run stopped at, so the false-alarm count is a lower bound (ADR 029)

## Calibration

| port type | trials | samples | period ms | not fresh | dropped | free-space N | at rest N, approach | at rest N, align | INSERT_STUCK | FORCE_LIMIT |
|---|---|---|---|---|---|---|---|---|---|---|
| sc | 25 | 14223 | mean 50.4, sd 1.4, p95 52.0, max 102.0 | 0 | 0 | mean 1.12, sd 1.50, p95 1.81, max 28.37 | undefined | undefined | 16 verdicts, 0 repeats, 0 untimed; rise mean 0.46 s, max 0.50 s | 1 verdicts, 0 repeats, 0 untimed; rise mean 0.55 s, max 0.55 s; over the limit at it mean 0.50 s, max 0.50 s |
| sfp | 31 | 18722 | mean 50.3, sd 2.2, p95 52.0, max 282.0 | 0 | 0 | mean 0.95, sd 0.46, p95 1.59, max 14.05 | mean 0.58, sd 0.20, p95 0.97, max 0.97 | mean 0.69, sd 0.29, p95 1.34, max 1.34 | 10 verdicts, 0 repeats, 0 untimed; rise mean 0.50 s, max 0.50 s | none |
| all | 56 | 32945 | mean 50.3, sd 1.9, p95 52.0, max 282.0 | 0 | 0 | mean 1.03, sd 1.07, p95 1.71, max 28.37 | mean 0.58, sd 0.20, p95 0.97, max 0.97 | mean 0.84, sd 0.48, p95 2.13, max 2.13 | 26 verdicts, 0 repeats, 0 untimed; rise mean 0.48 s, max 0.50 s | 1 verdicts, 0 repeats, 0 untimed; rise mean 0.55 s, max 0.55 s; over the limit at it mean 0.50 s, max 0.50 s |

- a distribution is mean, sd, nearest-rank p95 and max; undefined under 10 values
- free space: every fresh sample of approach; at rest: a fresh sample after the plug moved under 0.05 mm in each of 5 steps, over 2.0 mm above the port's entrance
- rise: INSERT_STUCK from the first of the consecutive fresh samples above 10 N within the skill; FORCE_LIMIT from the start of the task's first step that added to the Monitor's sum over 20 N, shown with the verdict's sum (over the limit) against the Monitor's trigger; only the first verdict of a rise, or of the task for FORCE_LIMIT, is timed
- measured only: a threshold changes under ADR 030's D5, with ADR 032
