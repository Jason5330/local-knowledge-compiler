# 本地知識庫共用作業規則

這是 Codex 與 Claude 共用的唯一正式規則。回答「使用者自己的資料、決策、專案、
筆記或歷史」等知識問題時，必須遵守以下流程。

1. 先執行 `kb prepare "<問題>" --space <space>`，再開始回答。`space` 必須由使用者
   明確指定，或只能採用系統已安全判定的範圍；不得混入其他空間的資料。
2. 只使用本地證據包中的 `evidence`。不得搜尋網路、不得補寫模型記憶，也不得把
   `pending_jobs` 當成已證實內容。
3. 每個重要結論都要附上結構化引用。原始證據使用
   `source_id + version_id + locator + evidence_sha256`；Wiki 衍生證據使用
   `path + locator + source_ids + evidence_sha256`。
4. 若證據不足、互相衝突或尚待處理，必須明說 `insufficient_evidence`、列出缺口，
   並說明哪些資料仍是 `pending_extractor` 或待人工處理；不得猜測。
5. 回答完成後，把答案與結構化引用存成 JSON，再執行
   `kb finalize --packet <證據包.json> --answer <答案.json>`。
6. `10_raw` 是不可改寫的原始資料；`20_wiki` 是可重建的衍生整理。不得直接修改、
   刪除或把衍生答案重新當成原始證據。
7. `personal`、`work`、`shared`、`unclassified` 與 `project:<slug>` 必須隔離。
   未獲明確授權，不得跨 space 搜尋、引用或推論。
8. 裸網址只視為書籤文字，永遠不得抓取。不得因看到 URL 就開啟網頁、下載內容或
   呼叫任何 web、browser、HTTP、網路搜尋工具。

一般程式設計問題或與此知識庫無關的聊天，不需要執行上述知識查詢流程。
