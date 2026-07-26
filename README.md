# 本地知識迭代系統

這套工具把一般資料夾變成 Codex 與 Claude 都能使用的本地知識庫。原始檔永久保留，
AI 只從本機索引整理證據；你不需要 Obsidian，也不需要理解程式架構。

## 初次安裝

在 PowerShell 進入本專案資料夾，依序執行：

```powershell
py -3.13 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e ".[dev]"
.\.venv\Scripts\kb.exe init "C:\你的資料夾\我的知識庫"
.\scripts\start-kb.ps1 -Vault "C:\你的資料夾\我的知識庫"
```

最後一行會在畫面上持續監看；錯誤不會被隱藏。若要每次登入 Windows 後自動執行，
可以按 `Win + R`、輸入 `shell:startup`，再手動放入這支 PowerShell 腳本的捷徑。
程式本身不會擅自修改 Windows 啟動項目。

## 每天怎麼用

流程很單純：

```text
新資料 → 放進 00_inbox → 監看器整理並保存原始版本
提問 → Codex 或 Claude 先跑 kb prepare → 只依證據回答並引用
回答完成 → kb finalize → 保存答案並排入下一輪知識整理
檢查 → kb lint
回看／復原 Wiki 變更 → git log → git revert <commit>
```

手動準備證據包：

```powershell
.\.venv\Scripts\kb.exe prepare "團隊最後選了哪個方案？" `
  --vault "C:\你的資料夾\我的知識庫" --space work `
  --output .kb\last-packet.json
```

AI 應把結論、信心、衝突與「結構化引用」寫入答案 JSON，然後執行：

```json
{
  "conclusion": "團隊選擇 B 方案。",
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

引用欄位不可自己改寫。若證據不足，結論要明說無法判定、`citations` 使用空陣列，
`confidence` 使用 `low`。

```powershell
.\.venv\Scripts\kb.exe finalize `
  --vault "C:\你的資料夾\我的知識庫" `
  --packet "C:\你的資料夾\我的知識庫\.kb\last-packet.json" `
  --answer "C:\你的資料夾\我的知識庫\.kb\answer.json"
.\.venv\Scripts\kb.exe lint --vault "C:\你的資料夾\我的知識庫"
```

## 你需要知道的限制

- Codex 與 Claude 都是雲端模型。本機保存資料，不代表送給 AI 的內容仍然離線；
  證據包可能會提交給你正在使用的模型服務，請先遵守公司與個人的資料規範。
- 裸網址只會當成書籤文字，系統絕不抓取網頁，也不會自動搜尋網路。
- 圖片、掃描 PDF、音訊與影片目前沒有文字擷取器時，會標記
  `pending_extractor`；檔案仍會安全保存，但不能冒充已讀取內容。
- 背景自動編譯目前使用 Claude CLI。原因是這台電腦安裝的 Codex Desktop
  不能當成背景 CLI 呼叫；Codex 仍可透過同一份 `AGENTS.md`、`kb prepare` 與
  `kb finalize` 完整使用知識庫。
- Claude CLI 是背景編譯選項，不代表所有資料都留在本機；其雲端資料處理規則仍
  取決於你的 Claude 帳戶與服務設定。

Codex 與 Claude 的入口檔都只指向
`80_system/KNOWLEDGE_PROTOCOL.md`。所以兩邊遵守同一份規則，不會各自長出兩套互相
矛盾的知識庫流程。
