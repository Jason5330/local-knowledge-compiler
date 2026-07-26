---
status: in-progress
branch: master
timestamp: 2026-07-26T21:48:04+08:00
files_modified:
  - AI_HANDOFF.md
---

# Local Knowledge Compiler 完整交接文件

更新日期：2026-07-26  
專案狀態：第一版已完成、已測試、已上傳私人 GitHub  
交接對象：Codex、Claude 或其他能操作本機檔案與 PowerShell 的 AI

---

## 0. 新 AI 請先做什麼

請完整讀完本文件，再依序讀：

1. `README.md`
2. `docs/superpowers/specs/2026-07-25-local-knowledge-iteration-system-design.md`
3. `80_system/KNOWLEDGE_PROTOCOL.md`  
   注意：這個檔案要等真正執行 `kb init` 建立知識庫後，才會出現在知識庫裡。

不要重新設計或重寫已完成的系統。下一個實際工作是：

> 在新電腦安裝本專案，建立使用者的第一座知識庫，然後匯入 Excel 資料並驗證查詢。

如果使用者尚未提供 Excel 檔案路徑與知識庫存放路徑，只需要詢問這兩個路徑。

---

## 1. 使用者背景與溝通方式

- GitHub 帳號：`Jason5330`
- 語言：繁體中文
- 技術程度：初學者，不熟悉 Git、Python、Codex 或系統架構
- 使用方式：有時使用 Codex，有時使用 Claude
- 溝通偏好：
  - 先給結果
  - 使用最大白話
  - 適合時使用「A → B → C」流程
  - 不要堆太多技術名詞
  - 能由 AI 安全完成的操作，直接協助完成

向使用者解釋時，請把以下名詞翻譯成白話：

- repository／repo：放在 GitHub 上的專案資料夾
- clone：把 GitHub 專案下載到電腦
- vault：真正存放個人資料的知識庫資料夾
- index：讓系統快速找資料的目錄
- compiler：把原始資料整理成 Wiki 的背景整理員

---

## 2. 事情的來龍去脈

使用者原本想研究 Obsidian 知識庫與 `kepano/obsidian-skills`，但工作電腦無法安裝
Obsidian。因此改為設計一套：

- 不依賴 Obsidian
- 資料保存在本機
- Codex 與 Claude 通用
- 新資料可持續加入
- 每次提問先找本地證據
- 回答後可把有來源支持的成果再整理回知識庫

這套設計受到 Andrej Karpathy 的 LLM Knowledge Bases／LLM Wiki 思路啟發：

- 原始 X 文章：<https://x.com/karpathy/status/2039805659525644595>
- Karpathy 的 LLM Wiki Gist：
  <https://gist.github.com/karpathy/442a6bf555914893e9891c11519de94f>
- 最早研究的 Obsidian skills：
  <https://github.com/kepano/obsidian-skills>

保留的核心想法是：

```text
原始資料 → AI 持續整理成 Markdown Wiki → 提問時讀取相關知識 → 新成果再迭代回去
```

本專案在這個想法上增加了安全措施：

- 原始檔永遠保留，不由 AI 覆寫
- 同一檔案更新時建立新版本
- 所有重要結論必須附來源定位
- 新舊資料衝突時並列，不偷偷選一邊
- Codex 與 Claude 使用同一份規則
- SQLite 只是可重建的搜尋目錄，不是唯一真相
- Wiki 修改使用 Git 保存歷史，可回復
- 多個 AI 同時工作時，寫入會排隊，避免互相覆蓋

---

## 3. 已完成的成果

私人 GitHub 儲存庫：

<https://github.com/Jason5330/local-knowledge-compiler>

目前 Git 狀態：

- 預設分支：`master`
- 保留分支：`feature/local-knowledge-compiler`
- 第一版程式的已驗證基準提交：
  `3eac172cfebb88c8dc8ed85c377b5e7eb53b89d4`
- `feature/local-knowledge-compiler` 保留在上述程式基準。
- `master` 在上述基準之後加入本交接文件，因此會比功能分支多出文件提交；這不是
  未合併的程式功能。
- 第一版最終測試：`433 passed, 25 skipped`
- 第一版整合審查：沒有剩餘的 Critical 或 Important 問題
- 儲存庫權限：PRIVATE

已完成的主要能力：

- `kb init`：建立知識庫骨架
- `kb watch`：持續監看新資料
- `kb ingest-once`：手動匯入單一檔案
- `kb prepare`：依問題建立本地證據包
- `kb finalize`：保存有引用的答案並排入知識迭代
- `kb status`：查看卡住或等待人工處理的工作
- `kb resume`：繼續先前卡住的工作
- `kb lint`：檢查知識庫健康狀態
- `kb rebuild`：從本地檔案重建搜尋索引

---

## 4. 「程式專案」與「知識庫」不是同一個資料夾

這點最容易讓初學者混淆。

```text
local-knowledge-compiler
→ 工具本身，相當於知識庫的機器

我的知識庫
→ 使用者自己的 Excel、文件、Wiki 與回答
```

建議在新電腦使用兩個不同路徑，例如：

```text
C:\AI\local-knowledge-compiler
C:\KnowledgeBase
```

不要把使用者的私人 Excel 直接提交到工具的 GitHub 儲存庫。

---

## 5. 在另一台 Windows 電腦接手

### 5.1 前置需求

需要安裝：

- Git
- GitHub CLI，指令名稱是 `gh`
- Python 3.13

先登入 GitHub：

```powershell
gh auth login
```

### 5.2 下載私人專案

```powershell
New-Item -ItemType Directory -Force "C:\AI"
Set-Location "C:\AI"
gh repo clone Jason5330/local-knowledge-compiler
Set-Location "C:\AI\local-knowledge-compiler"
```

如果使用者把專案放在別的磁碟或資料夾，後續命令要跟著更換路徑，不要硬套
`C:\AI`。

### 5.3 建立本機執行環境

`.venv` 不會上傳 GitHub，所以每台新電腦都要建立一次：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

驗證工具：

```powershell
.\.venv\Scripts\kb.exe --help
.\.venv\Scripts\python.exe -m pytest -q
```

完整測試可能需要一點時間。若只是第一次替使用者安裝，至少要確認 `kb --help`
可以正常執行；若修改程式碼，完成前必須跑相關測試。

---

## 6. 建立第一座知識庫

假設使用者選擇：

```text
C:\KnowledgeBase
```

執行：

```powershell
Set-Location "C:\AI\local-knowledge-compiler"
.\.venv\Scripts\kb.exe init "C:\KnowledgeBase"
```

初始化後會建立：

```text
C:\KnowledgeBase\
├── 00_inbox\       新資料入口
├── 10_raw\         永久保存的原始證據與歷史版本
├── 20_wiki\        AI 維護的知識頁
├── 30_answers\     保存的重要回答
├── 40_index\       搜尋索引與可讀目錄
├── 80_system\      Codex／Claude 共用規則與設定
├── 90_logs\        事件與查詢紀錄
├── 99_trash\       可復原回收區
├── .kb\            佇列、暫存與鎖
├── AGENTS.md       Codex 入口
└── CLAUDE.md       Claude 入口
```

初始化後，新 AI 必須讀取：

```text
C:\KnowledgeBase\80_system\KNOWLEDGE_PROTOCOL.md
```

這是 Codex 與 Claude 的唯一正式知識處理規則。

---

## 7. 使用者目前最關心：Excel 怎麼辦

系統已直接支援：

- `.xlsx`
- `.xlsm` 的儲存格資料
- 多工作表
- 文字、數字、日期與公式已計算後的值
- 每一列會保存「工作表名稱＋儲存格範圍」作為來源定位

舊格式 `.xls` 不支援，請先用 Excel 另存為 `.xlsx`。

目前不要承諾能完整理解：

- 圖表
- 內嵌圖片
- 巨集程式本身
- 密碼保護的活頁簿
- Excel 外部連結的即時內容

`.xlsm` 只讀取可見的儲存格資料，不執行巨集。

### 7.1 最安全的一次性匯入

不要刪除使用者原本的 Excel。可直接從原路徑匯入：

```powershell
Set-Location "C:\AI\local-knowledge-compiler"
.\.venv\Scripts\kb.exe ingest-once `
  "C:\KnowledgeBase" `
  "C:\使用者資料\資料.xlsx" `
  --space work
```

空間選擇：

- 個人或私密資料：`personal`
- 工作資料：`work`
- 可共用資料：`shared`
- 還不能判斷：`unclassified`
- 特定專案：`project:<英文專案代號>`

不要擅自把低信心資料放進 `shared`。

### 7.2 持續監看

若使用者希望「檔案放進去後自動整理」：

```powershell
.\scripts\start-kb.ps1 -Vault "C:\KnowledgeBase"
```

接著使用者只要把新資料放進：

```text
C:\KnowledgeBase\00_inbox
```

監看器會等待檔案複製完成後再處理，避免讀到半個檔案。

### 7.3 Excel 更新時

同一份 Excel 內容改變後再次匯入：

```text
舊版本保留 → 新版本建立 → 新內容重新索引 → Wiki 排隊更新
```

內容完全相同時會依雜湊去重，不會假裝它是新知識。

---

## 8. 每次提問的正式流程

使用者可以在 Codex 或 Claude 自然提問，但 AI 必須先建立證據包。

範例：

```powershell
.\.venv\Scripts\kb.exe prepare "這份 Excel 的主要決策與待辦是什麼？" `
  --vault "C:\KnowledgeBase" `
  --space work `
  --output "C:\KnowledgeBase\.kb\last-packet.json"
```

AI 只能依證據包回答，不得用模型印象補答案。回答要包含：

1. 直接結論
2. 證據整理
3. 來源與定位
4. 衝突與時效
5. 信心
6. 未知事項
7. 下一個應補進知識庫的本地資料

回答 JSON 範例：

```json
{
  "conclusion": "依目前本地資料整理出的結論。",
  "citations": [
    {
      "source_id": "從證據包原樣複製",
      "version_id": "從證據包原樣複製",
      "locator": "從證據包原樣複製",
      "evidence_sha256": "從證據包原樣複製"
    }
  ],
  "confidence": "high",
  "conflicts": "沒有發現衝突。"
}
```

引用欄位必須從證據包原樣複製。證據不足時：

- `conclusion` 明說無法判定
- `citations` 使用空陣列
- `confidence` 使用 `low`

完成答案後：

```powershell
.\.venv\Scripts\kb.exe finalize `
  --vault "C:\KnowledgeBase" `
  --packet "C:\KnowledgeBase\.kb\last-packet.json" `
  --answer "C:\KnowledgeBase\.kb\answer.json"
```

---

## 9. 日常使用的白話流程

```text
新資料
→ 放入 00_inbox 或用 ingest-once
→ 原始版本永久保存到 10_raw
→ 抽取可搜尋文字
→ 排隊整理 Wiki
→ 提問時 prepare 找證據
→ AI 依證據回答
→ finalize 保存有引用的成果
→ 後續問題使用累積後的知識
```

---

## 10. 背景整理員的設定

知識庫建立後查看：

```text
C:\KnowledgeBase\80_system\config.toml
```

`compiler.provider` 支援：

- `"claude"`：背景呼叫 Claude CLI 整理 Wiki
- `"manual"`：不自動呼叫模型，建立人工交接工作

Codex Desktop 目前不能直接當成背景 CLI，但 Codex 仍能完整使用：

- `kb prepare`
- 讀取證據包
- 產生有引用的回答
- `kb finalize`

若 Claude CLI 不存在或失敗，系統會把工作標成 `pending_attention`，不會假裝整理
成功。

查看工作：

```powershell
.\.venv\Scripts\kb.exe status --vault "C:\KnowledgeBase"
```

繼續工作：

```powershell
.\.venv\Scripts\kb.exe resume `
  --vault "C:\KnowledgeBase" `
  --job-id "畫面顯示的-job_id"
```

---

## 11. 健康檢查與重建

檢查知識庫：

```powershell
.\.venv\Scripts\kb.exe lint --vault "C:\KnowledgeBase"
```

重新建立搜尋目錄：

```powershell
.\.venv\Scripts\kb.exe rebuild --vault "C:\KnowledgeBase"
```

SQLite 索引可以重建。不要因為索引損壞就刪除 `10_raw` 或 `20_wiki`。

---

## 12. 不可破壞的安全原則

接手的 AI 必須遵守：

1. 不永久刪除原始資料。
2. 不直接覆寫 `10_raw`。
3. 不把 AI 回答冒充原始來源。
4. 不在沒有證據時猜測。
5. 不擅自跨 `personal`、`work`、`shared` 或其他專案搜尋。
6. 不因檔案裡有網址就自動上網下載。
7. 不把私人知識庫內容推到工具的 GitHub 儲存庫。
8. 不修改使用者原始 Excel；匯入時保留原檔。
9. 不在工作失敗時回報假成功。
10. 不重建已經完成並通過測試的第一版架構，除非使用者提出新需求或發現缺陷。

重要隱私提醒：

```text
資料保存在本機
≠
送給 Codex／Claude 的證據仍完全離線
```

若證據包交給雲端模型，內容仍可能離開本機。處理公司機密或個資前，必須遵守使用者
的公司政策與模型服務設定。

---

## 13. 重要程式與文件位置

- 使用說明：`README.md`
- 正式設計：
  `docs/superpowers/specs/2026-07-25-local-knowledge-iteration-system-design.md`
- 實作計畫：
  `docs/superpowers/plans/2026-07-25-local-knowledge-compiler-implementation.md`
- CLI：`src/local_kb/cli.py`
- 收錄流程：`src/local_kb/ingest.py`
- Excel／Word 抽取：`src/local_kb/extractors/office.py`
- 查詢：`src/local_kb/query.py`
- 答案回寫：`src/local_kb/finalize.py`
- 背景編譯：`src/local_kb/compiler.py`
- Wiki 發布：`src/local_kb/wiki.py`
- 安全交易：`src/local_kb/transaction.py`
- 健康檢查：`src/local_kb/health.py`
- 共用規則模板：`src/local_kb/templates/KNOWLEDGE_PROTOCOL.md`
- Windows 監看啟動器：`scripts/start-kb.ps1`
- 測試：`tests/`

---

## 14. 下一位 AI 的優先工作

目前不需要繼續開發程式。請依序：

1. 確認新電腦能登入私人 GitHub。
2. Clone `Jason5330/local-knowledge-compiler`。
3. 建立 `.venv` 並安裝專案。
4. 詢問或確認使用者想把真正的知識庫放在哪裡。
5. 執行 `kb init`。
6. 詢問或確認 Excel 的完整路徑及資料空間。
7. 用 `kb ingest-once` 匯入第一份 Excel。
8. 執行 `kb status` 與 `kb lint`。
9. 用一個實際問題執行 `kb prepare`。
10. 把結果用最大白話交付給使用者。

只有在以上步驟出現實際錯誤時，才進入除錯或修改程式。

---

## 15. 可直接貼給另一個 AI 的接手指令

```text
請先完整閱讀專案根目錄的 AI_HANDOFF.md，再閱讀 README.md。

這是我的本地知識庫工具，GitHub 私人儲存庫是：
https://github.com/Jason5330/local-knowledge-compiler

不要重新設計已完成的系統。請先檢查目前電腦的 Git、GitHub 登入、Python 3.13
與專案安裝狀態，然後依 AI_HANDOFF.md 的「下一位 AI 的優先工作」繼續。

我是初學者，請用繁體中文、最大白話、先給結果。執行任何操作後都要驗證，不要在
失敗時回報成功。不要刪除或修改我的原始 Excel。
```

---

## 16. 交接時的已知未完成事項

- 尚未在使用者選定的位置建立第一座實際知識庫。
- 尚未取得並匯入使用者的第一份 Excel。
- 尚未用使用者自己的資料跑第一次 `prepare → 回答 → finalize`。
- 另一台電腦必須重新建立 `.venv`。
- GitHub 是私人儲存庫，新電腦必須先用 `Jason5330` 有權限的帳號登入。

這些是正常的下一步，不代表第一版程式未完成。
