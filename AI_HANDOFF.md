---
status: ready
branch: master
updated: 2026-07-27
repository: Jason5330/local-knowledge-compiler
---

# Local Knowledge Compiler 完整交接

這份文件讓另一台電腦上的 Codex、Claude Code 或其他 AI 接手。使用者是技術初學者，
代理應直接完成本機操作，不要要求使用者自己輸入終端機指令。

## 1. 使用者目標

使用者無法在 OA 環境安裝 Obsidian，希望建立：

```text
本地原始資料
→ 自動保存版本
→ 建立可搜尋索引
→ 提問時找出精準證據
→ AI 只依證據回答並引用
→ 將高品質答案迭代進 Wiki
```

系統須同時供 Codex 與 Claude Code 使用，不能綁死單一 AI。

## 2. 思想來源

- Andrej Karpathy 的 LLM Knowledge Bases / LLM Wiki 思路：
  <https://x.com/karpathy/status/2039805659525644595>
- Karpathy LLM Wiki Gist：
  <https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f>
- Kepano Obsidian skills：
  <https://github.com/kepano/obsidian-skills>

採用的核心不是 Obsidian 軟體，而是：

```text
原始資料不變 → Markdown Wiki 是可重建的整理層 → 每次回答有證據 → 新答案再參與整理
```

## 3. GitHub 與版本

- 帳號：`Jason5330`
- 私人倉庫：<https://github.com/Jason5330/local-knowledge-compiler>
- 主要分支：`master`
- 已驗證實作基線：`3eac172cfebb88c8dc8ed85c377b5e7eb53b89d4`
- 基線測試：`433 passed, 25 skipped`

接手時先拉取遠端最新版，並檢查工作樹。不得覆蓋使用者尚未提交的修改。

## 4. 預設位置

```text
程式：C:\AI\local-knowledge-compiler
知識庫：C:\KnowledgeBase
```

若實際位置不同，使用現有位置，不要為了符合預設而搬動資料。

## 5. 接手必讀

依序讀取：

1. `README.md`
2. `docs/BEGINNER_GUIDE.zh-TW.md`
3. `docs/CLI_REFERENCE.zh-TW.md`
4. `docs/superpowers/specs/2026-07-25-local-knowledge-iteration-system-design.md`
5. vault 的 `80_system/KNOWLEDGE_PROTOCOL.md`
6. vault 的 `80_system/STATE.md`

若 vault 尚未建立，第 5、6 項不存在是正常的。

## 6. 使用者互動規則

- 使用繁體中文和白話。
- 先給結果，再說必要原因。
- 代理自己執行檢查與操作。
- 只有瀏覽器登入、公司管理員權限、付款、隱私決策或破壞性操作才請使用者接手。
- 若需使用者操作，逐步說「看哪個畫面、按哪個按鈕」。
- 不要把一串技術指令當成教學答案。
- 沒有實際驗證，不可宣稱成功。

## 7. 系統能力

- `kb init`：建立 vault。
- `kb ingest-once`：匯入單一 inbox 副本。
- `kb watch`：持續監看 inbox。
- `kb prepare`：依問題建立證據包。
- `kb finalize`：驗證並保存答案，排入知識整理。
- `kb status`：查看工作狀態。
- `kb resume`：繼續卡住或人工交接的工作。
- `kb lint`：完整性檢查。
- `kb rebuild`：從原始資料重建衍生層。

精確語法見 `docs/CLI_REFERENCE.zh-TW.md`，那是代理內部參考，不是要使用者操作。

## 8. 已確認的重要限制

### Excel 安全規則

同磁碟匯入會 claim/move 傳入檔案。因此：

```text
絕對禁止：把使用者唯一原始 Excel 直接傳給 ingest-once
正確方式：保留原檔 → 複製到 00_inbox → 只處理副本
```

完成後必須再次確認原檔仍在原位置。

### Windows space

目前 Windows v1 的 raw path 元件驗證不接受冒號，因此不要使用
`project:<slug>`。專案資料先歸入 `work`，專案名保留在檔名或內容。

### Manual provider

`compiler.provider = "manual"` 會讓編譯工作進入 `pending_attention`，內部 Exit code
可能是 2。這不代表匯入失敗：raw evidence 與索引可能已完成，`prepare` 也可能已能
找到資料。代理需分別驗證，不可用單一代碼粗略判斷。

### Claude provider

只有在 Claude CLI 存在、已登入且實際呼叫成功後才使用 `provider = "claude"`。
失敗時保留可恢復 handoff，不得假裝 Wiki 已更新。

### Codex

Codex Desktop 不能當背景 CLI 時使用 manual provider。Codex 仍能完成匯入、檢索、
回答、finalize 與人工 handoff。

### 非文字檔

掃描 PDF、圖片、音訊、影片若無擷取器，標記 `pending_extractor`。檔案可保存，但
不得冒充已讀取。

### 網址

裸網址只當書籤文字，系統不自動抓網頁。

## 9. 安裝策略

代理應自行：

```text
檢查 Git／GitHub 登入／Python 3.13
→ 取得或更新私人倉庫
→ 建立 .venv
→ 安裝專案及開發依賴
→ 驗證 kb help
→ 保留或建立 vault
→ 選擇 provider
→ 執行 status 與 lint
→ 用白話回報
```

GitHub 網頁授權或公司管理員權限無法自動完成時，才停下請使用者操作。禁止要求使用者
貼出密碼、API key 或存取權杖。

不得自行設定背景常駐、Windows 排程、開機啟動或 `shell:startup`。

## 10. 每次匯入程序

1. 解析來源絕對路徑。
2. 確認檔案存在，記錄基本資訊。
3. 產生 collision-safe inbox 副本名。
4. 複製到 `00_inbox`。
5. 再次確認來源仍存在。
6. `ingest-once` 只接收副本。
7. 分別檢查 raw、index、job state。
8. 執行 `status` 與 `lint`。
9. 回報原檔、保存位置、可搜尋狀態與待處理事項。

## 11. 每次提問程序

1. `prepare` 建立 packet。
2. 只讀 packet 內證據。
3. 證據不足就明說無法判定。
4. 每個重要結論附結構化引用。
5. 使用者要求保存時建立 answer JSON。
6. `finalize`。
7. 驗證衍生工作狀態與 `lint`。

引用的 `source_id`、`version_id`、`locator`、`evidence_sha256` 必須從 packet 原樣複製。

## 12. 完成標準

安裝完成至少要有：

- 程式說明可執行。
- vault 可讀取。
- status 可讀取。
- lint 通過，或已清楚列出非阻斷警告。
- Codex／Claude provider 與實際能力相符。

匯入完成至少要有：

- 使用者原始檔仍在原位置。
- raw 版本安全保存。
- 索引可找到新資料，或明確標示擷取器不足。
- 狀態與 lint 已檢查。

提問完成至少要有：

- packet 確實由問題產生。
- 回答沒有超出證據。
- 引用可驗證。
- 若保存，finalize 與後續狀態已確認。

## 13. 目前文件設計決策

2026-07-27 起，面向使用者的教學改為「貼提示詞給 Codex／Claude Code」：

- 使用者不需自行操作終端機。
- 技術命令集中在 `docs/CLI_REFERENCE.zh-TW.md`，只供代理執行。
- Codex 與 Claude Code 有各自的首次安裝提示詞。
- Excel 匯入、更新、提問、健康檢查與換 AI 都有可直接複製的提示詞。

## 14. 接手後的第一個回覆

先做只讀檢查，再用這種格式回報：

```text
我已讀完交接資料。
目前程式位置：……
目前知識庫位置：……
最近狀態：……
需要注意：……
現在可以直接替你做：……
```

不要重新問已在本文件回答過的問題，也不要未經檢查就重新安裝。
