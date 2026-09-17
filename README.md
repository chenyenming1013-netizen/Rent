# 租屋小幫手（rent）

七間房（輝煌、民權 A/B、憲政 01–04）的收費紀錄網頁，分屋主端與房客端。

## 功能

房客端 `/tenant`
- 輸入房東提供的四位數密碼進入自己的房間；空房第一次使用要填姓名、電話、入住日
- 登入後手機記住一年；可自行修改密碼（房東看不到自訂密碼）
- 月租繳費並通知、電費繳費並通知；可勾選「電費一起繳」合併成同一筆
- 繳費方式：匯款／轉帳（末五碼）、現金、LINE Pay、無摺存款（付款人）

屋主端 `/owner`
- 租屋總覽：待確認收款、房間卡片（依物件分色）、最近確認紀錄、電費單價、房客入口 QR code
- 新增房間（可新增物件）、自訂房型清單
- 單間房管理：房客密碼、基本資料、確認收款、解除綁定、刪除或停用房間
- 屋主密碼可在網頁修改；忘記時執行 `python reset_owner_password.py`

## 本機執行（Windows PowerShell）

```powershell
cd C:\Users\User\Desktop\rent
python -m venv .venv
.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python app.py
```

開啟 http://127.0.0.1:5000 。本機資料存在 `instance\rent.db`，屋主預設密碼 `admin`。

## 環境變數（部署時設定，不要寫進程式或 commit）

| 名稱 | 用途 |
|---|---|
| `OWNER_PASSWORD` | 屋主登入密碼，請用長一點的密碼 |
| `SECRET_KEY` | 登入狀態加密用，隨機長字串 |
| `DATABASE_URL` | PostgreSQL 連線字串（例如 Neon），沒設就用本機 SQLite |

產生 SECRET_KEY：`python -c "import secrets; print(secrets.token_hex(32))"`

## 部署到 Vercel

1. Vercel → Add New → Project → 匯入 GitHub 的 Rent repo，直接 Deploy（會自動偵測 Flask）
2. 專案的 Storage 分頁 → 安裝 Neon，連結到這個專案（會自動加入 DATABASE_URL）
3. Settings → Environment Variables 加入 OWNER_PASSWORD、SECRET_KEY
4. Deployments → 最新一筆 → Redeploy

之後每次 git push 都會自動更新網站。樣式檔放在 `public/static/`。
