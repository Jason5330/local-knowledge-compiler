# 本地知識庫指令參考

這是完整指令表。第一次安裝請先讀
[零基礎安裝與使用指南](BEGINNER_GUIDE.zh-TW.md)。

以下範例假設：

```text
工具：C:\AI\local-knowledge-compiler
知識庫：C:\KnowledgeBase
```

先進入工具資料夾：

```powershell
Set-Location "C:\AI\local-knowledge-compiler"
```

---

## 指令總表

| 指令 | 用途 | 會不會修改知識庫 |
|---|---|---|
| `kb init` | 建立知識庫 | 會 |
| `kb watch` | 持續監看並匯入 | 會 |
| `kb ingest-once` | 匯入一個檔案 | 會 |
| `kb prepare` | 依問題準備證據包 | 會寫入證據包檔案，不改原始證據 |
| `kb finalize` | 保存有引用的回答 | 會 |
| `kb status` | 查看待處理工作 | 不會 |
| `kb resume` | 繼續一個待處理工作 | 會 |
| `kb lint` | 健康檢查 | 不會 |
| `kb rebuild` | 重建搜尋索引 | 會重建索引，不改原始來源 |

查看全部指令：

```powershell
.\.venv\Scripts\kb.exe --help
```

查看某個指令：

```powershell
.\.venv\Scripts\kb.exe prepare --help
```

---

## `kb init`

用途：建立一座新知識庫。

格式：

```text
kb init <知識庫路徑>
```

範例：

```powershell
.\.venv\Scripts\kb.exe init "C:\KnowledgeBase"
```

成功：

```text
Initialized knowledge vault: C:\KnowledgeBase
```

補充：

- 已存在的預設設定與規則檔不會被初始化命令擅自覆蓋。
- 路徑有空格或中文時，保留雙引號。

---

## `kb ingest-once`

用途：匯入一個本地檔案。

格式：

```text
kb ingest-once <知識庫> <檔案> --space <空間>
```

安全規則：先把唯一原檔複製到 `00_inbox`，只處理副本。

範例：

```powershell
.\.venv\Scripts\kb.exe ingest-once `
  "C:\KnowledgeBase" `
  "C:\KnowledgeBase\00_inbox\資料.xlsx" `
  --space work
```

參數：

| 參數 | 必填 | 說明 |
|---|---|---|
| 第一個路徑 | 是 | 知識庫根目錄 |
| 第二個路徑 | 是 | 要匯入的檔案副本 |
| `--space` | 否 | 預設 `unclassified` |

可用空間：

- `personal`
- `work`
- `shared`
- `unclassified`

第一版 Windows 匯入請不要使用設計中預留的 `project:<代號>`；原始檔保存層目前
不接受帶冒號的空間名稱。特定專案資料暫時使用 `work`，並在檔名或內容標示專案。

---

## `kb watch`

用途：一直監看 `00_inbox` 裡直接放入的新檔案。

格式：

```text
kb watch <知識庫> --space <空間>
```

範例：

```powershell
.\.venv\Scripts\kb.exe watch "C:\KnowledgeBase" --space work
```

停止：

```text
Ctrl + C
```

注意：

- `--space` 預設是 `unclassified`。
- 一次只用一個 space 監看同一個 inbox。
- 新檔案要直接放進 `00_inbox`，不要藏在子資料夾。

---

## `kb prepare`

用途：根據問題搜尋本地知識，建立證據包。

格式：

```text
kb prepare "<問題>" --vault <知識庫> --space <空間> --output <輸出檔>
```

範例：

```powershell
.\.venv\Scripts\kb.exe prepare `
  "最後決定了什麼？" `
  --vault "C:\KnowledgeBase" `
  --space work `
  --output ".kb\last-packet.json"
```

參數：

| 參數 | 必填 | 預設 | 說明 |
|---|---|---|---|
| 問題 | 是 | 無 | 想查詢的自然語言問題 |
| `--vault` | 否 | 目前資料夾 | 建議初學者永遠明確填寫 |
| `--space` | 否 | 安全推斷 | 可重複指定；初學者建議明確填寫 |
| `--output` | 否 | `.kb/last-packet.json` | 必須留在知識庫內 |

跨多個已授權空間：

```powershell
.\.venv\Scripts\kb.exe prepare "共同資料是什麼？" `
  --vault "C:\KnowledgeBase" `
  --space work `
  --space shared
```

不要在未授權時把 `personal` 和 `work` 混在一起。

查詢層雖能辨識 `project:<代號>`，但第一版 Windows 匯入尚不能安全建立這種原始
來源；因此初學者目前不要使用。

---

## `kb finalize`

用途：驗證引用、保存答案，並建立下一輪知識整理工作。

格式：

```text
kb finalize --vault <知識庫> --packet <證據包> --answer <答案 JSON>
```

範例：

```powershell
.\.venv\Scripts\kb.exe finalize `
  --vault "C:\KnowledgeBase" `
  --packet "C:\KnowledgeBase\.kb\last-packet.json" `
  --answer "C:\KnowledgeBase\.kb\answer.json"
```

答案必要欄位：

- `conclusion`
- `citations`
- `confidence`

`confidence` 只能是：

- `high`
- `medium`
- `low`

引用必須與證據包完全一致，包含 `evidence_sha256`。

成功：

```text
Saved answer: ...
Queued derived update: ...
```

---

## `kb status`

用途：只查看目前有哪些工作未完成，不修改知識庫。

範例：

```powershell
.\.venv\Scripts\kb.exe status --vault "C:\KnowledgeBase"
```

重要欄位：

| 欄位 | 白話 |
|---|---|
| `healthy` | 目前是否沒有待處理問題 |
| `attention_required` | 是否需要你或 AI 處理 |
| `job_id` | 指定工作的編號 |
| `type` | 原始檔匯入或答案衍生更新 |
| `state` | 目前做到哪一步 |
| `error` | 錯誤原因 |
| `handoff_path` | 人工交接檔的位置 |

---

## `kb resume`

用途：修正背景編譯器後，繼續指定工作。

範例：

```powershell
.\.venv\Scripts\kb.exe resume `
  --vault "C:\KnowledgeBase" `
  --job-id "畫面上的-job_id"
```

`job_id` 必須從 `kb status` 的結果原樣複製。

---

## `kb lint`

用途：只讀取並檢查知識庫是否健康。

範例：

```powershell
.\.venv\Scripts\kb.exe lint --vault "C:\KnowledgeBase"
```

成功重點：

```json
"healthy": true
```

如果是 `false`，把完整 JSON 交給 Codex 或 Claude 分析，不要自行刪檔。

---

## `kb rebuild`

用途：從保存的抽取快取重建 SQLite 搜尋索引。

範例：

```powershell
.\.venv\Scripts\kb.exe rebuild --vault "C:\KnowledgeBase"
```

成功：

```text
Indexed sources: 數字
```

不要把 `rebuild` 當作清理原始資料。它只負責搜尋目錄。

---

## 結束代碼

| Exit code | 意思 |
|---|---|
| `0` | 指令完成 |
| `1` | 發生錯誤 |
| `2` | 工作尚未完成或健康檢查需要注意 |

Exit code `2` 不一定代表匯入失敗。例如 Claude CLI 不可用時，原始資料與索引可能
已完成，但 Wiki 整理停在人工交接。

---

## 相關文件

- [零基礎安裝與使用指南](BEGINNER_GUIDE.zh-TW.md)
- [README](../README.md)
- [AI 完整交接文件](../AI_HANDOFF.md)
