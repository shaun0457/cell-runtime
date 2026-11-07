# Phase 0 開工 prompt

在新 repo 的根目錄放好 CLAUDE.md 之後，把下面整段貼給 Claude Code。

---

請先讀 CLAUDE.md。我們要做 Phase 0：一個完全不碰 ROS 的 runtime 核心，加上 mock world 和 eval CLI。

**先進 plan mode，不要寫程式。** 請給我：

1. 模組切分，以及每個模組的公開介面（函式簽名就好）
2. Event 的種類和欄位，以及 state 怎麼從 event 推導出來
3. py_trees 和 async skill 之間的橋接怎麼做（tick、RUNNING、cancel）
4. MockWorld 怎麼模擬插線，以及每種 fault 怎麼注入
5. 你認為需要我做決定的地方，每一個都列出選項和取捨。對應的 `docs/decisions/` 檔案只寫到選項為止，決定由我自己寫

等我確認後，再照下面的順序實作。每一步都要有測試，做完一步就停下來讓我看。

## 實作順序

**1. Runtime：events 和 store**
- append-only 的 SQLite event store：`run_id`、`seq`、`ts`、`type`、`payload`（JSON）
- `state = fold(events)`，必須是純函式
- 測試：同一串 events 重播兩次，得到的 state 完全一樣

**2. Skill 契約和 mock skills**
- 照 CLAUDE.md 的 Skill contract 實作 `approach`、`align`、`insert_scripted`、`verify`
- MockWorld 用簡化模型：plug 和 port 之間有 XY 誤差、插入深度、受力。不需要物理正確，只要每種失敗都能發生，而且可以重現
- 測試：timeout 和 cancel 都會留下正確的 event

**3. Monitor**
- 從 world 訊號分類失敗，類別照 CLAUDE.md 的 failure classes
- `FORCE_LIMIT` 的判斷規則是受力 > 20 N 持續 > 1 s，跟 AIC 的扣分規則一致
- 測試：每種注入的 fault 都要被分到正確的類別

**4. Executor**
- 做出三棵樹，分別對應設定 A、B、C
- C 的 planner 先用 `RulePlanner`，遇到 `UNKNOWN` 就從固定規則選：換技能、重來一次、或放棄並回報
- Planner 要是一個 Protocol，之後可以換成 LLM 版本，不用改 executor

**5. Eval**
- 情境寫在 `eval/scenarios/phase0.yaml`：每種 fault 一個情境，再加一個沒有 fault 的基準情境
- `cellrt eval --scenarios eval/scenarios/phase0.yaml --settings A,B,C --seeds 20` 要輸出一張表：每個設定的成功率、pass^k、恢復率、不安全事件數、平均步數、planner 呼叫次數，以及 monitor 的分類正確率
- `cellrt replay <run_id>` 能用 event log 把一次執行的每一步印出來

## 完成標準

- `pytest -q` 全部通過
- 上面那個 eval 指令一次跑完，三種設定的差異看得出來（A < B ≤ C）
- core 裡沒有任何 ROS import。請加一個測試或 lint 規則來保證這一點
- README 寫一段「怎麼跑」就好，其他內容等 Phase 4 再寫
