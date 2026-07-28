# Codex／Claude Code 零基礎安裝與使用指南

這份指南寫給完全不懂技術的人。你只需要打開 Codex 或 Claude Code、貼上白話提示詞，
剩下的檢查、安裝和指令都交給 AI。若提示詞寫了「不要叫我自己輸入」，AI 就應直接做，
只有登入、公司管理員權限或隱私決定才請你接手。

## 先理解兩個資料夾

```text
local-knowledge-compiler/
├── 公開工具與說明，可以更新或上傳 GitHub
└── KnowledgeBase/
    └── 你的 Excel、原始資料、索引、回答和 Wiki，只留在本機
```

`KnowledgeBase/` 是本機私人資料夾，永遠不會跟著上傳 GitHub。

位置由你把 Repo 放在哪裡決定：

```text
Repo 放在 C 槽 → 知識庫也在同一個 C 槽專案
Repo 放在 D 槽 → 知識庫也在同一個 D 槽專案
Repo 放在 OneDrive → 仍可使用；OneDrive 只警告、不阻擋
```

OneDrive 的提醒是要你避免同時在兩台電腦修改同一份資料。它不會阻止初始化，也不會
把資料自動公開到 GitHub。

## 第一部分：取得專案

### 方法 A：請 AI Clone（建議）

在 Codex 或 Claude Code 貼：

```text
請幫我 Clone 這個 Repo：
https://github.com/Jason5330/local-knowledge-compiler

請放在我目前選擇的專案位置，資料夾名稱保持 local-knowledge-compiler。
完成後打開這個專案。不要叫我自己輸入終端機指令。
```

Clone 通常會得到乾淨的 `local-knowledge-compiler` 名稱。

### 方法 B：Download ZIP（公司環境的備用方式）

在 GitHub 頁面按「Code」→「Download ZIP」→ 解壓縮。資料夾可能叫
`local-knowledge-compiler-master`。再對 AI 說：

```text
我用 Download ZIP 取得專案。
請把 local-knowledge-compiler-master 安全改名為 local-knowledge-compiler，
確認內容完整後打開專案。不要刪除其他資料夾。
```

`-master` 只是 ZIP 命名方式，不是系統故障。

## 第二部分：用 Codex 首次安裝

先用 Codex 打開 `local-knowledge-compiler` 專案，再貼：

```text
初始化本專案知識庫。

請你直接檢查需要的環境、安裝本專案，並在目前專案建立 KnowledgeBase。
KnowledgeBase/ 是本機私人資料夾，不得加入 Git，也不得上傳 GitHub。
若專案在 OneDrive，只警告我風險，不要阻擋。
完成後執行 status 和 lint，再用最大白話告訴我結果。
不要叫我自己輸入任何技術指令。
```

成功後，你會看到：

```text
local-knowledge-compiler/KnowledgeBase/
├── 00_inbox     ← 你平常放新資料副本的入口
├── 10_raw       ← 系統保存的來源版本
├── 20_wiki      ← AI 整理後的知識頁
├── 30_answers   ← 你同意保存的回答
├── 40_index     ← 搜尋索引
├── 50_corrections ← 回答錯誤的修正記憶
└── 80_system    ← 規則與狀態
```

## 第三部分：用 Claude Code 首次安裝

先用 Claude Code 打開 `local-knowledge-compiler` 專案，再貼：

```text
初始化本專案知識庫。

請你直接檢查需要的環境、安裝本專案，並在目前專案建立 KnowledgeBase。
KnowledgeBase/ 是本機私人資料夾，不得加入 Git，也不得上傳 GitHub。
若專案在 OneDrive，只警告我風險，不要阻擋。
完成後執行 status 和 lint，再用最大白話告訴我結果。
不要叫我自己輸入任何技術指令。
```

Codex 和 Claude Code 使用同一個 `KnowledgeBase/`、同一套證據規則，不必建立兩份。

## 第四部分：匯入 Excel 或其他資料

最簡單的方法是把資料「複製一份」到：

```text
KnowledgeBase/00_inbox/
```

然後說：

```text
把新資料整理進我的知識庫。

請只處理 KnowledgeBase/00_inbox 裡的新副本，保留原始檔，完成後檢查
status 和 lint，告訴我哪些資料已可搜尋、哪些仍需要處理。
```

如果原檔在別處，也可以貼完整路徑：

```text
請把這份 Excel 安全整理進我的知識庫：
【貼上 Excel 的完整路徑】

原始 Excel 永遠不能被搬走、改名、覆蓋或刪除。
請先建立不衝突的副本放到 KnowledgeBase/00_inbox，
只把 00_inbox 裡的副本交給 ingest-once。
完成後確認原檔仍在，再檢查 status 和 lint。
```

支援的 Excel 內容會被拆成可搜尋片段。公式、合併儲存格或複雜圖表可能需要額外確認；
AI 不得假裝讀懂無法擷取的內容。

## 第五部分：向知識庫提問

直接說：

```text
用我的知識庫回答這個問題：
【把問題寫在這裡】

請先找出最相關的本地證據，只依證據回答，列出來源、資料衝突、時效與信心。
證據不足就說目前資料無法判定，不要猜，也不要自行搜尋網路。
```

系統流程：

```text
問題
→ prepare 找出相關證據
→ AI 只看證據回答
→ 你決定是否保存
```

回答不會因為出現在對話中就自動記錄。想保存時，再說：

```text
保存這次回答。

請用剛才的證據與引用完成 finalize，確認回答已保存並排入下一輪知識整理，
最後執行 status 和 lint。
```

這樣高品質回答才會進入 `30_answers`，並在後續整理時更新 `20_wiki`。

## 第六部分：回答錯了怎麼辦

你不用改程式，也不用自己找修正檔。直接說：

```text
這個回答錯了。
請重新核對剛才使用的 Excel 原始證據；有證據才建立修正，完成後重新回答。
```

AI 會做：

```text
找回剛才的問題證據包
→ 核對 Excel 的工作表、欄位、數值與單位
→ 確認真的是錯誤
→ 把「以後遇到什麼情況要怎麼讀」存到 50_corrections
→ 重新 prepare
→ 重新回答並說明用了哪一條修正
```

最重要的安全順序是：

```text
原始 Excel 證據 ＞ 修正紀錄 ＞ AI 自己的猜測
```

也就是說，修正紀錄只是一本「以前踩過哪些坑」的小冊子，不能取代 Excel。若新版
Excel 已改欄位或單位，舊修正不能硬套。

你可能看到四種修正狀態：

- `active`：有效，下次相似提問會自動提醒 AI。
- `stale`：新版資料結構變了，需要重新確認。
- `suspended`：暫停使用，可能遇到例外或由你要求停用。
- `retired`：永久退役，但歷史仍保留，方便追查。

管理時只要用白話說：

```text
列出目前所有修正紀錄
暫停這條修正：【修正編號】
重新啟用這條修正：【修正編號】
永久退役這條修正：【修正編號】
檢查修正系統是否正常
```

AI 會先顯示要處理的規則及目前版本，再安全執行。不要直接手改
`KnowledgeBase/50_corrections/` 裡的 JSON 檔。

## 第七部分：健康檢查與排除卡住

平常說：

```text
檢查知識庫是否正常。

請執行 status 和 lint，不要先修改任何資料。
用「正常／要注意／需要我決定」三類回報。
```

看到 `pending_attention` 時說：

```text
繼續處理知識庫中卡住的工作。

請先讀 status 和 handoff，說明原因，再安全 resume。
不得重複匯入、跳過引用驗證或假裝已完成。最後再執行 status 和 lint。
```

看到 `pending_extractor` 代表檔案已保存，但目前缺少適合的讀取器，常見於掃描 PDF、
圖片、音訊或影片。

## 第八部分：換電腦或換 AI

在新電腦先取得同一個公開 Repo。私人 `KnowledgeBase/` 不會從 GitHub 跟過去，需要用
你公司允許的安全方式單獨搬移或由 OneDrive 同步。移動前先關閉另一台電腦上的處理程序，
避免兩台同時寫入。

新 AI 打開含有 `KnowledgeBase/` 的專案後，貼：

```text
請接手這個本地知識庫。

先讀 AI_HANDOFF.md、README.md、docs/BEGINNER_GUIDE.zh-TW.md、
docs/CLI_REFERENCE.zh-TW.md，以及
KnowledgeBase/80_system/KNOWLEDGE_PROTOCOL.md。
不要重新初始化、不要覆蓋資料。先執行 status 和 lint，再告訴我目前狀態。
```

## 第九部分：隱私與安全

- `KnowledgeBase/` 不進 GitHub，但 Codex／Claude 可能是雲端模型。
- 機密、個資、密碼、醫療與財務資料要遵守公司政策。
- 不要把密碼、API key 或 GitHub 權杖貼進對話。
- AI 不得刪除唯一原檔，不得未經同意開啟排程、常駐或對外分享。
- AI 說成功前，必須實際檢查 status、lint 與需要的輸出。

## 最短版

```text
第一次：初始化本專案知識庫
有新資料：把新資料整理進我的知識庫
要查資料：用我的知識庫回答這個問題
要留下回答：保存這次回答
定期檢查：檢查知識庫是否正常
出現卡住：繼續處理知識庫中卡住的工作
```
