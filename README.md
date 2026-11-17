# BountyTracker

每天記錄各 Bug Bounty program 的回報數量,並顯示每日增量、平均值與 scope 變動。
目前以 YesWeHack 為基礎模板(`platforms/yeswehack.py`)。

## 使用

```bash
pip install -r requirements.txt
python3 tracker.py serve            # http://127.0.0.1:8787 ,每 6 小時自動抓一次
python3 tracker.py add <program 連結或 slug>
python3 tracker.py collect          # 手動抓一次(可放 cron / systemd timer)
```

`serve` 的選用旗標:`--host 0.0.0.0` 對區網開放、`--sync-all` 自動追蹤平台上所有公開 program、
`--read-only` 關閉網頁上的新增 / 刪除 / 手動更新。網站本身沒有登入機制,不要直接暴露在外網。

資料存在 `data/bountytracker.db`(SQLite)。每個 program 每天保留一筆快照,同一天多次抓取會覆蓋成最新值。

## 資料來源與限制

- 只打 `GET https://api.yeswehack.com/programs/<slug>`,每個 program 每次一個請求。
- 匿名可取得:累計回報數、過去 24 小時 / 7 天 / 本月回報數、scope 清單。
- **各 scope 的回報數匿名時為 null**。把自己的 YesWeHack token 放到 `data/yeswehack.token`
  (或環境變數 `YWH_TOKEN`)後會以 `Authorization: Bearer` 帶入;此路徑尚未用真實 token 驗證過。
- 每日增量 = 當日快照累計 − 前一筆快照累計,所以要追蹤滿兩天才有變動紀錄。

## 新增其他平台

在 `platforms/` 加一個模組,提供 `NAME`、`parse(text) -> slug`、`fetch(slug, token) -> dict`
(欄位照 `yeswehack.fetch` 的回傳),再註冊到 `platforms/__init__.py` 的 `ADAPTERS`。

## License

MIT
