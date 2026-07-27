# 本地知識迭代系統

這套工具把 Excel、文件與筆記整理成 Codex 和 Claude Code 都能共用的本地知識庫。
你不需要安裝 Obsidian，也不需要自己輸入終端機指令。

## 小白從這裡開始

打開以下指南，選擇你已經能使用的 AI，把裡面的提示詞整段貼給它：

- [Codex／Claude Code 零基礎安裝與使用指南](docs/BEGINNER_GUIDE.zh-TW.md)
- [給 AI 使用的內部指令參考](docs/CLI_REFERENCE.zh-TW.md)
- [換電腦／換 AI 的完整交接文件](AI_HANDOFF.md)

> 最重要的前提：Codex 或 Claude Code 至少要有一個已經能打開。
> 如果兩個都還沒有，必須先請公司資訊人員或懂電腦的人替你安裝其中一個；AI 尚未
> 啟動前，無法替自己安裝。

## 初次安裝

你只需把指南中的「首次安裝提示詞」貼給 Codex 或 Claude Code。AI 會替你：

```text
檢查環境 → 下載專案 → 建立執行環境 → 建立知識庫 → 測試 → 回報結果
```

預設位置：

```text
系統程式：C:\AI\local-knowledge-compiler
知識庫：C:\KnowledgeBase
```

已經有同名資料夾時，AI 必須先檢查並保留原資料，不得直接覆蓋。

## 每天怎麼用

你只要對 Codex 或 Claude Code 說自然語言：

```text
匯入 Excel → AI 複製一份到 00_inbox → 整理並保留原始版本
提出問題 → AI 執行 kb prepare → 只依證據回答並引用
保存回答 → AI 執行 kb finalize → 更新可搜尋的知識
健康檢查 → AI 執行 kb status 與 kb lint
需要復原 → AI 先查看 git log，再經你同意執行 git revert
```

請不要把唯一一份 Excel 直接交給匯入器。AI 必須先複製，原檔永遠留在原位置。

## Codex 與 Claude Code 如何共用

- 兩者都使用 `C:\KnowledgeBase`，不要各建一套。
- 兩者都必須遵守 `80_system/KNOWLEDGE_PROTOCOL.md`。
- Codex 可以匯入、找證據、回答、保存答案與整理知識。
- Claude Code 也能做相同工作；若電腦上的 Claude CLI 可用，還能擔任背景編譯器。
- Codex Desktop 不能當作背景 CLI 時，系統改用人工交接模式，不會假裝已經完成。

## 你需要知道的限制

- Codex 與 Claude 都是雲端模型。本機保存資料，不代表送給 AI 的內容仍然離線。
- 裸網址只會保存成文字，不會自動抓取網頁。
- 圖片、掃描 PDF、音訊與影片若無擷取器，會標記 `pending_extractor`。
- 工作顯示 `pending_attention`，代表 AI 還需處理，不代表原始資料遺失。
- 內部 Exit code `0` 代表成功。
- 內部 Exit code `1` 代表錯誤。
- 內部 Exit code `2` 代表需要人工接手。
- 是否設定背景監看或 Windows 自動啟動，必須先取得你的明確同意；AI 不得自行設定
  `shell:startup`。

遇到問題時，不用研究 `kb.exe status --vault`、`kb.exe resume --vault` 或
`--job-id` 的寫法。直接貼指南中的「健康檢查提示詞」，讓 AI 檢查、修復並用白話
回報。

## 開源授權

本專案採用 [MIT License](LICENSE)。

簡單說，任何人都可以使用、修改、分享或商業使用這套系統，但必須保留原本的
著作權與 MIT 授權聲明。軟體依現狀提供，作者不對使用結果提供保證或承擔責任。
