---
status: ready
updated: 2026-07-28
repository: Jason5330/local-knowledge-compiler
---

# Local Knowledge Compiler 完整交接

本文件讓另一台電腦上的 Codex、Claude Code 或其他 AI 直接接手。使用者是技術初學者；
請使用繁體中文、先給結果、自己執行安全操作，不要把一串終端機指令丟給使用者。

## 1. 使用者目標與原理

使用者無法在 OA 環境安裝 Obsidian，需要一個 AI 通用、本機優先、可持續迭代的知識庫：

```text
原始資料不變
→ 保存版本和可搜尋索引
→ 提問時只取相關證據
→ AI 根據證據回答並引用
→ 經使用者要求保存的回答再參與 Wiki 整理
```

思想來源包括 Karpathy 的 LLM Knowledge Base／LLM Wiki 與 Kepano 的 Obsidian skills。
採用的是「本機 Markdown、可重建整理層、證據式回答」原理，不依賴 Obsidian 軟體。

## 2. 公開 Repo 與私人 Vault

```text
公開 Repo：工具、測試、教學與代理入口
私人 Vault：目前專案根目錄下已初始化的 KnowledgeBase/
```

The authoritative Vault is the initialized KnowledgeBase/ under the active
project clone unless the user explicitly supplied another Vault.

`KnowledgeBase/` 是本機私人資料夾，永遠不會跟著上傳 GitHub。Repo 放在 C 槽，
Vault 跟著位於 C 槽專案；Repo 放在 D 槽，Vault 跟著位於 D 槽專案。

Git 保護：

```text
.gitignore 的 /KnowledgeBase/
→ pre-commit 檢查 staged paths
→ pre-push 檢查 tracked paths
→ scripts/check-local-data.py 負責共同判定
```

不得 stage、commit、push 或把 Vault 內容貼到 README、Issue、PR、commit 訊息。

## 3. 取得專案與名稱

公開 Repo：
`https://github.com/Jason5330/local-knowledge-compiler`

- 首選：由 AI Clone，資料夾名稱通常是 `local-knowledge-compiler`。
- 備用：GitHub Download ZIP，可能產生 `local-knowledge-compiler-master`，可安全改名。
- ZIP 沒有 Git metadata，因此初始化會跳過 hooks；這不是錯誤。

## 4. OneDrive A2 決策

OneDrive 只警告、不阻擋。警告輸出到 stderr，避免破壞 stdout 的 JSON。提醒使用者避免
兩台電腦同時修改同一 Vault，但不得因偵測到 OneDrive 而拒絕初始化或查詢。

## 5. Vault 尋找規則

對 prepare、finalize、status、resume、lint、rebuild：

```text
明確 --vault
→ 已初始化的 cwd
→ 已初始化的 cwd/KnowledgeBase
→ 找不到就回傳可操作錯誤
```

watch 與 ingest-once 維持明確 Vault 參數。`kb init` 無 path 時建立
`<cwd>/KnowledgeBase`；`kb init <path>` 保留顯式位置相容性。

## 6. 自然語言命令對照

```text
初始化本專案知識庫
→ 安裝環境、kb init、Git 保護、status、lint

把新資料整理進我的知識庫
→ 保留原檔、複製到 00_inbox、ingest-once、status、lint

用我的知識庫回答這個問題
→ prepare、只讀 packet、證據式回答

保存這次回答
→ 建立合規 answer、finalize、檢查衍生工作、lint

檢查知識庫是否正常
→ status、lint，只讀優先

繼續處理知識庫中卡住的工作
→ status、讀 handoff、resume、再驗證
```

根目錄的 `AGENTS.md` 與 `CLAUDE.md` 是兩種代理的共同入口；初始化後還要讀
`KnowledgeBase/80_system/KNOWLEDGE_PROTOCOL.md`。

## 7. Excel 安全規則

同磁碟 ingest 可能 claim/move 傳入的 inbox 檔，因此：

```text
禁止：把使用者唯一原始 Excel 直接傳給 ingest-once
正確：保留原檔 → 建立 00_inbox 副本 → 只處理副本 → 再確認原檔
```

完成後分別驗證 raw、index、job state、status、lint。

## 8. 提問與回答保存

每次提問：

1. `prepare` 建立問題專屬 packet。
2. 只讀 packet 內證據。
3. 證據不足就明說無法判定。
4. 引用欄位從 packet 原樣複製。
5. 對話回答預設不寫回。
6. 只有使用者要求「保存這次回答」才 `finalize`。
7. 驗證衍生整理狀態與 lint。

因此，同樣的問題回答不會自動記錄。只有成功 finalize 的回答才保存並參與下一輪整理。

## 9. 錯誤回饋與自動修正

使用者說「這個回答錯了」是明確觸發信號：

```text
讀回上次 packet
→ 重新核對原始 Excel 證據
→ 有明確證據才建立 correction
→ 保存於本機 50_corrections
→ 重新 prepare
→ 逐條輸出 correction_decisions
→ finalize 機械驗證後才保存
```

權威順序是 `原始 Excel 證據 ＞ 修正紀錄 ＞ 模型推測`。修正不得替代證據。建立來源
只允許明確使用者回報，或引用身分、十進位關係、單位換算的確定性驗證；主觀懷疑不寫入。

修正生命週期：

- `active`：有效，可被 prepare 帶入。
- `stale`：新版本表格結構不再吻合，等待確認。
- `suspended`：遇到明確例外或使用者暫停。
- `retired`：永久退役，不會自動恢復。

新來源版本寫入 catalog 後會自動重驗同 space、同來源家族的修正。重驗失敗不回滾
不可變原始資料，而是在 status 顯示 `correction_revalidation.pending_attention`。
接手時要檢查 status 的 corrections 計數、執行 lint，必要時用 `corrections-check`。
不得把 `KnowledgeBase/50_corrections/` 的真實內容寫入公開文件或 Git。

## 10. 已知狀態與限制

- `pending_attention`：需代理接手，不等於資料遺失。
- `pending_extractor`：檔案保存了，但內容讀取器不足，不得冒充已讀。
- 裸網址只作書籤文字，不自動抓取。
- Codex Desktop 無背景 CLI 時使用 manual provider。
- Claude provider 只有在 Claude CLI 存在、登入且實測成功後使用。
- 不得自行設定排程、開機啟動、常駐或對外分享。

## 11. 接手順序

1. 只讀檢查目前工作樹，不覆蓋未提交修改。
2. 讀 `README.md`、本文件、`docs/BEGINNER_GUIDE.zh-TW.md`、
   `docs/HOW_IT_WORKS.zh-TW.md`、`docs/CLI_REFERENCE.zh-TW.md`。
3. 找到 Vault 後讀 `80_system/KNOWLEDGE_PROTOCOL.md`。
4. 執行 status 與 lint。
5. 用白話回報位置、最近狀態、警告與可做的下一步。

不要重新問本文件已有答案的事情，也不要未經檢查重新初始化。

## 12. 完成標準

安裝：

- `kb --help` 可執行。
- `KnowledgeBase/` 已建立在預期專案。
- Git ignore／hooks 正常，或 ZIP 模式已明確說明。
- status 可讀、lint 通過或警告已清楚列出。

匯入：

- 原始檔仍在原位置。
- raw 版本安全保存。
- 索引可找到新資料，或明確標示 extractor 缺口。
- status 與 lint 已檢查。

提問：

- packet 確由問題產生。
- 回答未超出證據，引用可驗證。
- 若要求保存，finalize 和後續狀態已確認。
- 每筆 applicable correction 都有 decision；conflict 時沒有強行保存。
- 修正健康狀態與新版本重驗警告已檢查。

## 13. 接手回報模板

```text
我已讀完交接資料。
目前專案位置：……
目前知識庫位置：……
Git 私人資料保護：……
最近狀態：……
需要注意：……
現在可以直接替你做：……
```
