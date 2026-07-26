# 本地知識庫零基礎安裝與使用指南

這份指南寫給完全沒有技術基礎的人。你不需要懂 Python、Git、資料庫或程式架構。
只要照順序做，不要跳步。

適用環境：

- Windows 10 或 Windows 11
- 你的 GitHub 帳號可以開啟私人專案 `Jason5330/local-knowledge-compiler`
- 你有網路可以安裝工具

完成後，你會得到：

```text
Excel／文件
→ 放進本機知識庫
→ 系統保存原始版本並建立搜尋目錄
→ Codex 或 Claude 先找本地證據
→ 用繁體中文回答並附來源
→ 有用的答案繼續整理回知識庫
```

---

## 第一部分：先看懂你要安裝的東西

這套系統有兩個不同的資料夾。

### 1. 工具資料夾

範例：

```text
C:\AI\local-knowledge-compiler
```

它是「知識庫機器」。裡面放程式，不放你的私人資料。

### 2. 知識庫資料夾

範例：

```text
C:\KnowledgeBase
```

它是「你的資料倉庫」。Excel、文件、Wiki、答案與搜尋目錄都放這裡。

請記住：

```text
工具資料夾 ≠ 你的知識庫
```

不要把私人 Excel 放進工具的 GitHub 專案。

---

## 第二部分：第一次安裝

這一部分每台新電腦只做一次。

### 步驟 1：開啟 PowerShell

1. 按鍵盤的 Windows 鍵。
2. 輸入 `PowerShell`。
3. 點開「Windows PowerShell」或「PowerShell」。

PowerShell 是一個可以輸入指令的視窗。看到藍色、黑色或深色畫面都正常。

後面的灰色指令框，都是要貼到 PowerShell 執行的內容。

### 步驟 2：確認電腦能不能使用 winget

貼上：

```powershell
winget --version
```

如果出現版本號，例如：

```text
v1.x.x
```

代表可以繼續。

如果顯示「找不到 winget」：

1. 開啟 Microsoft Store。
2. 搜尋並更新「應用程式安裝程式」或 `App Installer`。
3. 更新後關閉 PowerShell，再重新開啟一次。

### 步驟 3：安裝 Git

Git 的白話意思：保存專案版本，並從 GitHub 下載專案。

貼上：

```powershell
winget install --id Git.Git -e --source winget
```

安裝畫面若詢問是否同意，輸入 `Y` 再按 Enter。

官方下載頁：

<https://git-scm.com/install/windows.html>

### 步驟 4：安裝 GitHub CLI

GitHub CLI 的白話意思：讓 PowerShell 可以登入 GitHub、下載私人專案。

貼上：

```powershell
winget install --id GitHub.cli --source winget
```

官方說明：

<https://github.com/cli/cli/blob/trunk/docs/install_windows.md>

### 步驟 5：安裝 Python 3.13

Python 的白話意思：實際執行知識庫工具的引擎。

貼上：

```powershell
winget install --id Python.Python.3.13 -e --source winget
```

官方下載頁：

<https://www.python.org/downloads/windows/>

請安裝 Python 3.13。不要只裝 3.12，也不要因為網站顯示更新的 3.14 就跳過 3.13；
這個版本的知識庫明確要求 Python 3.13 或以上，但目前的完整驗證環境使用 3.13。

### 步驟 6：關閉 PowerShell，再重新開啟

這一步不能省略。剛安裝的工具通常要到新的 PowerShell 視窗才找得到。

### 步驟 7：確認三個工具都能使用

依序貼上：

```powershell
git --version
gh --version
py -3.13 --version
```

三個指令都應該顯示版本號。

如果其中一個顯示「不是內部或外部命令」或「無法辨識」：

1. 再關閉 PowerShell。
2. 重新開啟。
3. 再試一次。
4. 還是不行時，重新執行對應的安裝指令。

---

## 第三部分：登入 GitHub 並下載知識庫工具

### 步驟 1：登入 GitHub

貼上：

```powershell
gh auth login
```

畫面會逐步詢問。一般情況請選：

```text
GitHub.com
HTTPS
Login with a web browser
```

PowerShell 會顯示一組一次性代碼，並開啟瀏覽器。

1. 在瀏覽器登入 GitHub 帳號 `Jason5330`。
2. 輸入 PowerShell 顯示的代碼。
3. 同意授權。
4. 回到 PowerShell。

確認登入：

```powershell
gh auth status
```

成功時應看到類似：

```text
Logged in to github.com account Jason5330
```

### 步驟 2：建立工具存放位置

貼上：

```powershell
New-Item -ItemType Directory -Force "C:\AI"
Set-Location "C:\AI"
```

白話：

- 第一行建立 `C:\AI` 資料夾。
- 第二行進入這個資料夾。

### 步驟 3：從私人 GitHub 下載專案

貼上：

```powershell
gh repo clone Jason5330/local-knowledge-compiler
```

成功後會出現：

```text
C:\AI\local-knowledge-compiler
```

如果出現「資料夾已存在」，不要重複 clone。改用：

```powershell
Set-Location "C:\AI\local-knowledge-compiler"
git pull
```

`git pull` 的白話意思：下載 GitHub 上最新的更新。

### 步驟 4：進入工具資料夾

貼上：

```powershell
Set-Location "C:\AI\local-knowledge-compiler"
```

確認位置：

```powershell
Get-Location
```

應看到：

```text
C:\AI\local-knowledge-compiler
```

---

## 第四部分：建立工具自己的執行環境

這一段每台電腦做一次。若你刪除了 `.venv`，也要重新做一次。

`.venv` 的白話意思：替這套工具準備一個獨立的小房間，不和電腦上其他 Python
工具互相干擾。

### 步驟 1：建立 `.venv`

```powershell
py -3.13 -m venv .venv
```

這個指令可能幾秒沒有畫面，屬於正常現象。

### 步驟 2：安裝知識庫工具

```powershell
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

你會看到很多英文下載訊息。只要最後沒有紅色 `ERROR`，通常就是成功。

### 步驟 3：確認知識庫指令能執行

```powershell
.\.venv\Scripts\kb.exe --help
```

成功時會看到以下指令名稱：

```text
init
watch
ingest-once
prepare
finalize
status
resume
lint
rebuild
```

看到這些名稱，代表工具安裝成功。

---

## 第五部分：建立你自己的知識庫

以下範例把知識庫放在：

```text
C:\KnowledgeBase
```

如果你想放到 D 槽，可以把所有 `C:\KnowledgeBase` 改成例如
`D:\我的知識庫`。路徑有中文或空格時，雙引號不能拿掉。

### 步驟 1：初始化

先確認 PowerShell 位於工具資料夾：

```powershell
Set-Location "C:\AI\local-knowledge-compiler"
```

建立知識庫：

```powershell
.\.venv\Scripts\kb.exe init "C:\KnowledgeBase"
```

成功時會看到：

```text
Initialized knowledge vault: C:\KnowledgeBase
```

### 步驟 2：確認資料夾

```powershell
Get-ChildItem "C:\KnowledgeBase"
```

應看到：

```text
00_inbox
10_raw
20_wiki
30_answers
40_index
80_system
90_logs
99_trash
AGENTS.md
CLAUDE.md
```

各資料夾的白話意思：

| 資料夾 | 用途 |
|---|---|
| `00_inbox` | 你把新資料副本放進來 |
| `10_raw` | 系統永久保存的原始證據與歷史版本 |
| `20_wiki` | AI 根據來源整理的知識頁 |
| `30_answers` | 保存值得留下的問答 |
| `40_index` | 快速搜尋使用的目錄 |
| `80_system` | Codex 與 Claude 共用的規則與設定 |
| `90_logs` | 處理與查詢紀錄 |
| `99_trash` | 可復原的處理區或回收區 |
| `.kb` | 系統佇列、暫存與鎖；平常不用碰 |

### 步驟 3：健康檢查

```powershell
.\.venv\Scripts\kb.exe lint --vault "C:\KnowledgeBase"
```

剛建立的空知識庫應該顯示：

```json
"healthy": true
```

看到 `true` 代表正常。

---

## 第六部分：決定要不要安裝 Claude Code

這套工具可由 Codex 與 Claude 共用，但「背景自動把原始資料整理成 Wiki」目前使用
Claude Code 的 `claude` 指令。

### 情況 A：你已安裝 Claude Code

確認：

```powershell
claude --version
```

如果有版本號，保留知識庫的預設設定：

```toml
provider = "claude"
```

### 情況 B：你沒有 Claude Code

這不代表知識庫不能用。

仍可使用：

- 保存原始檔
- Excel／文件文字抽取
- 搜尋本地證據
- `kb prepare`
- 由 Codex 或其他 AI 根據證據回答
- `kb finalize`

差別是 Wiki 自動整理工作會顯示：

```text
pending_attention
```

白話意思：原始資料已保存，但「背景整理員」尚未完成 Wiki。

若目前不安裝 Claude Code，可以把：

```text
C:\KnowledgeBase\80_system\config.toml
```

裡面的：

```toml
provider = "claude"
```

改成：

```toml
provider = "manual"
```

這會明確使用人工交接模式，不會假裝背景整理成功。

請用「記事本」或其他文字編輯器修改，只改這一行。不要使用 Windows PowerShell
5.1 的 `Set-Content -Encoding utf8` 重寫整個 `config.toml`；它可能加入 BOM
字元，造成 `Invalid statement (at line 1, column 1)`。

### 情況 C：你想要背景自動整理

依 Anthropic 官方說明安裝 Claude Code：

<https://docs.anthropic.com/en/docs/claude-code/getting-started>

Windows 原生模式需要 Git for Windows。官方常用安裝方式還需要 Node.js：

```powershell
winget install --id OpenJS.NodeJS.LTS -e --source winget
```

安裝後關閉並重新開啟 PowerShell，再執行：

```powershell
npm install -g @anthropic-ai/claude-code
claude
```

第一次執行 `claude` 時，依畫面登入 Claude 帳號。完成後檢查：

```powershell
claude --version
claude doctor
```

注意：

- Claude Code 需要可用的 Anthropic 帳戶或方案。
- 公司電腦可能封鎖安裝或網路連線。
- 背景整理送出的內容會經過 Claude 雲端服務，不等於完全離線。

---

## 第七部分：第一次安全匯入 Excel

### 最重要的安全規則

不要直接把唯一一份原始 Excel 交給 `ingest-once`。

正確流程：

```text
唯一原檔
→ 先複製一份到 00_inbox
→ 只處理 00_inbox 裡的副本
→ 原檔留在原來的位置
```

原因：系統會接管它收到的檔案。在同一個磁碟上，`ingest-once` 可能把該檔案移進
處理區。只要處理的是副本，你的唯一原檔就不會受到影響。

### 步驟 1：確認 Excel 格式

直接支援：

- `.xlsx`
- `.xlsm` 的儲存格資料

不支援舊格式：

- `.xls`

如果是 `.xls`：

1. 用 Excel 開啟。
2. 點「檔案」。
3. 點「另存新檔」。
4. 選擇「Excel 活頁簿（.xlsx）」。
5. 再匯入新存的 `.xlsx`。

### 步驟 2：用檔案總管複製

假設原檔是：

```text
C:\使用者資料\業務資料.xlsx
```

操作：

1. 在檔案總管找到 `業務資料.xlsx`。
2. 按右鍵，選「複製」。
3. 前往 `C:\KnowledgeBase\00_inbox`。
4. 在空白處按右鍵，選「貼上」。

貼上後應有：

```text
C:\KnowledgeBase\00_inbox\業務資料.xlsx
```

如果 `00_inbox` 已經有同名檔案，而且它可能還在處理，不要按「取代」。請先等待
處理完成，或把新副本改成例如：

```text
業務資料_2026-07-26.xlsx
```

### 步驟 3：選擇資料空間

空間的白話意思：避免私人資料與工作資料混在一起。

| 資料內容 | 使用的 space |
|---|---|
| 個人筆記、健康、私密資料 | `personal` |
| 公司、客戶、工作資料 | `work` |
| 可以安全共用的資料 | `shared` |
| 還不能判斷 | `unclassified` |
第一版在 Windows 匯入時，請只使用上表中的 `personal`、`work`、`shared`、
`unclassified`。設計中雖預留 `project:<專案代號>`，但目前 Windows 原始檔保存層
尚未開放這種帶冒號的空間名稱。不要使用它，以免匯入失敗。特定專案資料可暫時放進
`work`，並在檔名或文件內容寫明專案名稱。

### 步驟 4：執行一次性匯入

以下假設它是工作資料：

```powershell
Set-Location "C:\AI\local-knowledge-compiler"
.\.venv\Scripts\kb.exe ingest-once `
  "C:\KnowledgeBase" `
  "C:\KnowledgeBase\00_inbox\業務資料.xlsx" `
  --space work
```

PowerShell 裡的反引號 `` ` `` 表示「下一行仍是同一個指令」。若複製後出錯，也可以
改成單行：

```powershell
.\.venv\Scripts\kb.exe ingest-once "C:\KnowledgeBase" "C:\KnowledgeBase\00_inbox\業務資料.xlsx" --space work
```

### 步驟 5：看懂結果

成功且背景編譯完成時，通常會看到類似：

```text
ver_xxxxxxxxx extracted
```

如果看到：

```text
pending_attention
Claude 不可用，需要人工處理
```

白話意思：

- Excel 原始版本通常已保存。
- 可搜尋文字通常已抽取並建立索引。
- Wiki 背景整理尚未完成。
- 這不是資料消失。

記下畫面上的 `job_id`，之後可以用 `status` 與 `resume` 處理。

### Excel 會讀到什麼

系統會讀取：

- 每個工作表
- 非空白的列
- 文字
- 數字
- 日期
- Excel 儲存過的公式計算結果

來源會標示為：

```text
sheet:工作表名稱;cells:A1-C1
```

這樣回答可以指出資料來自哪個工作表、哪一段儲存格。

目前不保證讀取：

- 圖表代表的含意
- 內嵌圖片
- 巨集程式
- 密碼保護活頁簿
- 尚未計算或未儲存的公式最新結果
- 外部連結的即時資料

`.xlsm` 不會執行巨集，只讀取儲存格可見資料。

---

## 第八部分：持續自動收資料

如果你希望：

```text
把副本放進 00_inbox → 系統自動處理
```

請開一個 PowerShell 視窗，輸入：

```powershell
Set-Location "C:\AI\local-knowledge-compiler"
.\.venv\Scripts\kb.exe watch "C:\KnowledgeBase" --space work
```

這個視窗會一直停在執行狀態，屬於正常現象。它正在監看資料夾。

接著把工作資料的副本放到：

```text
C:\KnowledgeBase\00_inbox
```

注意：

- 檔案要直接放在 `00_inbox`，不要再建立多層子資料夾。
- 系統會等待檔案大小穩定後才讀取，避免讀到尚未複製完成的檔案。
- 關閉 PowerShell 或按 `Ctrl + C`，監看就會停止。
- 重新開機後要重新啟動監看，除非你另外建立 Windows 自動啟動。

如果資料類型不同，建議分批監看：

```powershell
# 工作資料
.\.venv\Scripts\kb.exe watch "C:\KnowledgeBase" --space work

# 個人資料
.\.venv\Scripts\kb.exe watch "C:\KnowledgeBase" --space personal
```

同一個 `00_inbox` 同時跑多個不同 space 的監看器可能造成分類競爭，不建議這樣做。
最安全做法是一次只啟動一個監看器，或使用 `ingest-once` 明確指定每個檔案的 space。

專案附帶的簡易啟動器：

```powershell
.\scripts\start-kb.ps1 -Vault "C:\KnowledgeBase"
```

它使用預設空間 `unclassified`。若你已經知道資料是 `work` 或 `personal`，直接使用
前面的 `kb.exe watch ... --space ...` 會更清楚。

---

## 第九部分：第一次提問

### 步驟 1：準備證據包

假設你想問：

```text
業務資料裡最重要的三個問題是什麼？
```

執行：

```powershell
Set-Location "C:\AI\local-knowledge-compiler"
.\.venv\Scripts\kb.exe prepare `
  "業務資料裡最重要的三個問題是什麼？" `
  --vault "C:\KnowledgeBase" `
  --space work `
  --output ".kb\last-packet.json"
```

成功時會印出證據包路徑，通常是：

```text
C:\KnowledgeBase\.kb\last-packet.json
```

證據包的白話意思：系統先從本地資料找出最相關片段，再把來源資訊一起交給 AI。

### 步驟 2：交給 Codex

建議把 `C:\KnowledgeBase` 開成 Codex 的工作資料夾，然後對 Codex 說：

```text
請先完整讀取 AGENTS.md 與 80_system/KNOWLEDGE_PROTOCOL.md。
我的證據包在 C:\KnowledgeBase\.kb\last-packet.json。
請只根據證據包，用繁體中文回答：
「業務資料裡最重要的三個問題是什麼？」
每個重要結論都要保留結構化引用；證據不足時不要猜。
```

### 步驟 3：交給 Claude

如果使用 Claude Code，可以先進入知識庫：

```powershell
Set-Location "C:\KnowledgeBase"
claude
```

`CLAUDE.md` 會要求 Claude 讀取同一份 `80_system\KNOWLEDGE_PROTOCOL.md`。

也可以直接說：

```text
請先完整讀取 CLAUDE.md 與 80_system/KNOWLEDGE_PROTOCOL.md。
證據包在 .kb/last-packet.json。
請只根據證據包回答，不要上網補資料。
```

### AI 回答必須包含

1. 直接結論
2. 證據整理
3. 來源與定位
4. 衝突與時效
5. 信心：`high`、`medium` 或 `low`
6. 未知事項
7. 下一個應補進知識庫的本地資料

如果 AI 沒有引用來源，請要求它重做，不要直接保存。

---

## 第十部分：保存答案，讓知識繼續迭代

AI 除了給你白話回答，還要建立答案 JSON，例如：

```json
{
  "conclusion": "依目前本地證據整理出的結論。",
  "citations": [
    {
      "source_id": "必須從證據包原樣複製",
      "version_id": "必須從證據包原樣複製",
      "locator": "必須從證據包原樣複製",
      "evidence_sha256": "必須從證據包原樣複製"
    }
  ],
  "confidence": "high",
  "conflicts": "沒有發現衝突。"
}
```

建議保存到：

```text
C:\KnowledgeBase\.kb\answer.json
```

再執行：

```powershell
Set-Location "C:\AI\local-knowledge-compiler"
.\.venv\Scripts\kb.exe finalize `
  --vault "C:\KnowledgeBase" `
  --packet "C:\KnowledgeBase\.kb\last-packet.json" `
  --answer "C:\KnowledgeBase\.kb\answer.json"
```

成功時會看到：

```text
Saved answer: ...
Queued derived update: ...
```

白話：

- 完整回答已保存到 `30_answers`。
- 有來源支持的內容已排隊，等待整理進 `20_wiki`。
- AI 的回答不會冒充原始 Excel。

如果本地根本沒有證據，答案 JSON 應使用：

```json
{
  "conclusion": "目前本地資料無法判定。",
  "citations": [],
  "confidence": "low",
  "conflicts": "缺少足夠資料。"
}
```

---

## 第十一部分：每天怎麼用

### 最簡單的日常流程

```text
1. 保留唯一原檔
2. 複製一份到 00_inbox
3. 用 ingest-once 指定 personal／work／shared／unclassified
4. 用 status 看有沒有卡住
5. 提問前執行 prepare
6. AI 只根據證據回答
7. 正確答案執行 finalize
8. 偶爾執行 lint
```

### 每天開始前

```powershell
Set-Location "C:\AI\local-knowledge-compiler"
git pull
```

這會把工具更新到 GitHub 最新版本。

接著檢查知識庫：

```powershell
.\.venv\Scripts\kb.exe status --vault "C:\KnowledgeBase"
.\.venv\Scripts\kb.exe lint --vault "C:\KnowledgeBase"
```

### 每天結束前

1. 確認沒有唯一原檔只剩在 `00_inbox`。
2. 查看 `status`。
3. 若有 `pending_attention`，保留畫面上的 `job_id`。
4. 不要手動刪除 `.kb`、`10_raw` 或 `40_index\catalog.sqlite3`。

---

## 第十二部分：Excel 更新後怎麼辦

假設原本的：

```text
業務資料.xlsx
```

後來內容更新。

正確做法：

1. 保留你原來工作的 Excel。
2. 另複製一份到 `00_inbox`。
3. 若同名副本仍存在，先等待處理完成或在檔名加日期。
4. 再次執行 `ingest-once`。

系統會：

```text
舊版本保留
→ 新版本建立
→ 新內容重新索引
→ Wiki 排隊更新
```

內容完全相同時會去重，不會重複冒充新知識。

---

## 第十三部分：支援哪些檔案

目前有文字抽取能力：

| 類型 | 副檔名 | 來源定位 |
|---|---|---|
| 純文字、Markdown | `.txt`、`.md` | 行號 |
| 資料與程式碼 | `.json`、`.csv`、`.py`、`.js`、`.ts` | 行號 |
| 離線網頁 | `.html`、`.htm` | 網頁標題 |
| PDF 文字檔 | `.pdf` | 頁碼 |
| Word | `.docx` | 段落或表格列 |
| Excel | `.xlsx`、`.xlsm` | 工作表與儲存格範圍 |

目前會保存原檔、但標記 `pending_extractor`：

- 圖片
- 截圖
- 掃描型 PDF
- 音訊
- 影片
- PowerPoint
- 舊版 `.xls`
- 其他尚未支援的副檔名

`pending_extractor` 的白話意思：

```text
檔案已保存
但系統目前不能可靠讀出裡面的文字
所以不會假裝已經理解
```

---

## 第十四部分：工作卡住時

### 查看狀態

```powershell
.\.venv\Scripts\kb.exe status --vault "C:\KnowledgeBase"
```

如果沒有卡住的工作：

```json
"healthy": true
```

如果有工作需要處理：

```json
"attention_required": true
```

畫面會列出：

- `job_id`
- 工作類型
- 目前狀態
- 錯誤
- 人工交接檔路徑
- `source_id`
- `version_id`

### 繼續指定工作

先修好 Claude Code或調整 `config.toml`，再執行：

```powershell
.\.venv\Scripts\kb.exe resume `
  --vault "C:\KnowledgeBase" `
  --job-id "把畫面上的-job_id-貼在這裡"
```

### 看懂結束代碼

有時 PowerShell 或 AI 會提到 exit code：

| 代碼 | 白話意思 |
|---|---|
| `0` | 成功，或沒有待處理工作 |
| `1` | 真正發生錯誤，請看 `kb:` 後面的原因 |
| `2` | 尚未全部完成，需要人工處理；不等於資料遺失 |

---

## 第十五部分：常見錯誤與處理

### 錯誤：找不到 `py`

畫面可能顯示：

```text
py is not recognized
```

處理：

```powershell
winget install --id Python.Python.3.13 -e --source winget
```

關閉 PowerShell，重新開啟，再試：

```powershell
py -3.13 --version
```

### 錯誤：找不到 `.venv\Scripts\python.exe`

原因：尚未建立工具環境，或目前不在工具資料夾。

處理：

```powershell
Set-Location "C:\AI\local-knowledge-compiler"
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
```

### 錯誤：GitHub 儲存庫找不到

畫面可能顯示：

```text
repository not found
```

處理：

```powershell
gh auth status
```

確認登入帳號是 `Jason5330`，而且該帳號有私人儲存庫權限。必要時重新登入：

```powershell
gh auth logout
gh auth login
```

### 錯誤：`Vault directory not found`

原因：知識庫路徑打錯，或尚未執行 `kb init`。

處理：

```powershell
.\.venv\Scripts\kb.exe init "C:\KnowledgeBase"
```

### 錯誤：`cannot infer a safe space`

原因：系統不敢猜資料是個人還是工作。

處理：明確加上：

```powershell
--space personal
```

或：

```powershell
--space work
```

### 錯誤：`Claude 不可用，需要人工處理`

這通常不是 Excel 匯入失敗，而是背景 Wiki 整理員不可用。

先檢查：

```powershell
claude --version
claude doctor
```

若不打算安裝 Claude Code，把 `config.toml` 改成：

```toml
provider = "manual"
```

仍可使用 Codex 做 `prepare → 回答 → finalize`。

### 錯誤：`Invalid statement (at line 1, column 1)`

常見原因：`80_system\config.toml` 被 Windows PowerShell 5.1 重新存成帶 BOM 的
UTF-8。

處理：

1. 用記事本開啟 `C:\KnowledgeBase\80_system\config.toml`。
2. 確認第一個字元就是 `[`，前面沒有空白或奇怪符號。
3. 另存時選擇 UTF-8。
4. 若不確定內容，應先備份該檔，再請 AI 依 README 的預設設定修復；不要刪除整座
   知識庫。

### 錯誤：Excel 無法讀取

檢查：

1. 是否為 `.xlsx` 或 `.xlsm`。
2. 是否有密碼。
3. 檔案是否損壞。
4. Excel 是否仍在儲存。
5. 工作表是否大到超出安全限制。

先用 Excel 開啟，再另存成新的 `.xlsx`，然後匯入新副本。

### 錯誤：掃描 PDF 沒有內容

如果 PDF 只有圖片，系統會顯示：

```text
PDF has no extractable text; OCR required
```

白話：需要 OCR 文字辨識。目前第一版不會假裝讀懂。

### 錯誤：PowerShell 指令換行失敗

把多行指令改成一整行。路徑保留雙引號。

例如：

```powershell
.\.venv\Scripts\kb.exe prepare "我的問題" --vault "C:\KnowledgeBase" --space work --output ".kb\last-packet.json"
```

---

## 第十六部分：健康檢查與搜尋索引修復

### 健康檢查

```powershell
.\.venv\Scripts\kb.exe lint --vault "C:\KnowledgeBase"
```

### 重建搜尋索引

只有在索引損壞或工具明確要求時才執行：

```powershell
.\.venv\Scripts\kb.exe rebuild --vault "C:\KnowledgeBase"
```

成功時會看到：

```text
Indexed sources: 數字
```

SQLite 只是搜尋目錄，可以從保存的檔案重建。不要刪除 `10_raw`。

---

## 第十七部分：備份與換電腦

工具程式已在私人 GitHub：

<https://github.com/Jason5330/local-knowledge-compiler>

但真正的：

```text
C:\KnowledgeBase
```

不會自動上傳 GitHub。

你仍需備份知識庫資料夾。可使用：

- 外接硬碟
- 公司核准的備份磁碟
- 公司核准的同步空間

備份前最好先：

1. 關閉 `kb watch`。
2. 執行 `kb status`。
3. 確認沒有正在寫入的工作。
4. 完整複製整個 `C:\KnowledgeBase`。

不要只備份 Excel，因為 `10_raw`、`20_wiki`、`30_answers` 與 `90_logs` 都是知識歷史。

---

## 第十八部分：隱私提醒

「知識庫保存在本機」只代表檔案與索引在你的電腦。

如果你把證據包交給 Codex 或 Claude：

```text
相關證據內容可能會送到模型服務
```

因此：

- 公司機密先遵守公司規範。
- 個資與敏感資料先確認服務設定。
- 不要把私人知識庫提交到工具的 GitHub 儲存庫。
- 裸網址只會當書籤；知識庫不會自動下載網頁。

---

## 第十九部分：可以直接交給 AI 的安裝指令

在新電腦開啟 Codex 或其他可操作本機的 AI，貼上：

```text
請協助我安裝本地知識庫。我是完全沒有技術基礎的初學者。

專案是私人 GitHub：
https://github.com/Jason5330/local-knowledge-compiler

請先完整閱讀：
1. AI_HANDOFF.md
2. docs/BEGINNER_GUIDE.zh-TW.md
3. README.md

請依零基礎指南逐步檢查 Git、GitHub CLI、Python 3.13、GitHub 登入、clone、
.venv、專案安裝與 kb --help。

工具建議放在 C:\AI\local-knowledge-compiler。
真正知識庫建議放在 C:\KnowledgeBase。

所有操作後都要驗證。不要直接把我的唯一原始 Excel 交給 ingest-once；先複製到
00_inbox，再只處理副本。不要刪除原始資料。失敗時不要回報成功。
```

---

## 第二十部分：安裝完成檢查表

逐項確認：

- [ ] `git --version` 有版本號
- [ ] `gh --version` 有版本號
- [ ] `py -3.13 --version` 有版本號
- [ ] `gh auth status` 顯示正確 GitHub 帳號
- [ ] `C:\AI\local-knowledge-compiler` 存在
- [ ] `.venv\Scripts\kb.exe --help` 能執行
- [ ] `C:\KnowledgeBase` 已初始化
- [ ] `kb lint` 顯示 `"healthy": true`
- [ ] 原始 Excel 仍在原本位置
- [ ] 只把 Excel 副本放進 `00_inbox`
- [ ] 已選對 `personal`／`work`／`shared`／`unclassified`
- [ ] 第一次 `ingest-once` 已完成或有可追蹤的 `job_id`
- [ ] 第一次 `prepare` 已產生證據包
- [ ] AI 回答包含來源、衝突、信心與未知事項

完成以上項目，你的第一座本地知識庫就可以開始使用。

---

## 延伸資料

- [所有 CLI 指令參考](CLI_REFERENCE.zh-TW.md)
- [專案 README](../README.md)
- [完整 AI 交接文件](../AI_HANDOFF.md)
- [系統設計原理](superpowers/specs/2026-07-25-local-knowledge-iteration-system-design.md)
