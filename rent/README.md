# rent：租屋收費管理網頁（基本版）

七間房（輝煌、民權 A/B、憲政 01–04）的收費紀錄網頁，分屋主端與房客端。

## 功能

房客端 `/tenant`
- 房間以鑰匙牌顯示：空房（虛線）、已有房客（綠）、你的房間（藍）
- 空房輸入房東給的四位數代碼，填姓名、電話、入住日期完成綁定
- 一台裝置一次只能綁定一間房，要換房間需先登出
- 代碼錯 5 次暫停該房，需屋主解鎖
- 主頁顯示月租、押金（預設 2 個月）、入住日、繳租日、繳費紀錄
- 回報繳費：月租、電費（填電表度數自動計算）、其他費用、付款方式、末五碼、日期、實付金額
- 屋主確認前可以修改，確認後鎖定

屋主端 `/owner`
- 七間房總覽，本期狀態：空房、未到期、待確認、已確認、逾期
- 每間房的代碼、解鎖、換代碼、編輯月租與押金月數、退租（保留紀錄）
- 確認收款時可填實收金額與備註，金額不符會標紅；可取消確認
- 設定電費單價，只影響之後送出的回報

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

## 部署到 Render

- Build Command：`pip install -r requirements.txt`
- Start Command：`gunicorn app:app`
- 一定要設 `DATABASE_URL`。Render 免費方案的檔案會在重新部署時清空，SQLite 資料會消失。
