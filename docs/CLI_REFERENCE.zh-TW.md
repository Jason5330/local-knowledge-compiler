# 給 Codex／Claude Code 的內部執行參考

這份文件是給 AI 代理讀的。一般使用者不需要輸入以下指令。代理應自行找出目前專案路徑
與虛擬環境，不得假設固定在 C 槽，也不要把命令原樣丟給初學者。

## 基本位置

```text
project = 目前開啟的 local-knowledge-compiler Repo 根目錄
vault = <project>/KnowledgeBase
kb = <project>/.venv/Scripts/kb.exe（Windows 虛擬環境）
```

Repo 在 C 槽，Vault 就在該 C 槽專案；Repo 在 D 槽，Vault 就在該 D 槽專案。

## 初始化

專案內預設初始化：

```text
kb init
→ 建立 <cwd>/KnowledgeBase
→ 檢查 /KnowledgeBase/ Git ignore
→ 設定此 Repo 的 .githooks
```

指定其他位置的舊用法仍可使用：

```text
kb init <path>
```

指定路徑初始化不會安裝「目前專案」的 Git hooks。

初始化後應確認：

```text
KnowledgeBase/00_inbox
KnowledgeBase/10_raw
KnowledgeBase/20_wiki
KnowledgeBase/80_system/KNOWLEDGE_PROTOCOL.md
```

## Vault 自動尋找順序

對 `prepare`、`finalize`、`status`、`resume`、`lint`、`rebuild`：

```text
1. 使用明確傳入的 --vault
2. 若 cwd 本身是已初始化 Vault，使用 cwd
3. 若 cwd/KnowledgeBase 是已初始化 Vault，使用 cwd/KnowledgeBase
4. 都找不到時，回傳可操作錯誤，提示初始化或指定 --vault
```

`watch` 與 `ingest-once` 保留明確 Vault 位置，避免長時間或寫入作業選錯資料庫。

## OneDrive A2 模式

若 Vault 位於 `OneDrive`、`OneDriveConsumer` 或 `OneDriveCommercial` 根目錄內：

- 警告只寫到 stderr，避免破壞 stdout 的 JSON 或可供程式讀取的輸出。
- OneDrive 只警告，不阻擋命令。
- 提醒避免兩台電腦同時寫入同一 Vault。

## Git 本機資料保護

公開 Repo 必須包含：

```text
.gitignore              → /KnowledgeBase/
.githooks/pre-commit    → 檢查 staged paths
.githooks/pre-push      → 檢查 tracked paths
scripts/check-local-data.py
```

`kb init` 在 Git Repo 根目錄執行時，會把本 Repo 的 `core.hooksPath` 設成 `.githooks`。
若無法確認 ignore 或 hooks，初始化本身可能已完成，但命令回傳 Exit code `2`，代理必須先
修復發布保護。Download ZIP 沒有 `.git` 時跳過 hooks，`KnowledgeBase/` 仍保持本機。

## 安裝開發環境

代理依目前 Python 執行環境建立 `.venv`，再執行等價於：

```text
python -m venv <project>/.venv
<project>/.venv/Scripts/python.exe -m pip install -e "<project>[dev]"
<project>/.venv/Scripts/kb.exe --help
```

不要要求初學者自行輸入。公司政策阻擋安裝時，清楚說明缺少項目和管理員需要做什麼。

## 安全匯入

原始檔不得直接傳給 `ingest-once`：

```text
解析原始檔
→ 記錄大小、時間與必要雜湊
→ 建立 KnowledgeBase/00_inbox 不衝突副本
→ 確認原檔仍存在
→ ingest-once 只處理副本
→ 檢查 raw、index、status、lint
```

語法：

```text
kb.exe ingest-once <vault> <vault>/00_inbox/safe-copy.xlsx --space work
```

可用 space：`personal`、`work`、`shared`、`unclassified`。

## 建立問題證據包

```text
kb.exe prepare "使用者問題" --vault <vault> --space work --output <vault>/.kb/last-packet.json
```

若在專案根目錄，可省略 `--vault`。代理只能根據 packet 回答。重要結論的
`source_id`、`version_id`、`locator`、`evidence_sha256` 必須原樣引用。

證據不足時明確回答「目前資料無法判定」，不得猜測或自行搜尋網路。

## 保存回答

只有使用者要求保存時執行：

```text
kb.exe finalize --vault <vault> --packet <vault>/.kb/last-packet.json --answer <vault>/.kb/answer.json
```

若在專案根目錄，可省略 `--vault`。finalize 驗證引用、保存答案並建立後續 Wiki 整理
工作；衍生答案不得冒充新的原始來源。

## 狀態、恢復與檢查

完整顯式語法：

```text
kb.exe status --vault <vault>
kb.exe resume --vault <vault> --job-id ACTUAL_JOB_ID
kb.exe lint --vault <vault>
kb.exe rebuild --vault <vault>
```

在專案根目錄可省略 `--vault`：

```text
kb.exe status
kb.exe lint
```

`resume` 前先讀 status、錯誤與 handoff，不得盲目重試。`rebuild` 不是一般故障排除的
第一步，執行前要說明影響並取得同意。

## 背景監看

```text
kb.exe watch <vault>
```

這是長時間執行程序，不等於 Windows 服務。未經使用者同意，不得設定排程、
`shell:startup` 或開機啟動。

## Exit code

- Exit code `0`：成功，或目前無待處理項目。
- Exit code `1`：錯誤；保留輸出並調查。
- Exit code `2`：`pending_attention` 或可恢復的人工作業／保護提醒；讀取輸出與 handoff。

## Provider

- Codex Desktop 無背景 CLI 時使用 `manual`，由目前代理接手 handoff。
- Claude CLI 只有在命令存在、已登入且實際呼叫成功後使用 `claude`。
- provider 失敗時保留 `pending_attention`，不可宣稱 Wiki 已更新。

## 完成回報

```text
結果：成功／部分完成／未完成
原始資料：是否保持不動
保存位置：raw 或版本位置
可搜尋：是／否，以及驗證證據
待處理：pending_attention／pending_extractor／無
檢查：status 與 lint
下一步：一句最簡單的建議
```
