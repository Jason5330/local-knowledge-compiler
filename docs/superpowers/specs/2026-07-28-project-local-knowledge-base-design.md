# 專案內本機知識庫與 OneDrive 相容設計

日期：2026-07-28
狀態：已完成對話設計確認，等待使用者審閱書面規格
適用專案：Local Knowledge Compiler

## 1. 目標

讓完全不懂技術的使用者完成以下流程：

```text
取得 GitHub 專案
→ 在專案資料夾開啟 Codex 或 Claude Code
→ 對 AI 說「初始化本專案知識庫」
→ 系統在目前專案內建立 KnowledgeBase/
→ 使用者把資料放進 KnowledgeBase/00_inbox/
→ 對 AI 說「整理知識庫裡的新資料」
→ AI 完成收錄、整理、檢索與健康檢查
```

知識庫使用專案所在磁碟，不再固定使用 `C:\KnowledgeBase`：

```text
C:\Projects\local-knowledge-compiler
→ C:\Projects\local-knowledge-compiler\KnowledgeBase

D:\Projects\local-knowledge-compiler
→ D:\Projects\local-knowledge-compiler\KnowledgeBase
```

OneDrive 可以正常安裝及運作，但本模式的知識庫定位為單機本地資料，不以 OneDrive
進行跨電腦同步。

## 2. 不在本次範圍

- 不實作多台電腦共同寫入同一座知識庫。
- 不使用 OneDrive 同步工作佇列、SQLite、鎖檔或暫存檔。
- 不自動移動既有知識庫。
- 不自動修改、停止或重新設定 OneDrive。
- 不建立圖形化安裝程式。
- 不要求 Codex 與 Claude Code 支援相同的斜線指令。

## 3. 專案與知識庫配置

初始化完成後：

```text
local-knowledge-compiler/
├── .git/
├── .gitignore
├── AGENTS.md
├── CLAUDE.md
├── README.md
├── src/
├── docs/
└── KnowledgeBase/                    # 本機私有，永不納入公開 Repo
    ├── 00_inbox/                     # 使用者只需把新資料放到這裡
    ├── 10_raw/                       # 不可變的原始證據與歷史版本
    ├── 20_wiki/                      # AI 整理的知識
    ├── 30_answers/                   # 保存的重要問答
    ├── 40_index/                     # 本機可重建搜尋索引
    ├── 80_system/                    # 共用規則與設定
    ├── 90_logs/                      # 本機事件與查詢紀錄
    ├── 99_trash/                     # 可復原回收區
    ├── .kb/                          # 本機佇列、鎖與暫存
    ├── AGENTS.md                     # Vault 內 Codex 入口
    └── CLAUDE.md                     # Vault 內 Claude 入口
```

專案根目錄的 `AGENTS.md` 與 `CLAUDE.md` 是公開、通用的操作入口，不包含用戶資料。
它們要求代理在 `KnowledgeBase/` 存在時讀取其中的正式 protocol。

## 4. 取得 Repo 與資料夾名稱

### 4.1 主要方式：由 AI Clone

使用者先在準備存放專案的父資料夾開啟 Codex 或 Claude Code，再貼安裝提示詞。AI
把 Repo Clone 到明確名稱：

```text
local-knowledge-compiler
```

因此不出現 `-master`。

### 4.2 備用方式：Download ZIP

GitHub 的分支 ZIP 可能解壓成 `local-knowledge-compiler-master`。AI 在確認目標名稱
沒有衝突後，協助改名為：

```text
local-knowledge-compiler
```

若資料夾已存在，不覆蓋、不合併，先向使用者回報。

### 4.3 不為名稱改預設分支

把 `master` 改成 `main` 只會把 ZIP 後綴改為 `-main`，不能消除後綴。因此本功能不
為了資料夾名稱更改 Git 分支。

## 5. 初始化行為

### 5.1 命令相容性

保留既有明確路徑：

```text
kb init <自訂路徑>
```

新增無路徑預設：

```text
kb init
```

在專案根目錄執行 `kb init` 時，目標固定為：

```text
<目前專案資料夾>\KnowledgeBase
```

初始化必須可重複執行。已存在的設定、原始資料、Wiki、答案及索引不得被覆蓋。

### 5.2 Vault 自動發現

後續命令的 Vault 選擇順序：

1. 使用者或 AI 明確提供 `--vault` 時，使用該路徑。
2. 目前資料夾本身是已初始化 Vault 時，使用目前資料夾。
3. 目前資料夾下有已初始化的 `KnowledgeBase/` 時，使用該資料夾。
4. 都找不到時停止，用白話提示先初始化；不得猜測其他資料夾。

這讓代理在專案根目錄執行 `prepare`、`finalize`、`status`、`resume`、`lint` 與
`rebuild` 時，不必反覆輸入 Vault 絕對路徑。

### 5.3 完成回報

初始化後，AI 用白話回報：

- 專案位置。
- 知識庫位置。
- 知識庫位於 C 槽或 D 槽。
- 是否位於 OneDrive。
- `KnowledgeBase/` 是否已被 Git 排除。
- 下一步把資料放入哪個資料夾。

## 6. Codex／Claude Code 共用的白話口令

不依賴產品專屬斜線命令，改用兩者都能理解的自然語言：

### 初始化

```text
初始化本專案知識庫
```

代理執行環境檢查、安裝及 `kb init`，再執行 `status` 與 `lint`。

### 整理新資料

```text
整理知識庫裡的新資料
```

代理只處理 `KnowledgeBase/00_inbox/` 內的新資料。使用者在外部提供唯一原檔時，
代理仍須先複製到 inbox，再只處理副本。

### 提問

```text
使用知識庫回答：<問題>
```

代理先 `prepare`，只依證據回答；預設保存答案並 `finalize`。使用者說「只查詢、不
保存」時不得執行 finalize。

### 健康檢查

```text
檢查知識庫是否正常
```

代理執行 `status` 與 `lint`，用「正常／要注意／需要我決定」分類回報。

### 繼續未完成工作

```text
繼續處理知識庫中卡住的工作
```

代理先讀狀態與 handoff，再執行安全恢復，不得盲目重複匯入。

## 7. 用戶資料永不進入公開 Repo

### 7.1 Git 排除

公開 Repo 的根 `.gitignore` 必須包含精確規則：

```text
/KnowledgeBase/
```

此規則只排除專案根目錄的本機 Vault，不會誤排除程式測試中的其他同名資料。

### 7.2 初始化檢查

若目前專案是 Git Clone，初始化後必須驗證 `KnowledgeBase/` 確實被 Git 忽略。
驗證失敗時：

- 不刪除或移動 Vault。
- 清楚警告資料保護未完成。
- 不執行任何 Git 提交或推送。
- 指示 AI 先修復排除規則再繼續。

### 7.3 本機 Git 防護

Clone 模式初始化時安裝 Repo 專用的資料防護檢查。提交或推送若包含下列路徑即拒絕：

```text
KnowledgeBase/
```

防護腳本本身可以公開，內容不得包含任何使用者路徑或資料。

### 7.4 AI 規則

根 `AGENTS.md` 與 `CLAUDE.md` 明確規定：

- 禁止讀取用戶資料後把內容複製進公開文件、Issue、PR 或 commit。
- 禁止 stage、commit、push `KnowledgeBase/`。
- Git 操作前檢查 staged paths。
- 若偵測到資料已被追蹤，立即停止推送並回報。

### 7.5 保證邊界

上述是多層防誤操作機制，能阻止一般 `git add`、代理提交與正常 push。蓄意使用
`git add -f`、停用 hooks、`--no-verify` 或手動上傳仍可能繞過，因此文件不得宣稱
技術上「任何情況都絕不可能上傳」。對初學者的正常流程，系統必須預設安全。

## 8. OneDrive 相容模式 A2

### 8.1 預期模式

Repo 與 `KnowledgeBase/` 可以位於任何本機磁碟。推薦放在非 OneDrive 的一般本機
資料夾。

若使用者在看見警告後仍選擇 OneDrive 路徑，OneDrive 本身可能同步其中的資料；
系統只能提醒，不能再宣稱資料保證只留在單一電腦。GitHub 排除與 OneDrive 同步是
兩件不同的事：Vault 仍不得進入 Git，但可能被 OneDrive 同步。

### 8.2 偵測

Windows 上以現有 OneDrive 環境位置辨認：

- 個人 OneDrive。
- 公司／學校 OneDrive。
- 位於上述根目錄下的 Desktop、Documents 或其他資料夾。

偵測是唯讀、best-effort。環境未提供 OneDrive 位置時不猜測、不修改系統設定。

### 8.3 A2 警告

Vault 位於 OneDrive 內時，在 stderr 顯示：

```text
提醒：知識庫位於 OneDrive 內，可能會同步到其他裝置。
這次操作仍會繼續。
如果你希望資料只留在本機，請把整個專案放到 OneDrive 以外的資料夾。
```

警告不得：

- 改變成功或失敗狀態。
- 污染 `status` 的 JSON stdout。
- 阻止初始化、匯入、查詢或健康檢查。
- 自動搬移 Vault。
- 自動改變 OneDrive Files On-Demand 設定。

每次主要命令都可重現提醒，`lint` 也會顯示；不另外寫入大量重複錯誤紀錄。

## 9. 資料處理流程

```text
使用者把資料放入 KnowledgeBase/00_inbox
→ AI 確認檔案已複製完成
→ 計算指紋與版本關係
→ 原始版本保存到 10_raw
→ 抽取可搜尋內容
→ 更新 40_index
→ 編譯或建立 manual handoff
→ 驗證引用與 Wiki
→ 發布到 20_wiki
→ status + lint
```

若使用者直接提供外部 Excel 路徑：

```text
保留外部原檔
→ 產生不重名副本到 00_inbox
→ ingest-once 只接收副本
```

## 10. 錯誤處理

- 專案路徑不存在：停止並指出應開啟哪個資料夾。
- `KnowledgeBase/` 已存在：保留內容並驗證，不重新覆蓋。
- 同名 Repo 資料夾已存在：不覆蓋，請使用者選擇現有專案或另一名稱。
- OneDrive 偵測到：警告但繼續。
- OneDrive 無法辨認：使用目前專案路徑，不猜測。
- Git ignore 失效：停止 Git 發布，不停止本機知識庫使用。
- Git 不存在或使用 ZIP：略過 Git hook，仍建立 `.gitignore` 規則與 AI 禁止上傳規則。
- D 槽不存在：不需特別處理，因為 Vault 跟隨目前 Repo 所在磁碟。
- 編譯器不可用：保留 `pending_attention`，原始資料與索引仍須分別驗證。

## 11. 文件調整

README 與零基礎指南的主要流程改為：

```text
選擇父資料夾
→ 開啟 Codex／Claude Code
→ 貼「下載並初始化」提示詞
→ AI 建立 local-knowledge-compiler/KnowledgeBase
→ 把資料放入 00_inbox
→ 對 AI 說「整理知識庫裡的新資料」
```

文件同時保留：

- AI Clone 主流程。
- Download ZIP 改名備用流程。
- OneDrive A2 警告。
- C／D 槽由 Repo 位置決定。
- `KnowledgeBase/` 本機私有邊界。
- 更新程式前先保護 Vault 的說明。

## 12. 測試與驗收

### 12.1 初始化

- `kb init` 在 cwd 建立 `KnowledgeBase/`。
- `kb init <path>` 保持既有行為。
- 重複初始化不覆蓋使用者檔案。
- Repo 在 C／D 型路徑時，Vault 跟隨該路徑。

### 12.2 Vault 發現

- 在專案根目錄可找到 `KnowledgeBase/`。
- 在 Vault 根目錄可找到自身。
- 明確 `--vault` 優先。
- 無合法 Vault 時清楚失敗，不搜尋不相關位置。

### 12.3 OneDrive

- 個人版 OneDrive 內顯示警告。
- 公司／學校版 OneDrive 內顯示警告。
- OneDrive 外不誤報。
- 大小寫與相似前綴不誤判。
- 警告不改變命令 exit code 或 JSON stdout。

### 12.4 Git 資料保護

- `/KnowledgeBase/` 受到 `.gitignore` 保護。
- 一般 stage 不會加入 Vault。
- 防護檢查拒絕已追蹤或 staged 的 Vault 路徑。
- 防護不會 stage、修改或刪除那兩張既有本機圖片或其他無關檔案。
- ZIP／非 Git 模式仍能初始化及使用。

### 12.5 回歸

- 原有 init、ingest、watch、prepare、finalize、status、resume、lint、rebuild 測試通過。
- 真實 Excel 安全副本流程仍保持。
- Codex manual provider 與 Claude provider 行為不變。

## 13. 完成定義

功能完成必須同時滿足：

1. 使用者可在 Repo 根目錄用一句白話要求完成初始化。
2. Vault 預設建立於 `<repo>\KnowledgeBase`。
3. Repo 位於 C 或 D 槽都不需改設定。
4. OneDrive 內只警告、不阻止。
5. `KnowledgeBase/` 預設不會被 Git 收錄。
6. Codex 與 Claude Code 使用同一份 Vault 與 protocol。
7. 使用者只需把資料放入 `00_inbox`，再要求 AI 整理。
8. 文件、精準測試與完整回歸測試通過。
