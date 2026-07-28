# 本地知識迭代系統

這套工具會把 Excel、文件與筆記整理成一個 Codex 和 Claude Code 都能使用的本地知識庫。
不需要安裝 Obsidian，也不需要懂 Git、Python 或終端機。

最重要的分工是：

```text
公開 Repo = 工具和說明書，可以放 GitHub
KnowledgeBase/ = 你的私人資料，只留在自己的電腦
```

## 初次安裝：只要跟 AI 說一句話

你可以把 Repo 放在任何你想要的位置：

```text
Repo 放在 C 槽 → KnowledgeBase 也建立在這個 C 槽專案裡
Repo 放在 D 槽 → KnowledgeBase 也建立在這個 D 槽專案裡
Repo 放在 OneDrive → 可以使用；OneDrive 只警告、不阻擋
```

建議用 Codex 或 Claude Code 的 Clone 功能取得：
`https://github.com/Jason5330/local-knowledge-compiler`

Clone 完成後，資料夾通常就叫 `local-knowledge-compiler`，不會帶 `-master`。如果公司
環境只能用 GitHub 的 Download ZIP，解壓縮後可能叫
`local-knowledge-compiler-master`；請 AI 幫你改名成 `local-knowledge-compiler` 即可。

在 `local-knowledge-compiler` 專案資料夾打開 Codex 或 Claude Code，輸入：

```text
初始化本專案知識庫
```

AI 會完成：

```text
檢查環境
→ 安裝本專案
→ 在目前專案建立 KnowledgeBase/
→ 啟用 Git 防誤上傳
→ 執行狀態與完整性檢查
```

## 日常使用

把 Excel 或其他資料的「副本」放進 `KnowledgeBase/00_inbox/`，然後說：

```text
把新資料整理進我的知識庫
```

提問時說：

```text
用我的知識庫回答這個問題：【你的問題】
```

想讓同一問題的回答成為下一輪知識時，再說：

```text
保存這次回答
```

健康檢查說：

```text
檢查知識庫是否正常
```

卡住時說：

```text
繼續處理知識庫中卡住的工作
```

## 資料會怎麼走

```text
00_inbox 新資料副本
→ 10_raw 保存原始版本
→ 40_index 建立可搜尋索引
→ kb prepare 找出和問題最相關的證據
→ AI 只根據證據回答並標示來源
→ kb finalize 保存你同意留下的回答
→ 20_wiki 在後續整理中持續更新
```

原始資料和整理後的 Wiki 是兩層。Wiki 可以重新整理，但原始版本不能被它取代。
相同問題不會偷偷自動保存；只有你要求「保存這次回答」並完成 `kb finalize`，回答才會
寫入知識庫，參加下一輪整理。

## 私隱與 GitHub 安全

`KnowledgeBase/` 是本機私人資料夾，永遠不會跟著上傳 GitHub。Repo 已用三層保護：

1. 根目錄規則 `/KnowledgeBase/` 讓 Git 忽略資料。
2. commit 前再次檢查。
3. push 前檢查是否曾有人強制加入資料。

這些保護是防呆，不代表可以主動把私人內容貼進 README、Issue、PR 或對話。

「檔案存在本機」也不代表送給 AI 的內容仍然離線。Codex 或 Claude 可能是雲端模型；
公司機密、個資或敏感資料仍要遵守你的公司規定。

## Excel 安全規則

原始 Excel 永遠保留在原位置。AI 必須先複製一份到 `00_inbox`，而且
`ingest-once` 只能處理這份副本，不能直接處理唯一原檔。

## 你可能看到的狀態

- `pending_attention`：資料沒有消失，只是需要 AI 接手一個步驟。
- `pending_extractor`：檔案已保存，但目前缺少讀取掃描 PDF、圖片、影音等內容的工具。
- 裸網址：只當文字書籤，系統不會自行下載網頁。

Codex Desktop 沒有可呼叫的背景 CLI 時可以用人工交接模式；Claude CLI 只有在已安裝、
登入並實測成功後才使用。系統不會自行設定 `shell:startup`、排程或開機常駐。

技術代理會在內部使用 `kb prepare`、`kb finalize`、`kb lint` 等命令。一般使用者只要
使用上面的自然語言即可。Wiki 變更若需要復原，代理應先查看 `git log`、說明影響，再經
同意使用 `git revert`。

## 詳細說明

- [零基礎安裝與使用指南](docs/BEGINNER_GUIDE.zh-TW.md)
- [Codex／Claude Code 內部命令參考](docs/CLI_REFERENCE.zh-TW.md)
- [換電腦或換 AI 的完整交接](AI_HANDOFF.md)

## 授權

本專案採用 [MIT License](LICENSE)。任何人都可以使用、修改與分享這套工具，但必須保留
原著作權與 MIT 授權聲明。使用者放進 `KnowledgeBase/` 的資料不因本授權而公開。
