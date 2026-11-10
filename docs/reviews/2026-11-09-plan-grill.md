# Plan grill after the first sim run (#28), 2026-11-09

After #28's first sim run ([report](../phase1-run28-report.md)), Claude proposed re-planning
Phases 2–4 around the agentic side of the Agentic Robotics Engineer JD. Shaun asked Codex to
grill the plan, read-only. This file keeps the plan as it was grilled, Codex's answer verbatim,
and the steps taken from it, which are worked through one at a time.

## The plan as grilled

1. Merge PR #42 as is; put the re-prioritization in a follow-up PR.
2. Phase 1: fix seated detection deterministically, preferably from proprioception (TCP pose and
   a force plateau) rather than ground-truth height.
3. Phase 2 slimmed, not cut: only scene faults through the engine config (port yaw, grasp
   offset, empty rail), small scale; ACT optional; no large noise floor, no four channels.
4. Phase 3 expanded: real UNKNOWNs from slim Phase 2 as the main case; a "VLM verify against
   ground truth" experiment; tracing and agent eval; no LangGraph, with an ADR saying why.
5. Ground truth stays as the referee and as the skills' geometry source; the runtime's own
   success judgement should not read it; an LLM or VLM may give a slow second opinion,
   measured against ground truth. (Prompted by Shaun's question: "remove ground truth and let
   the LLM judge?")

## Steps

From Codex's ranked changes (# is Codex's rank). Shaun's interview is within a week, and the
agentic side is what the project must show first, so the steps run in the order below (plan
approved 2026-11-09). Between the LLM path and Phase 1's gaps comes the README and an interview
one-pager, which is not a grill step. Status is updated as each step is done.

| Order | # | Step | Status |
|---|---|---|---|
| 1 | 5 | Revise PR #42 now (a "run-28 findings → revised scope" commit) instead of merging it as is; keep the history, remove stale exit criteria; separate what replay does today from the fork that does not exist yet | done: `c2f06ed` on #42 |
| 2 | 2 | Define ground-truth permissions and eval semantics: referee labels, oracle geometry, measured observations; state the Monitor's ground-truth dependence; fix how scene faults count in the confusion matrix | definitions done: ADR 015. The counting is built with the Phase 2 runner; mock metrics and the baseline are unchanged |
| 3 | 4 | One complete LLM path first: confirmed HOLD → async call → schema/state validation → bounded action or escalation → ground-truth result; a simple-policy baseline and a full trace. VLM and promotion after it | done: on the mock world (#44–#52) and in the sim with a live world (#54–#61), both in PR #64, with fixed and rule policies as the baselines and full traces (`docs/phase3-llm-results.md`, `docs/sim-results.md`). Promotion (#62) and task-to-tree (#63) came after it; the VLM check has not been started |
| 4 | 1 | Close Phase 1's real gaps: tell seated from jammed, fix the align/insert boundary, the SC jam, act on `hold_unconfirmed`; new 3/3 evidence with the runtime's return and the scorer's insertion | in part: seated detection from the oracle height in `insert_scripted` and `verify` (#58); sim runs since then: #61 (A/B/C under a command dropout) and a rerun after a review fix (`docs/sim-results.md`). The align/insert boundary, the SC jam and `hold_unconfirmed` are deferred and stated as known limits |
| — | 3 | A small, cost-capped fault discovery before committing to Phase 3: do real UNKNOWNs exist, with enough evidence and actionable options; an exit if none do | deferred to Phase 2 (its brief was revised in #42). Until then Phase 3's UNKNOWNs are mock scenarios we designed, and its results say so |

## Codex's answer (verbatim)

Links to local files were repointed to their repo paths; the JD is not in the repo. Line numbers
refer to `main` @ `85c2216`.

```text
[GT TF] ──port／plug／TCP pose──> [Skills]
    └──height／XY offset──> [Signals] ──進度／偏差──> [Monitor]
[Wrench] ──tared force／torque──> [Signals]
[Monitor] ──failure verdict──> [BT／Planner] ──skill 選擇──> [Skills]

[Scorer insertion_event] ──獨立結果標籤──> [離線 Eval]
[Runtime events] ──系統自身判斷──> [離線 Eval]
```

**方向大致成立，但目前有三個會被面試官打穿的缺口：GT 邊界與現況不符、縮減 Phase 2 可能抽掉 Phase 3 的材料、replay 被賦予尚未實作的能力。**

下文使用三個來源縮寫，行號均指實際讀到的檔案：

- **D**：PR #42 diff (`docs/phase-2-4-briefs` @ `6a2aa42`)。
- **R**：[run-28 report](../phase1-run28-report.md)。我核對了報告，未獨立重算 bags／scorer。
- **J**：Agentic Robotics Engineer JD (not in the repo)。

**1. PR42 原樣合併，再用 follow-up 展示資料驅動重排**

- **最強反對：**你正在為 git history 的敘事，接受已知失準的工作規格。D:16–20 寫「Phase 1 proved … no faults」，但 R:105–114 明確列出 Phase 1 尚未通過；D:126–134 又把大規模 noise floor、ACT 設為 exit criteria。這些不是單純保留歷史，而是合併後仍會被引用的現行要求。
- **失敗情境：**下一個 session 按已合併 brief 開工，先做四種 fault channel／ACT；follow-up 尚未完成，有限的面試準備時間已花在你決定刪除的工作上。
- **是否存活：****不支持「原樣」合併；支持保留重排歷史。**
- **我會改：**在 PR42 追加一個「run-28 findings → revised scope」commit，修正事實與 exit criteria；原始設計仍留在 commit history。若一定拆 PR，至少先標明哪些段落已被 run-28 推翻，並讓替代規格同時可見。**資料驅動的證據是決策與量測之間的因果，不是兩張 PR 的先後。**

**2. Phase 1 用 deterministic seated detection，優先 proprioception**

- **最強反對：**「TCP pose＋force plateau」還沒有證明能辨識 seating。R:48–56 已顯示 seated 與 entrance jam 都可能是約 12–13 N、低進度、小 XY error。加入 TCP pose 能否區分，取決於 port reference、TCP-to-plug transform、grasp variation；其中任何一個若來自每步 GT，就仍是 privileged observation。
- **失敗情境〔假設〕：**名義 grasp 下校正出的 TCP seating threshold，在 Phase 2 改變 grasp offset 後，把入口 jam 判成 seated；runtime 成功率上升，scorer insertion 卻沒有上升。
- **是否存活：****deterministic 原則存活；proprioception 優先應是有期限的實驗，不應成為無限延長 Phase 1 的新專案。**
- **我會改：**
  1. 修正整個 skill boundary：`align` 已可能完成 insertion，不能只改最後的 `verify` threshold。現況 `align` 用 `motion` profile，直到 `insert_scripted` 才啟用 insertion rules，與實際接觸階段不一致。[skills.py:142](../../adapters/aic/skills.py#L142)
  2. 先用 seated、入口 jam、未接觸三類片段，檢查候選訊號是否可分；加上 grasp variation 作保留驗證，不能只重現 run-28。
  3. 優先評估已提供的 `controller_state.tcp_pose`，再決定是否需要自己從 joint states 實作 FK。adapter 已接入 measured TCP pose。[node.py:27](../../adapters/aic/node.py#L27)、[ros_port.py:132](../../adapters/aic/ros_port.py#L132)
  4. seated 成立時停止多餘下壓；仍保留 safety verdict 優先權。現有 executor 已讓 `FORCE_LIMIT`／`SENSOR_DROPOUT` 覆蓋 skill success。[behaviours.py:139](../../cellrt/executor/behaviours.py#L139)

此外，**修 seated detection 不等於完成 Phase 1**：SC 的真 jam 仍要處理，3/3 exit criterion 仍有效。[phase-1 brief:84](../../docs/briefs/phase-1-aic-adapter.md#L84)

**3. Phase 2 只保留小規模 scene faults，ACT optional**

- **最強反對：**這可能讓 Phase 3 沒有可研究的 UNKNOWN。`gripper_target()` 每步用 GT plug／port orientation 做校正，也讀實際 tip offset；yaw 與 grasp offset 有可能被 skills 補償。[geometry.py:103](../../adapters/aic/geometry.py#L103) D:145–146 本來就承認此風險。另一方面，empty rail 若造成目標 TF 不存在，可能直接終止為 `ground_truth_unavailable`，根本到不了 UNKNOWN triage。[world.py:218](../../adapters/aic/world.py#L218)
- **失敗情境〔假設〕：**yaw／offset 全被補償，empty rail 全是 readiness abort；最後只剩 run-28 那個「其實已成功」的 UNKNOWN，卻拿它展示 LLM recovery。
- **是否存活：****有條件存活。縮 scope 正確，但必須先驗證材料是否存在。**
- **我會改：**先做有成本上限的 discovery pilot，記錄每個擾動是「被補償、已知失敗、UNKNOWN、adapter abort」哪一種。若沒有 UNKNOWN，接受負結果，再明確決定是否補一種 fault channel；不要把已知類別故意改叫 UNKNOWN。

大 noise-floor study 可以刪，**最小 clean repeats、固定 tuning／held-out split、各 setting 交錯執行仍應保留**，否則不知道改善來自 recovery 還是 sim variation。D:90–104 已有這些必要設計。

另外，scene faults 是人為挑選的情境；比較準確的說法是「失敗軌跡由物理模擬產生」，不是「沒有設計過的失敗」。

**4. Phase 3 擴充 UNKNOWN、VLM verification、tracing／eval；不加 LangGraph**

- **最強反對：**你同時開了 triage、task-to-tree、escalation、rule promotion、VLM 五條產品線，卻尚未證明 LLM 有一個比簡單 policy 更有價值的決策。原 brief 已承認 mock 的 C−B gap 是預先設計的 scenario 效果。[phase-3 brief:9](../../docs/briefs/phase-3-unknown-promotion.md#L9)
- **失敗情境〔假設〕：**ACT 被省略，現有 decision schema 剩下實用的 `retry`／`abort`；LLM 多花數秒重述同一規則。VLM 只看走到 verify 的成功樣本而 accuracy 很高；promotion 又只在提案使用過的 logs 上通過。
- **是否存活：****tracing／agent eval 存活；五項同時作為必要交付不存活。**
- **我會改：**
  - 主線只選一個：**真實 failure → 有界決策 → 驗證 → 執行／拒絕 → GT 結果**。先說清楚可選 recovery，以及為何需要推理。現有輸出只有 `SwitchSkill | Retry | Abort`。[schemas.py:42](../../cellrt/planner/schemas.py#L42)
  - 對照至少包括同預算的固定 retry／abort policy 與 `RulePlanner`；量測成功、錯誤動作、latency、tokens、cost、invalid output、timeout、重複穩定性。
  - VLM 先做離線 optional experiment。必須收集失敗終點與提早中止的影像，不能只收 verify；報 false-success、false-failure、abstention，而非只有 accuracy。並先確認 insertion event 的時間語意：是曾經插入，還是拍照當下仍 seated。
  - Promotion 的 replay 只證明**分類規則對既有輸入的行為**；不能證明新 recovery 會成功。規則要有未參與提案的保留 logs；recovery 要重新跑 sim。[phase-3 brief:37](../../docs/briefs/phase-3-unknown-promotion.md#L37)

不加 LangGraph／LangChain 合理，但 ADR 應解釋這個 cell 的需求與維護成本，避免宣稱 event sourcing 本身證明框架較差。**現有 `state_at()` 明寫是 future fork 的基礎；`replay_lines()` 是事件讀取。**目前不能宣稱已支援 Gazebo 的 counterfactual fork。[state.py:112](../../cellrt/runtime/state.py#L112)、[replay.py:30](../../cellrt/replay.py#L30)

**5. GT 保留 referee／geometry，runtime 自行判斷，VLM 作 second opinion**

- **最強反對：**這句話混用了兩種 GT。「不讀答案標籤」與「不讀完美幾何」完全不同。目前程式符合部分前者，明顯不符合後者。
- **失敗情境：**面試展示宣稱「runtime 不讀 GT」，面試官沿 `Signals.height_mm` 往回追，發現它直接來自 GT plug／port TF；接著問 proprioceptive verification 是否仍使用同一個 GT plug offset，整個獨立判斷的論述失去可信度。
- **是否存活：****保留 GT、不讓 LLM 取代 referee 的答案正確；「runtime 判斷不得讀 GT」必須改成可驗證、與實作一致的規格。**
- **我會改：**明確區分以下權限。

| 資料 | Online 使用政策 | 結果的意義 |
|---|---|---|
| scorer insertion／fault labels | 不給 skill、Monitor、planner 作決策 | 獨立評估答案 |
| GT geometry 作 perception substitute | 可在明確標示的 oracle mode 使用 | 結果以完美幾何為條件 |
| measured TCP／wrench／images | 給 runtime observer／verifier | 仍須說明 calibration 與 estimator 假設 |
| VLM judgement | 慢路徑、有 timeout、可 abstain | 待評估的預測，不是答案 |

**實際 GT 路徑核對結果：**

- `AicWorld` 從 TF 取得 port、plug、gripper，再用它們填 `Signals.height_mm`／`xy_offset_mm`。[world.py:218](../../adapters/aic/world.py#L218)
- `stuck_under_force` 讀 XY error 與 height progress；`POSE_ERROR` 讀 XY；`insert_stalled` 的 progress 也是前後 height 相減。因此 **Monitor 間接讀 GT geometry**。[classifier.py:170](../../cellrt/monitor/classifier.py#L170)、[classifier.py:223](../../cellrt/monitor/classifier.py#L223)
- `AicVerify` 直接讀 geometry，以 `max(0, -height)` 與 10 mm 比較，直接決定成功失敗。[skills.py:194](../../adapters/aic/skills.py#L194)
- `plug_to_port_mm()` 實際做的是 **base-frame 座標差**，並假設 insertion axis 為 base −z；不是一般化的 port-frame 投影。[convert.py:132](../../adapters/aic/convert.py#L132)

所以，**skills 用 GT geometry 與「不讀 referee labels」一致；與嚴格「成功／失敗判斷完全不依賴 GT geometry」並不一致，現況 Monitor 和 verify 都違反後者。**只把 verify 改成 proprioception，仍不能把整個 runtime 稱為 GT-free。

也不必把「系統不能自己評分」說成禁止 runtime 自行 verify：runtime 本來就需要判斷完成。應禁止的是拿自身判斷當獨立效能證據。移除所有 GT 是更換 perception／observation stack 的另一項工程；repo 已明訂 GT TF 代替 perception，且 submission 會阻擋 GT。[roadmap.md:45](../../docs/roadmap.md#L45)、[CLAUDE.md:153](../../CLAUDE.md#L153)

**對這份 JD，計畫還缺的是可展示的 agent infrastructure 行為。**

J:17–25、43–53、79 很對應這個專案；目前最有價值的補強是：

- **真正的非同步 model wait。** 現有 `drain()` 要求 live tasks 都在等待模擬 clock，100 次 yield 後仍未滿足就丟 `DrainError`；planner protocol 也明說目前不能等待外部工作。真 API call 不能直接替換 stub。應展示等待期間 cancel、task deadline、訊號採樣持續有效，late response 被丟棄。[runner.py:83](../../cellrt/executor/runner.py#L83)、[planner/base.py:43](../../cellrt/planner/base.py#L43)
- **具語意的 skill contract。** AIC skills 的 preconditions 目前直接回 `True`；planner catalogue 只有名稱與 Monitor profile。schema 正確不足以證明可以從某個接觸狀態安全切換技能。[skills.py:119](../../adapters/aic/skills.py#L119)、[behaviours.py:287](../../cellrt/executor/behaviours.py#L287)
- **可重建的 model trace。** 現有 events 保存 context、validated decision；沒有完整 rendered prompt、成功 raw response、model identity、token usage。D:166–169 是待完成要求，不能當成既有能力。[events.py:156](../../cellrt/runtime/events.py#L156)
- **故障標籤與效應的區別。** 現有 metrics 將沒有 injected fault 的 verdict 當 false alarm；因此 clean run 中的真 SC jam 也可能被錯算。scene yaw 被補償也不應自動算 detection miss。這個分類評估定義必須在搬到 sim 前修正。[metrics.py:13](../../cellrt/eval/metrics.py#L13)

還有一項應提前處理：R:90–92 的 `hold_unconfirmed`。現在 `_confirm_hold()` 的結果被記錄，policy return 仍只看原本 `outcome`；**它尚未成為成功或繼續執行的必要條件**。[run.py:172](../../adapters/aic/run.py#L172) 這比再加一個 agent 功能，更能證明 J:25 要求的可靠性工程。

**過度擴張的是：**四種 fault channel、大型 noise-floor campaign、ACT 強制整合、完整 GT-free perception、Gazebo 任意步驟 fork，以及五條 Phase 3 主線一起交付。VLM 是 J:71 的加分項；LangGraph 等是 J:61 的加分項。它們不應排在一條有對照、有 timeout、有 trace、有實際結果的 agent recovery 流程前面。

報告與 demo 也不該全等到 Phase 4。D:208–209 甚至允許沒有 Phase 3 就完成交付；對這份 JD，至少應要求一段真 model integration 的可查證證據，允許結果顯示「沒有優於簡單 policy」。

全程唯讀，未修改檔案、未執行測試或 sim。前後 `git status --short` 均只有原已存在的 `M docs/PHASE0_KICKOFF.md`。

**建議變更，依優先順序：**

1. **先關閉 Phase 1 的實際缺口：**seated／jam 區分、align／insert 邊界、SC jam、`hold_unconfirmed` 的處置；重新取得含 runtime return 與 scorer insertion 的 3/3 證據。
2. **立即定義 GT 權限與評估語意：**區分 referee labels、oracle geometry、measured observations；揭露 Monitor 的 GT 依賴，修正 scene-fault confusion matrix。
3. **先做小型 fault discovery，再承諾 Phase 3：**確認 UNKNOWN 真存在、有足夠證據、且有可執行的決策選項；設定成本上限與沒有 UNKNOWN 時的退出方案。
4. **集中做一條完整 LLM 流程：**confirmed HOLD → async call → schema／state validation → 有界動作或 escalation → GT 評估；加入簡單 policy 對照與完整 trace。VLM、promotion 排在其後。
5. **修訂 PR42，現在就整理面試證據：**保留 run-28 前後決策歷史，移除過時 exit criteria；清楚區分已完成的 replay、未完成的 fork，以及每個結果的限制。
