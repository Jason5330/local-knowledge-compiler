# 給 Codex／Claude Code 的內部執行參考

這份文件是給 AI 代理讀的。一般使用者不需要輸入以下指令。

預設位置：

```text
repository = C:\AI\local-knowledge-compiler
vault = C:\KnowledgeBase
python = C:\AI\local-knowledge-compiler\.venv\Scripts\python.exe
kb = C:\AI\local-knowledge-compiler\.venv\Scripts\kb.exe
```

## AI 執行原則

1. 先讀 `README.md`、`AI_HANDOFF.md`、本文件與 vault protocol。
2. 在執行寫入前解析絕對路徑，確認目標在使用者指定範圍。
3. 不得將唯一一份來源直接傳給 `ingest-once`。
4. 先複製到 `00_inbox`，使用不衝突的檔名，再只處理副本。
5. 對既有 vault 不得重跑具破壞性的初始化。
6. 未經同意不得設定啟動項目、排程、常駐監看或外部分享。
7. `provider=claude` 前必須實測 Claude CLI；否則使用 `manual`。
8. Exit code 2 是可恢復的人工作業狀態，不可當成資料遺失。
9. 完成前執行 `status` 與 `lint`，以實際輸出作為證據。

以下使用單行形式，讓不同代理可以依自己的 shell 安全執行。不要把這些內容原樣丟給
初學者。

## 建立隔離環境

```text
py -3.13 -m venv C:\AI\local-knowledge-compiler\.venv
C:\AI\local-knowledge-compiler\.venv\Scripts\python.exe -m pip install -e "C:\AI\local-knowledge-compiler[dev]"
```

驗證：

```text
C:\AI\local-knowledge-compiler\.venv\Scripts\kb.exe --help
```

## `kb init`

用途：第一次建立 vault。只有確認目標不存在或是空白新位置時才執行。

```text
C:\AI\local-knowledge-compiler\.venv\Scripts\kb.exe init C:\KnowledgeBase
```

初始化後檢查：

```text
C:\KnowledgeBase\00_inbox
C:\KnowledgeBase\10_raw
C:\KnowledgeBase\20_wiki
C:\KnowledgeBase\80_system
C:\KnowledgeBase\80_system\KNOWLEDGE_PROTOCOL.md
```

## `kb ingest-once`

用途：處理一個已複製到 inbox 的檔案。

```text
C:\AI\local-knowledge-compiler\.venv\Scripts\kb.exe ingest-once C:\KnowledgeBase C:\KnowledgeBase\00_inbox\safe-copy.xlsx --space work
```

可用 space：

```text
personal
work
shared
unclassified
```

Windows v1 暫時不要使用 `project:<slug>`。專案資料先歸 `work`，把專案名保留在檔名
或內容中。

安全前置程序：

```text
解析原始檔絕對路徑
→ 記錄原檔大小、修改時間與雜湊（需要時）
→ 產生不衝突的 inbox 副本名稱
→ 複製
→ 確認原檔仍存在
→ ingest-once 只接收副本
→ 檢查 raw、index、status、lint
```

同磁碟 ingest 可能以 move claim 副本，這是為什麼絕不能直接傳入使用者唯一原檔。

## `kb prepare`

用途：根據問題建立證據包。

```text
C:\AI\local-knowledge-compiler\.venv\Scripts\kb.exe prepare "使用者問題" --vault C:\KnowledgeBase --space work --output C:\KnowledgeBase\.kb\last-packet.json
```

代理必須讀取 packet，只根據 packet 回答。引用欄位必須逐字使用 packet 的值：

```json
{
  "conclusion": "根據證據得到的結論",
  "citations": [
    {
      "source_id": "原樣複製",
      "version_id": "原樣複製",
      "locator": "原樣複製",
      "evidence_sha256": "原樣複製"
    }
  ],
  "confidence": "high",
  "conflicts": "沒有發現衝突。"
}
```

證據不足時：

```json
{
  "conclusion": "目前資料無法判定。",
  "citations": [],
  "confidence": "low",
  "conflicts": "證據不足。"
}
```

## `kb finalize`

用途：驗證答案與引用，保存回答，排入知識整理。

```text
C:\AI\local-knowledge-compiler\.venv\Scripts\kb.exe finalize --vault C:\KnowledgeBase --packet C:\KnowledgeBase\.kb\last-packet.json --answer C:\KnowledgeBase\.kb\answer.json
```

finalize 後：

```text
status → 確認衍生工作狀態
lint → 確認知識庫完整性
```

衍生答案只能整理進 `20_wiki`，不得冒充新的原始來源寫入 `10_raw`。

## `kb status`

用途：只讀查看工作狀態。

```text
C:\AI\local-knowledge-compiler\.venv\Scripts\kb.exe status --vault C:\KnowledgeBase
```

重點欄位：

- `job_id`
- `job_type`
- `state`
- `error`
- `handoff_path`
- `source_id`
- `version_id`

## `kb resume`

用途：在修正問題或完成人工 handoff 後，繼續指定工作。

```text
C:\AI\local-knowledge-compiler\.venv\Scripts\kb.exe resume --vault C:\KnowledgeBase --job-id ACTUAL_JOB_ID
```

不要盲目重試。先讀 status、錯誤與 handoff，確認前置條件已處理。

## `kb lint`

用途：檢查 vault 結構、索引、引用與一致性。

```text
C:\AI\local-knowledge-compiler\.venv\Scripts\kb.exe lint --vault C:\KnowledgeBase
```

代理回報時不要只說「跑過」。要清楚說通過、失敗或警告數量，並列出使用者能理解的
影響。

## `kb rebuild`

用途：從既有原始資料重建衍生索引。這不是一般故障排除的第一步。

```text
C:\AI\local-knowledge-compiler\.venv\Scripts\kb.exe rebuild --vault C:\KnowledgeBase
```

執行前：

1. 說明重建範圍與預期影響。
2. 確認 `10_raw` 與必要索引仍完整。
3. 取得使用者明確同意。
4. 完成後執行 `status`、`lint` 與抽樣查詢。

## `kb watch`

用途：持續監看 inbox。

```text
C:\AI\local-knowledge-compiler\.venv\Scripts\kb.exe watch C:\KnowledgeBase
```

注意：

- 這是長時間執行程序。
- 代理工作階段關閉後可能停止。
- 不等於 Windows 服務。
- 不得未經同意設定自動啟動。
- manual provider 可能產生 `pending_attention`，需由 AI 接手 handoff。

## Exit code

- `0`：成功，或目前無待處理項目。
- `1`：錯誤；保留輸出並調查原因。
- `2`：等待人工處理；讀取 handoff，完成後用 `resume`。

## Provider 選擇

### Codex

Codex Desktop 無可呼叫的背景 CLI 時：

```toml
[compiler]
provider = "manual"
```

Codex 仍可執行匯入、prepare、回答、finalize、status、resume 與 lint。

### Claude Code

只有在 `claude` 命令實測成功且已登入後：

```toml
[compiler]
provider = "claude"
```

若 Claude CLI 呼叫失敗，切回 manual 或保留 `pending_attention`，不可宣稱 Wiki 已更新。

## 代理完成回報模板

```text
結果：成功／部分完成／未完成
原始資料：是否保持不動
保存位置：raw 或版本位置
可搜尋狀態：是／否，證據是什麼
待處理：pending_attention／pending_extractor／無
檢查：status 結果、lint 結果
下一步：一句最簡單的建議
```
