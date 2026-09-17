"""忘記屋主密碼時執行：清除網頁上設定的密碼，改回 WSGI 檔裡的 OWNER_PASSWORD。

用法（PythonAnywhere 的 Bash）：
    cd ~/Rent
    workon rent
    python reset_owner_password.py
執行後到 Web 頁按 Reload。
"""
from app import app, db, Setting

with app.app_context():
    s = db.session.get(Setting, "owner_pw_hash")
    if s:
        db.session.delete(s)
    v = db.session.get(Setting, "owner_ver") or Setting(key="owner_ver", value="0")
    v.value = str(int(v.value or 0) + 1)
    db.session.add(v)
    db.session.commit()
print("已重設。請用 WSGI 檔裡的 OWNER_PASSWORD 登入，然後到 Web 頁按 Reload。")
