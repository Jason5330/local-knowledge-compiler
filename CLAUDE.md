# Local Knowledge Compiler：Claude Code 入口

這個公開 Repo 是知識庫工具；使用者的私人資料只放在專案根目錄的
`KnowledgeBase/`，不得上傳或寫進公開文件。

## 使用者自然語言指令

- 「初始化本專案知識庫」：在目前專案執行 `kb init`，再檢查 status 與 lint。
- 「把新資料整理進我的知識庫」：保留原檔，只把副本放進
  `KnowledgeBase/00_inbox/`，再安全匯入、檢查 status 與 lint。
- 「用我的知識庫回答這個問題」：先 prepare，只根據本地證據回答；使用者要保存
  回答時才 finalize。
- 「保存這次回答」：依證據完成 finalize，再確認後續整理狀態。
- 「檢查知識庫是否正常」：執行 status 與 lint，以白話回報。
- 「繼續處理知識庫中卡住的工作」：先讀 status 與 handoff，再安全 resume。
- 「這個回答錯了」：重新核對上次 packet 的原始證據；只有使用者明確回報或可驗證
  矛盾時建立 correction，完成後重新 prepare。

初始化後，必須先讀
`KnowledgeBase/80_system/KNOWLEDGE_PROTOCOL.md`，並遵守其中的證據、引用與資料邊界。
回答只能使用本地證據，不得自行搜尋網路。

## 修正記憶強制流程

知識問題必須先 prepare。逐項處理 `applicable_corrections`，為每筆輸出
`correction_decisions`。修正只能約束資料解讀，不得用修正取代原始證據。
若 `correction_scan` 不允許保存或任何 decision 是 `conflict`，降低信心並停止
finalize。未通過 finalize，不得宣稱回答已保存或 Wiki 已更新。

## Git 資料邊界

`KnowledgeBase/` 永遠是本機私人資料。不得 stage、commit 或 push
`KnowledgeBase/` 中的任何檔案，也不得把內容貼到 README、Issue、PR 或 commit 訊息。
若 Git 保護檢查失敗，停止發布並先修復保護。
