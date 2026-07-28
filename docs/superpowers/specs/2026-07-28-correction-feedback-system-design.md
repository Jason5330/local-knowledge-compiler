# Local Knowledge Compiler 錯誤回饋與自動修正系統設計

日期：2026-07-28  
狀態：使用者已逐節批准，等待書面規格總審  
目標版本：下一版 Local Knowledge Compiler

## 1. 問題與目標

大量、複雜 Excel 可能因為合併儲存格、多層表頭、公式、單位、工作表版本或檢索範圍
而產生錯誤回答。現有系統可以阻止未經使用者要求的回答自動寫回，但尚未把一次錯誤
轉成未來查詢可強制套用的修正紀錄。

本功能的目標是：

```text
發現錯誤
→ 根據本機原始證據建立修正
→ 對修正建立可解釋的適用條件
→ 未來 prepare 自動搜尋相似修正
→ AI 必須逐項處理修正
→ finalize 機械驗證後才能保存
```

核心要求：

1. 不依賴 AI 自己記得讀取修正。
2. Codex、Claude Code 與其他遵守協議的 AI 得到相同修正資料。
3. 修正永遠不能取代或改寫原始 Excel。
4. 修正與原始證據衝突時，停止正式保存。
5. 所有修正、索引與日誌只存在本機 `KnowledgeBase/`。

## 2. 已批准的產品決策

- 採用「證據包強制法」，不採用提示詞提醒法或把修正混進 Wiki。
- 強匹配修正自動附在證據包，AI 必須說明本次如何處理。
- AI 可以自行建立修正並立即生效，不需要逐筆等待使用者批准。
- 自動建立必須由使用者明確指出答案錯誤，或由系統找到可驗證矛盾觸發。
- AI 不能只因為感覺可能有錯就建立修正。
- 修正是解讀警告與規則，權力低於原始 Excel 證據。
- 相似判斷使用本機結構化條件與搜尋，不依賴雲端語意模型。
- 新 Excel 版本進入後，自動重新驗證相關修正。
- 修正不直接刪除，使用狀態和時間線保存完整歷史。

權力順序固定為：

```text
原始 Excel 證據 ＞ 修正紀錄 ＞ AI 自己的推測
```

## 3. Vault 結構

新增本機目錄：

```text
KnowledgeBase/
├── 10_raw/
├── 20_wiki/
├── 30_answers/
├── 40_index/
├── 50_corrections/
│   ├── records/
│   └── timeline/
└── 80_system/
```

### 3.1 `50_corrections/records`

每筆修正一個受驗證、可讀的結構化紀錄。這是修正的正式資料來源。

### 3.2 `50_corrections/timeline`

保存修正建立、匹配、套用、拒絕、暫停、恢復、重新驗證與退役事件。時間線只追加，
不得覆蓋舊事件。

### 3.3 `40_index`

既有本機索引增加修正搜尋資料。索引是可重建衍生資料，不是修正的正式來源。

## 4. 修正資料模型

每筆正式修正至少包含：

```text
correction_id
schema_version
status
created_at
updated_at
trigger_type
created_by
original_question
wrong_answer_summary
error_type
correction_rule
applicability
exclusions
supporting_evidence
validated_versions
supersedes
superseded_by
content_sha256
```

### 4.1 觸發類型

只允許：

- `user_reported_wrong`：使用者明確表示回答錯誤。
- `deterministic_validation_failure`：可驗證的加總、單位、引用、範圍或版本矛盾。

### 4.2 錯誤類型

固定分類：

- `extraction_error`：合併儲存格、多層表頭、公式或其他讀取問題。
- `retrieval_error`：找錯檔案、工作表或版本。
- `unit_error`：元／萬元、公斤／噸等單位混用。
- `time_error`：混用月份、年度或生效日期。
- `range_error`：把小計當總計，或包含錯誤資料範圍。
- `reasoning_error`：證據正確但推論錯誤。
- `citation_error`：回答與引用位置不一致。

### 4.3 適用條件

`applicability` 使用固定欄位，不接受任意可執行提示：

```text
spaces
file_types
source_families
sheet_names
column_names
units
question_types
keywords
error_types
```

每筆修正必須至少具備一個資料結構錨點，例如來源系列、工作表或欄位；只有普通關鍵詞
不足以建立強匹配修正。

### 4.4 支持證據

每筆修正必須引用現有原始證據：

```text
source_id
version_id
locator
evidence_sha256
```

缺少可驗證原始證據時，修正建立必須失敗並回傳 `correction_rejected`。

## 5. 修正生命週期

狀態只允許：

- `active`：目前可參與強或中度匹配。
- `stale`：來源版本更新，尚未完成重新驗證。
- `suspended`：新證據與修正發生衝突，暫停套用。
- `retired`：已被取代或確定不再適用，保留歷史但不參與匹配。

AI 在合格觸發和證據完整時可建立 `active` 修正並立即生效。這不代表修正可以壓過
原始證據；只要兩者衝突，修正必須轉為 `suspended`，回答不得正式保存。

舊修正不直接刪除。新修正取代舊修正時：

```text
舊修正.status = retired
舊修正.superseded_by = 新修正 ID
新修正.supersedes = 舊修正 ID
```

## 6. 相似匹配

### 6.1 輸入特徵

`prepare` 從以下本機資料取得比對特徵：

- 問題詞語與問題類型。
- 已選 space。
- 搜尋到的來源檔案與來源系列。
- Excel 工作表、欄位、單位與日期。
- 修正錯誤類型與排除條件。

不同 space 是硬性邊界，禁止跨 space 匹配。

### 6.2 三級結果

#### 強匹配

同時滿足相同 space、資料結構錨點及問題類型等必要條件。放入
`applicable_corrections`，AI 必須處理。

#### 中度匹配

部分結構條件相同，例如新月份沿用同一表格。放入 `applicable_corrections`，但標示
`verification_required`；AI 必須先核對本次原始證據。

#### 弱匹配

只有普通關鍵詞或少數非結構條件相同。放入 `possible_corrections`，不得自動當成
正式限制，也不阻止回答。

只靠檔名、單一普通關鍵詞或 AI 語意感覺，不能形成強匹配。

### 6.3 可解釋結果

每項匹配都必須附：

```text
correction_id
match_level
matched_conditions
unmatched_conditions
reason
content_sha256
```

使用者可以看懂為何套用，例如：

```text
同為月報、工作表「年度總表」、欄位「金額」、單位「萬元」。
```

## 7. 查詢證據包

`kb prepare` 在現有 packet 增加：

```text
applicable_corrections
possible_corrections
correction_scan
correction_warnings
```

`correction_scan` 至少提供：

```text
total_considered
applicable_count
possible_count
truncated
index_available
```

若強制修正搜尋遭截斷、索引損壞或正式紀錄無法驗證，packet 必須降低狀態，禁止產生
可正式保存的高信心答案。原始證據查詢仍可繼續，但必須顯示
`correction_unavailable`。

修正的支持證據必須保持原始引用。修正本身不能冒充 raw evidence。

## 8. AI 回答契約

答案 JSON 增加 `correction_decisions`。每個強匹配和中度匹配修正都必須剛好出現一次：

```json
{
  "correction_id": "COR-2026-001",
  "decision": "applied",
  "reason": "本次工作表仍以萬元表示，且原始證據確認相同欄位。",
  "content_sha256": "..."
}
```

`decision` 只允許：

- `applied`
- `not_applicable`
- `conflict`

`not_applicable` 必須說明本次原始證據中哪個條件不符。`conflict` 必須阻止正式保存，
直到相關修正完成重新驗證。

每次使用者可見回答固定增加「本次修正紀錄」：

```text
套用哪些修正
哪些修正不適用及原因
是否存在衝突
是否允許保存
```

## 9. Finalize 強制驗證

`finalize` 必須檢查：

1. packet 中每筆 `applicable_corrections` 都有唯一 decision。
2. 回答沒有增加 packet 不存在的修正 ID。
3. ID、版本與 `content_sha256` 完全一致。
4. `applied` 修正仍有可驗證的原始支持證據。
5. `not_applicable` 有非空理由及可核對條件。
6. 沒有 `conflict`。
7. 沒有使用 `stale`、`suspended` 或 `retired` 修正。
8. 修正沒有取代重要結論所需的原始引用。
9. 修正掃描沒有未處理的截斷或損壞。

任何一項失敗都拒絕保存、拒絕排入 Wiki 更新，並回傳可操作錯誤。

系統無法阻止不守協議的 AI 在聊天視窗生成文字，但可以把該文字視為未驗證回答，
拒絕寫入 `30_answers` 和 `20_wiki`。

## 10. 修正建立流程

使用者自然語言：

```text
這個回答錯了。
正確情況是：【可選】
請核對原始 Excel、建立修正，再重新回答。
```

代理內部流程：

```text
讀取上次問題、packet 與回答
→ 重新核對原始證據
→ 判定是否有合格觸發
→ 產生受限制的修正提案
→ 驗證原始引用
→ 驗證適用與排除條件
→ 去重
→ 原子寫入正式修正
→ 更新修正索引
→ 追加時間線
→ 重新 prepare
```

若證據不足或觸發不合格，不建立修正，回傳 `correction_rejected` 和具體資料缺口。

實作時應提供代理內部 CLI，但使用者文件只呈現自然語言。CLI 名稱與精確參數在實作
計畫中確定，不能改變上述資料和驗證契約。

## 11. 去重與取代

去重使用經正規化的：

```text
space
error_type
correction_rule
applicability
exclusions
```

相同錯誤再次發生時，不建立重複修正；在原修正時間線追加 occurrence，並記錄新的問題
和來源版本。

規則實質不同時才建立新修正。若新規則取代舊規則，必須建立雙向取代關係並退役舊修正。

## 12. 新來源版本與重新驗證

匯入新 Excel 版本後，系統依來源系列、工作表、欄位、單位及日期找出可能受影響修正。

結果：

- 結構和規則不變且新證據支持：保持或恢復 `active`。
- 結構改變、證據不足：轉為 `stale`。
- 新證據反駁：轉為 `suspended`。
- 已被新規則完整取代：轉為 `retired`。

自動重新驗證必須引用新版本原始證據。狀態變更與理由追加到時間線。

重新驗證尚未完成時，相關修正不得作為強制規則生效。

## 13. 使用者控制

使用者可用自然語言要求：

- 列出最近建立的錯誤修正。
- 顯示某修正曾影響的回答。
- 暫停或恢復指定修正。
- 退役指定修正但保留歷史。
- 檢查互相衝突、過期或等待驗證的修正。

每次狀態變更都記錄操作者、原因、時間與前後狀態。使用者明確暫停或退役的決定優先於
AI 的自動恢復；AI 不得在沒有新指示或新證據時偷偷撤銷使用者決定。

## 14. 安全與失敗模式

修正是受限制資料，不是可執行提示。禁止：

- 內嵌要求 AI 執行命令、搜尋網路或外洩資料的內容。
- 跨 space 讀取或套用。
- 修改 `10_raw`。
- 跳過引用和 finalize 檢查。
- 直接刪除歷史。
- 用修正內容取代原始證據。

所有路徑、檔案大小、紀錄數、欄位長度、packet 大小和每次帶入修正數都必須有硬上限。

損壞、無法驗證或超出預算時採 fail-closed：

```text
停止套用問題修正
→ 顯示 correction_unavailable
→ 降低回答信心
→ 禁止正式保存
```

`KnowledgeBase/` 和 `50_corrections/` 仍受既有 `/KnowledgeBase/` Git ignore、
pre-commit 與 pre-push 保護，不得進入公開 Repo。

## 15. 代理協議

更新公開 Repo 根目錄 `AGENTS.md`、`CLAUDE.md` 與 Vault 的
`80_system/KNOWLEDGE_PROTOCOL.md`：

```text
知識問題先 prepare
逐項處理 applicable_corrections
回答顯示 correction_decisions
未通過 finalize 不得宣稱已保存或已更新 Wiki
```

代理規則是第一層提醒；packet 自動附帶是第二層；finalize 驗證是最後機械保證。

## 16. 測試要求

至少涵蓋：

1. 強、中、弱匹配。
2. 不同 space 完全隔離。
3. 單一關鍵詞或檔名不能形成強匹配。
4. `stale`、`suspended`、`retired` 不會生效。
5. 缺少支持證據時修正建立被拒。
6. 使用者錯誤回饋可建立立即生效修正。
7. 可驗證矛盾可建立修正，主觀懷疑不可建立。
8. 漏處理、重複處理或偽造修正時 finalize 拒絕。
9. `not_applicable` 缺少理由時 finalize 拒絕。
10. 衝突阻止答案保存與 Wiki 更新。
11. 新 Excel 版本觸發 active／stale／suspended／retired 狀態轉換。
12. 重複錯誤合併 occurrence。
13. 取代關係雙向一致。
14. 損壞紀錄、索引或截斷採 fail-closed。
15. 大量修正受到數量和大小限制。
16. Codex 與 Claude Code 得到相同 packet 契約。
17. 舊 Vault 沒有 `50_corrections` 時可安全升級。
18. 完整端到端：錯誤回答 → 建立修正 → 相似問題自動匹配 → 合規保存。
19. `KnowledgeBase/` 和修正內容不出現在 Git tracked status。

## 17. 完成標準

- AI 不靠對話記憶也能收到適用修正。
- 每項匹配都有可解釋原因。
- 每筆 active 修正都有可驗證原始證據。
- 錯誤修正不能壓過 Excel。
- 未處理強制修正的回答不能保存。
- Excel 更新後舊修正不會盲目永久生效。
- 使用者能查看、暫停、恢復和退役修正。
- 換 Codex、Claude Code 或電腦後仍遵守相同本機規則。
- 功能不降低既有原始資料、引用、space 隔離、Git 隱私與離線搜尋保證。

## 18. 非目標

本階段不包含：

- 使用雲端 embedding 或外部向量資料庫。
- 讓修正直接覆寫 Excel、raw 或 Wiki 事實。
- AI 無證據自行發明永久規則。
- 自動刪除舊修正。
- 跨使用者、跨 Vault 或跨 space 分享修正。
- 未經現有安全匯入流程直接處理使用者唯一原始 Excel。
