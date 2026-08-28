# data/

本機執行時產生的資料，不進版控（見 `.gitignore`）。

- `twflow.db` — SQLite 資料庫，由 `twflow sync` / `poll` / `eod` 建立
- `holidays.txt` — 國定假日清單，一行一個 `YYYY-MM-DD`。盤後抓取遇到空資料時
  會自動把該日記進來
- `bsr/` — 手動從 [BSR](https://bsr.twse.com.tw/bshtm/) 下載的券商分點 CSV，
  檔名格式 `YYYY-MM-DD_股票代號.csv`（例如 `2026-08-27_2330.csv`）
