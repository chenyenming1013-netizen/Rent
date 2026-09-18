# 租屋小幫手（rent）

七間房（輝煌、民權 A/B、憲政 01–04）的收費紀錄網頁，分屋主端與房客端。

## 功能

房客端 `/tenant`
- 輸入房東提供的四位數密碼進入自己的房間；空房第一次使用要填姓名、電話、入住日
- 登入後手機記住一年；可自行修改密碼（房東看不到自訂密碼）
- 查看房東發出的繳費單（本月月租、本月電費、之前尚未繳清的項目）
- 勾選要繳的項目後「繳費並通知房東」；填錯可取消重填
- 金額有誤可回報房東，房東修改後提示自動消失
- 繳費方式：匯款／轉帳（末五碼）、現金、LINE Pay、無摺存款（付款人）

屋主端 `/owner`
- 發送繳費單：月租自動帶入、電費由屋主輸入，一鍵發送；晚到的電費可指定為上個月份
- 租屋總覽：本月總收入、待確認收款、房間卡片（依物件分色）、最近確認紀錄、電費單價、房客入口 QR code
- 新增房間（可新增物件）、自訂房型清單
- 單間房管理：房客密碼、基本資料、確認收款、解除綁定、刪除或停用房間
- 屋主密碼可在網頁修改；忘記時執行 `python reset_owner_password.py`
- 匯出 CSV（繳費單明細、付款紀錄），Excel 可直接開啟
- 繳費帳號與 QR code 設定，顯示在房客繳費畫面
- 退租時可將未繳項目標記為「押金扣抵」或「已收現金」
- 記錄 PythonAnywhere 延長日期，超過 25 天用 LINE 提醒管理者

## 本機執行（Windows PowerShell）

```powershell
cd C:\Users\User\Desktop\rent
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

開啟 http://127.0.0.1:5000 。本機資料存在 `instance\rent.db`，屋主預設密碼 `admin`。

## 角色

| 角色 | 登入 | 能做的事 |
|---|---|---|
| 管理者 | `OWNER_PASSWORD` 或網頁上設定的密碼 | 全部功能、新增與刪除房間、系統設定、LINE 綁定 |
| 房東 | 管理者在「系統設定」設定的房東密碼 | 發送繳費單、確認收款、查看總覽 |
| 房客 | 房間四位數密碼 | 看繳費單、繳費並通知房東 |

## LINE 通知

- 繳費單發出 → 公告群組（不含房號與金額）
- 房客通知付款 → 房東（含金額與末五碼）
- 房東確認收款 → 該房客
- 每日 09:00 → 期限前 3 天／當天／逾期隔天提醒房客，房東收到待辦摘要
- 推播失敗等系統異常 → 管理者

設定方式：管理者登入 → 系統設定 → 產生綁定碼 → 對方私訊官方帳號。
Webhook URL：`https://你的網址/line/callback`
每日排程：用 cron-job.org 每天呼叫 `https://你的網址/cron/daily?key=CRON_KEY`

## 環境變數（寫在 PythonAnywhere 的 WSGI 檔，不要進版控）

| 名稱 | 用途 |
|---|---|
| `OWNER_PASSWORD` | 管理者密碼（網頁上改過之後就以資料庫為準） |
| `SECRET_KEY` | 登入狀態加密用，隨機長字串 |
| `LINE_CHANNEL_SECRET` | LINE Channel secret |
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Channel access token |
| `CRON_KEY` | 每日排程網址的密鑰，隨機英數字 |
| `LINE_BASIC_ID` | 官方帳號 ID，預設 `@234xxejt` |
| `DATABASE_URL` | PostgreSQL 連線字串；沒設就用本機 SQLite |

產生隨機字串：`python -c "import secrets; print(secrets.token_hex(32))"`

## 部署到 Vercel

1. Vercel → Add New → Project → 匯入 GitHub 的 Rent repo，直接 Deploy（會自動偵測 Flask）
2. 專案的 Storage 分頁 → 安裝 Neon，連結到這個專案（會自動加入 DATABASE_URL）
3. Settings → Environment Variables 加入 OWNER_PASSWORD、SECRET_KEY
4. Deployments → 最新一筆 → Redeploy

之後每次 git push 都會自動更新網站。樣式檔放在 `public/static/`。
