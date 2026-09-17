"""租屋小幫手：租屋收費管理網頁

角色：
  屋主端  /owner   用屋主密碼登入：租屋總覽、確認收款、管理與新增房間
  房客端  /tenant  輸入房東提供的四位數密碼 → 空房先填資料綁定 → 進入自己的房間
                   查看房東發出的繳費單（月租、電費），付款後通知房東；可自行修改密碼
"""
import hmac
import io
import json
import os
import secrets
import zlib
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import qrcode
import qrcode.image.svg
from flask import (Flask, abort, flash, redirect, render_template, request,
                   session, url_for)
from flask_sqlalchemy import SQLAlchemy
from markupsafe import Markup
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

import line_api

TZ = ZoneInfo("Asia/Taipei")


def tw_now():
    """台灣時間（不含時區資訊，直接存進資料庫）。主機是 UTC，所以一律用這個。"""
    return datetime.now(TZ).replace(tzinfo=None)


def tw_today():
    return tw_now().date()

# ================================================================ 設定
app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or "dev-only-change-me"
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 365  # 登入記住一年

db_url = os.environ.get("DATABASE_URL") or "sqlite:///rent.db"
if db_url.startswith("postgres://"):
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 280}

OWNER_PASSWORD = os.environ.get("OWNER_PASSWORD", "admin")
IP_MAX_FAILS = 5          # 同一個網路一小時內，房客密碼最多錯幾次
GLOBAL_MAX_FAILS = 50     # 全部加起來錯這麼多次，就暫停房客登入，等屋主重新開放
BIND_MINUTES = 15         # 輸入密碼後多久內要完成綁定
OWNER_MAX_FAILS = 5       # 屋主登入一小時內最多錯幾次
CHANGE_MAX_REJECTS = 3    # 房客改密碼時，一天內最多被拒絕幾次
LINE_BASIC_ID = os.environ.get("LINE_BASIC_ID", "@234xxejt")
CRON_KEY = os.environ.get("CRON_KEY", "")
BIND_CODE_MINUTES = 10

DEFAULT_ROOMS = [
    ("輝煌", "輝煌", "套房"),
    ("民權", "民權 A", "套房"), ("民權", "民權 B", "套房"),
    ("憲政", "憲政 01", "套房"), ("憲政", "憲政 02", "套房"),
    ("憲政", "憲政 03", "套房"), ("憲政", "憲政 04", "套房"),
]
DEFAULT_ROOM_TYPES = ["套房", "雅房", "整層", "日租", "店面", "車位", "其他"]
WEAK_CODES = {"0000", "1234", "4321", "1111", "2222", "3333", "4444", "5555",
              "6666", "7777", "8888", "9999", "1212", "0123", "9876"}

PAY_METHODS = ["匯款／轉帳", "現金", "LINE Pay", "無摺存款"]
KIND_LABEL = {"rent": "月租", "elec": "電費", "both": "月租＋電費"}

# 物件顏色：(牆面, 屋頂/重點色, 橫幅樣式)
PALETTE = [("#FFB4A2", "#F2785C", "coral"), ("#9ED8F5", "#3E9BD6", "sky"),
           ("#A8E6C9", "#3FB984", "mint"), ("#D9C8F5", "#8E6CD6", "lilac"),
           ("#FFE08A", "#D9A400", "warm")]
FIXED_COLORS = {"輝煌": 0, "民權": 1, "憲政": 2}

db = SQLAlchemy(app)


# ================================================================ 資料表
class Room(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    building = db.Column(db.String(20), nullable=False)   # 物件（大樓）
    name = db.Column(db.String(20), unique=True, nullable=False)
    sort = db.Column(db.Integer, default=0)
    room_type = db.Column(db.String(20), default="套房")
    active = db.Column(db.Boolean, default=True)           # 停用的房間不出現在總覽
    rent = db.Column(db.Integer, default=0)
    deposit_months = db.Column(db.Integer, default=2)
    meter_start = db.Column(db.Integer, default=0)         # 入住時電表度數
    code = db.Column(db.String(8))                         # 房東產生的密碼（房東看得到）
    code_hash = db.Column(db.String(255))                  # 房客自訂的密碼（只存雜湊）
    failed = db.Column(db.Integer, default=0)              # 舊版欄位，保留相容
    locked = db.Column(db.Boolean, default=False)          # 舊版欄位，保留相容

    @property
    def tenant(self):
        return Tenant.query.filter_by(room_id=self.id, active=True).first()

    @property
    def custom_code(self):
        return bool(self.code_hash)

    def matches(self, code):
        if self.code_hash:
            return check_password_hash(self.code_hash, code)
        return bool(self.code) and hmac.compare_digest(self.code, code)

    def set_owner_code(self, code):
        self.code, self.code_hash = code, None

    @property
    def deposit(self):
        return (self.rent or 0) * (self.deposit_months or 0)

    @property
    def color(self):
        return building_color(self.building)


class Tenant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey("room.id"), nullable=False)
    name = db.Column(db.String(40), nullable=False)
    phone = db.Column(db.String(20), default="")
    checkin = db.Column(db.Date, nullable=False)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=tw_now)
    moved_out_at = db.Column(db.DateTime)
    pin_hash = db.Column(db.String(255), default="")   # 舊版欄位，保留相容
    pin_ver = db.Column(db.Integer, default=1)         # 換密碼時加一，讓舊登入失效
    verified = db.Column(db.Boolean, default=False)    # 房東是否已核對
    pay_day = db.Column(db.Integer)                    # 房東指定的繳租日（1~28），空白＝依入住日

    @property
    def due_day(self):
        if self.pay_day and 1 <= self.pay_day <= 28:
            return self.pay_day
        return min(self.checkin.day, 28)

    def due_date(self, period):
        y, m = map(int, period.split("-"))
        return date(y, m, self.due_day)


class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenant.id"), nullable=False)
    room_id = db.Column(db.Integer, db.ForeignKey("room.id"), nullable=False)
    period = db.Column(db.String(7), nullable=False)     # YYYY-MM
    kind = db.Column(db.String(8), nullable=False, default="both")  # rent / elec / both
    rent_amount = db.Column(db.Integer, default=0)
    meter_last = db.Column(db.Integer)
    meter_now = db.Column(db.Integer)
    usage = db.Column(db.Integer, default=0)
    rate = db.Column(db.Float, default=0)                # 當時的電費單價
    elec_amount = db.Column(db.Integer, default=0)
    other_amount = db.Column(db.Integer, default=0)
    other_note = db.Column(db.String(100), default="")
    total = db.Column(db.Integer, default=0)             # 房客回報實際付款金額
    method = db.Column(db.String(20), default="匯款／轉帳")
    last5 = db.Column(db.String(5), default="")
    payer = db.Column(db.String(40), default="")         # LINE Pay／無摺存款的付款人
    paid_date = db.Column(db.Date)
    status = db.Column(db.String(10), default="待確認")
    received = db.Column(db.Integer)
    confirm_note = db.Column(db.String(200), default="")
    confirmed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=tw_now)
    updated_at = db.Column(db.DateTime, default=tw_now, onupdate=tw_now)

    tenant = db.relationship("Tenant")
    room = db.relationship("Room")
    __table_args__ = (db.UniqueConstraint("tenant_id", "period", "kind"),)

    @property
    def due(self):
        return (self.rent_amount or 0) + (self.elec_amount or 0) + (self.other_amount or 0)

    @property
    def is_bill(self):
        return (self.kind or "").startswith("b") and self.kind != "both"

    @property
    def kind_label(self):
        if self.is_bill:
            return "繳費單"
        return KIND_LABEL.get(self.kind, "")

    @property
    def has_rent(self):
        return self.kind in ("rent", "both")

    @property
    def has_elec(self):
        return self.kind in ("elec", "both")

    @property
    def confirmed(self):
        return self.status == "已確認"

    @property
    def pay_info(self):
        d = self.paid_date.strftime("%m/%d") if self.paid_date else ""
        if self.last5:
            return f"末五碼 {self.last5}・{d}・{self.method}"
        if self.payer:
            return f"付款人 {self.payer}・{d}・{self.method}"
        return f"{d}・{self.method}"

    @property
    def detail(self):
        if self.is_bill:
            return "、".join(f"{i.label} {i.amount:,}" for i in self.items)
        parts = []
        if self.has_rent:
            parts.append(f"月租 {self.rent_amount:,}")
        if self.has_elec:
            parts.append(f"電費 {self.elec_amount:,}（{self.meter_last}→{self.meter_now}，{self.usage} 度）")
        return "＋".join(parts)


class Bill(db.Model):
    """房東每月發出的繳費單。"""
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenant.id"), nullable=False)
    room_id = db.Column(db.Integer, db.ForeignKey("room.id"), nullable=False)
    period = db.Column(db.String(7), nullable=False)
    created_at = db.Column(db.DateTime, default=tw_now)
    updated_at = db.Column(db.DateTime, default=tw_now, onupdate=tw_now)
    items = db.relationship("BillItem", backref="bill", cascade="all, delete-orphan",
                            order_by="BillItem.id")
    __table_args__ = (db.UniqueConstraint("tenant_id", "period"),)


class BillItem(db.Model):
    """繳費單上的一個項目：某月月租或某月電費。"""
    id = db.Column(db.Integer, primary_key=True)
    bill_id = db.Column(db.Integer, db.ForeignKey("bill.id"), nullable=False)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenant.id"), nullable=False)
    kind = db.Column(db.String(8), nullable=False)       # rent / elec / diff（差額或溢繳折抵）
    month = db.Column(db.String(7), nullable=False)      # 這筆費用屬於哪個月份
    amount = db.Column(db.Integer, nullable=False, default=0)
    note = db.Column(db.String(60), default="")
    kwh = db.Column(db.Integer)                          # 用電度數（電費用）
    rate = db.Column(db.Float)                           # 當時的台電公告電價（每度）
    payment_id = db.Column(db.Integer, db.ForeignKey("payment.id"))
    source_payment_id = db.Column(db.Integer)            # 差額項目來自哪一筆付款
    paid = db.Column(db.Boolean, default=False)
    paid_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=tw_now)
    payment = db.relationship("Payment", backref="items", foreign_keys=[payment_id])

    @property
    def label(self):
        m = int(self.month[5:])
        if self.kind == "diff":
            return f"{m}月差額" if self.amount > 0 else f"{m}月溢繳折抵"
        return f"{m}月{'月租' if self.kind == 'rent' else '電費'}"

    @property
    def state(self):
        if self.paid:
            return "paid"
        if self.payment_id:
            return "pending"
        return "unpaid"


class LineUser(db.Model):
    """綁定到 LINE 官方帳號的人。role：tenant 房客、landlord 房東、admin 管理者。"""
    user_id = db.Column(db.String(64), primary_key=True)
    role = db.Column(db.String(10), nullable=False)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenant.id"))
    created_at = db.Column(db.DateTime, default=tw_now)
    tenant = db.relationship("Tenant")


class Notice(db.Model):
    """推播紀錄，方便管理者檢查。"""
    id = db.Column(db.Integer, primary_key=True)
    at = db.Column(db.DateTime, default=tw_now, index=True)
    target = db.Column(db.String(40))
    kind = db.Column(db.String(20))
    ok = db.Column(db.Boolean)
    detail = db.Column(db.String(300))


class Setting(db.Model):
    key = db.Column(db.String(40), primary_key=True)
    value = db.Column(db.String(2000))


class LoginFail(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ip = db.Column(db.String(64), index=True)
    at = db.Column(db.DateTime, default=tw_now, index=True)


def get_setting(key, default=""):
    s = db.session.get(Setting, key)
    return s.value if s else default


def set_setting(key, value):
    s = db.session.get(Setting, key) or Setting(key=key)
    s.value = str(value)
    db.session.add(s)


def room_types():
    try:
        types = json.loads(get_setting("room_types", "[]"))
    except ValueError:
        types = []
    return types or list(DEFAULT_ROOM_TYPES)


def buildings():
    """所有物件名稱，依第一次出現的順序。"""
    seen = []
    for r in Room.query.order_by(Room.sort, Room.id).all():
        if r.building not in seen:
            seen.append(r.building)
    return seen


def building_color(name):
    if name in FIXED_COLORS:
        return PALETTE[FIXED_COLORS[name]]
    others = [b for b in buildings() if b not in FIXED_COLORS]
    i = others.index(name) if name in others else len(others)
    return PALETTE[(3 + i) % len(PALETTE)]


def find_room(code, exclude_room_id=None):
    """用密碼找房間；密碼在所有房間中是唯一的。"""
    if not (code.isdigit() and len(code) == 4):
        return None
    for r in Room.query.filter_by(active=True).order_by(Room.sort).all():
        if r.id != exclude_room_id and r.matches(code):
            return r
    return None


def new_code(exclude_room_id=None):
    while True:
        code = f"{secrets.randbelow(10000):04d}"
        if code not in WEAK_CODES and not find_room(code, exclude_room_id):
            if not Room.query.filter(Room.code == code, Room.id != exclude_room_id).first():
                return code


# ---------------------------------------------------------------- 資料庫升級
def migrate():
    insp = inspect(db.engine)
    room_cols = {c["name"] for c in insp.get_columns("room")}
    with db.engine.begin() as conn:
        for col, ddl in [("code", "VARCHAR(8)"), ("code_hash", "VARCHAR(255)"),
                         ("room_type", "VARCHAR(20) DEFAULT '套房'"),
                         ("active", "BOOLEAN DEFAULT 1")]:
            if col not in room_cols:
                conn.execute(text(f"ALTER TABLE room ADD COLUMN {col} {ddl}"))

    tenant_cols = {c["name"] for c in inspect(db.engine).get_columns("tenant")}
    if "pay_day" not in tenant_cols:
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE tenant ADD COLUMN pay_day INTEGER"))

    item_cols = {c["name"] for c in inspect(db.engine).get_columns("bill_item")}
    with db.engine.begin() as conn:
        if "kwh" not in item_cols:
            conn.execute(text("ALTER TABLE bill_item ADD COLUMN kwh INTEGER"))
        if "rate" not in item_cols:
            conn.execute(text("ALTER TABLE bill_item ADD COLUMN rate FLOAT"))
        if "source_payment_id" not in item_cols:
            conn.execute(text("ALTER TABLE bill_item ADD COLUMN source_payment_id INTEGER"))

    pay_cols = {c["name"] for c in inspect(db.engine).get_columns("payment")}
    if "kind" not in pay_cols:
        # 舊版每期只有一筆（月租＋電費）；重建資料表以改用新的唯一條件
        old_cols = [c for c in pay_cols if c != "id"]
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE payment RENAME TO payment_old"))
        Payment.__table__.create(db.engine)
        keep = [c for c in old_cols if c in Payment.__table__.c]
        cols = ", ".join(["id"] + keep)
        with db.engine.begin() as conn:
            conn.execute(text(
                f"INSERT INTO payment ({cols}, kind) SELECT {cols}, 'both' FROM payment_old"))
            conn.execute(text("UPDATE payment SET method = '匯款／轉帳' WHERE method = '銀行轉帳'"))
            conn.execute(text("DROP TABLE payment_old"))
    elif "payer" not in pay_cols:
        with db.engine.begin() as conn:
            conn.execute(text("ALTER TABLE payment ADD COLUMN payer VARCHAR(40) DEFAULT ''"))


def init_db():
    db.create_all()
    migrate()
    if not Room.query.first():
        try:
            for i, (b, n, t) in enumerate(DEFAULT_ROOMS):
                db.session.add(Room(building=b, name=n, sort=i, room_type=t))
            set_setting("elec_rate", "5")
            db.session.commit()
        except IntegrityError:
            db.session.rollback()
    changed = False
    for r in Room.query.order_by(Room.sort).all():
        if r.active is None:
            r.active = True
            changed = True
        if not r.room_type:
            r.room_type = "套房"
            changed = True
        if not r.code and not r.code_hash:
            r.set_owner_code(new_code(r.id))
            db.session.flush()
            changed = True
    if changed:
        db.session.commit()


with app.app_context():
    init_db()


# ================================================================ 共用工具
def to_int(v, default=0):
    try:
        return int(str(v).replace(",", "").strip())
    except (TypeError, ValueError):
        return default


def to_date(v):
    try:
        return datetime.strptime(v, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def this_period():
    return tw_today().strftime("%Y-%m")


def last_meter(room, tenant, period):
    """上期度數：本期以前最近一筆有抄表的紀錄，沒有就用入住度數。"""
    p = (Payment.query.filter(Payment.tenant_id == tenant.id,
                              Payment.meter_now.isnot(None),
                              Payment.period < period)
         .order_by(Payment.period.desc()).first())
    return p.meter_now if p else (room.meter_start or 0)


def period_payments(tenant, period):
    rows = Payment.query.filter_by(tenant_id=tenant.id, period=period).all()
    return {p.kind: p for p in rows}


def rent_record(pays):
    return pays.get("both") or pays.get("rent")


def elec_record(pays):
    return pays.get("both") or pays.get("elec")


def group_by_building(rooms):
    groups = {}
    for r in rooms:
        groups.setdefault(r.building, []).append(r)
    order = buildings()
    return sorted(groups.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 99)


def unpaid_items(tenant):
    return (BillItem.query.filter_by(tenant_id=tenant.id, paid=False)
            .order_by(BillItem.month, BillItem.kind.desc(), BillItem.id).all())


def month_label(period):
    y, m = period.split("-")
    return f"{int(m)}月"


def prev_month(period):
    y, m = map(int, period.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def public_url(path):
    host = request.host
    scheme = "http" if host.startswith(("127.0.0.1", "localhost")) else "https"
    return f"{scheme}://{host}{path}"


def qr_svg(value):
    img = qrcode.make(value, image_factory=qrcode.image.svg.SvgPathImage,
                      box_size=10, border=2)
    buf = io.BytesIO()
    img.save(buf)
    svg = buf.getvalue().decode("utf-8")
    return Markup(svg[svg.index("<svg"):])


@app.template_filter("money")
def money(v):
    return f"{int(v or 0):,}"


@app.context_processor
def inject():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return {"csrf": session["csrf"], "today": tw_today(),
            "PAY_METHODS": PAY_METHODS, "month_label": month_label,
            "role": session.get("role") if owner_required() else None,
            "is_admin": admin_required(), "LINE_BASIC_ID": LINE_BASIC_ID}


CSRF_EXEMPT = {"line_callback"}


@app.before_request
def check_csrf():
    if request.method == "POST" and request.endpoint not in CSRF_EXEMPT:
        token = request.form.get("csrf", "")
        if not hmac.compare_digest(token, session.get("csrf", "")):
            abort(400, "表單已過期，請重新整理頁面")


def read_payment_fields(f, errors):
    """讀取並檢查繳費方式相關欄位。"""
    method = f.get("method", "")
    if method not in PAY_METHODS:
        errors["method"] = "請選擇繳費方式"
    last5 = f.get("last5", "").strip()
    payer = f.get("payer", "").strip()
    paid_date = to_date(f.get("paid_date"))
    total = to_int(f.get("total"), -1)
    if method == "匯款／轉帳":
        payer = ""
        if not (last5.isdigit() and len(last5) == 5):
            errors["last5"] = "請填轉出帳號末五碼（五位數字）"
    else:
        last5 = ""
    if method in ("LINE Pay", "無摺存款"):
        if not payer:
            errors["payer"] = "請填付款人名稱"
    else:
        payer = "" if method != "匯款／轉帳" else payer
    if not paid_date:
        errors["paid_date"] = "請選擇日期"
    if total <= 0:
        errors["total"] = "請填實際付款金額"
    return dict(method=method, last5=last5, payer=payer, paid_date=paid_date, total=total)


def prev_payment_info(tenant):
    p = (Payment.query.filter(Payment.tenant_id == tenant.id, Payment.last5 != "")
         .order_by(Payment.created_at.desc()).first())
    q = (Payment.query.filter(Payment.tenant_id == tenant.id)
         .order_by(Payment.created_at.desc()).first())
    return {"last5": p.last5 if p else "", "method": q.method if q else "匯款／轉帳",
            "payer": q.payer if q and q.payer else tenant.name}


# ================================================================ LINE 通知
def site_url(endpoint, **kw):
    try:
        return public_url(url_for(endpoint, **kw))
    except RuntimeError:
        return ""


def notify(to, text, kind):
    """推播並記錄；失敗時提醒管理者（每種錯誤每天最多一次）。"""
    if not to:
        return False
    ok, detail = line_api.push(to, text)
    db.session.add(Notice(target=to[:40], kind=kind, ok=ok, detail="" if ok else detail))
    db.session.commit()
    if not ok and kind != "admin_alert":
        alert_admin(f"LINE 推播失敗（{kind}）：{detail[:120]}")
    return ok


def alert_admin(text):
    today_key = f"alert_{tw_today().isoformat()}_{zlib.crc32(text[:40].encode()) % 100000}"
    if get_setting(today_key):
        return
    set_setting(today_key, "1")
    db.session.commit()
    for u in LineUser.query.filter_by(role="admin").all():
        notify(u.user_id, f"【租屋小幫手・系統通知】\n{text}", "admin_alert")


def notify_landlords(text, kind):
    users = LineUser.query.filter_by(role="landlord").all()
    for u in users:
        notify(u.user_id, text, kind)
    return bool(users)


def notify_tenant(tenant, text, kind):
    for u in LineUser.query.filter_by(role="tenant", tenant_id=tenant.id).all():
        notify(u.user_id, text, kind)


def notify_group(text, kind):
    gid = get_setting("line_group_id")
    if gid:
        return notify(gid, text, kind)
    return False


# ================================================================ 入口
@app.route("/")
def index():
    if tenant_required()[0] and not owner_required():
        return redirect(url_for("tenant_home"))
    return render_template("index.html")


@app.route("/ping")
def ping():
    return "ok"


# ================================================================ 房客端
def tenant_required():
    rid = session.get("room_id")
    room = db.session.get(Room, rid) if rid else None
    t = room.tenant if room and room.active else None
    if not t or t.id != session.get("tenant_id") or t.pin_ver != session.get("pin_ver"):
        for k in ("room_id", "tenant_id", "pin_ver"):
            session.pop(k, None)
        return None, None
    return room, t


def client_ip():
    return (request.headers.get("X-Real-IP")
            or request.headers.get("X-Forwarded-For", "").split(",")[0].strip()
            or request.remote_addr or "?")


def recent_fails(key, **delta):
    since = tw_now() - timedelta(**delta)
    return LoginFail.query.filter(LoginFail.ip == key, LoginFail.at >= since).count()


def login_blocked():
    if get_setting("tenant_login_locked") == "1":
        return "房客登入暫時關閉，請聯絡房東。"
    if recent_fails(client_ip(), hours=1) >= IP_MAX_FAILS:
        return "錯誤太多次，請一小時後再試，或聯絡房東。"
    return None


def record_fail():
    db.session.add(LoginFail(ip=client_ip()))
    q = LoginFail.query.filter(LoginFail.ip.notlike("owner:%"),
                               LoginFail.ip.notlike("change:%"))
    reset_at = get_setting("fails_reset_at")
    if reset_at:
        q = q.filter(LoginFail.at >= datetime.fromisoformat(reset_at))
    if q.count() + 1 >= GLOBAL_MAX_FAILS:
        set_setting("tenant_login_locked", "1")
    db.session.commit()
    return max(IP_MAX_FAILS - recent_fails(client_ip(), hours=1), 0)


def _login_tenant(room, tenant, msg=None):
    session.permanent = True
    session.pop("bind", None)
    session["room_id"] = room.id
    session["tenant_id"] = tenant.id
    session["pin_ver"] = tenant.pin_ver
    if msg:
        flash(msg, "ok")
    return redirect(url_for("tenant_home"))


def tenant_or_login():
    room, tenant = tenant_required()
    if not room:
        return None, None, redirect(url_for("tenant_login"))
    return room, tenant, None


@app.route("/tenant", methods=["GET", "POST"])
def tenant_login():
    if tenant_required()[0]:
        return redirect(url_for("tenant_home"))
    error = None
    if request.method == "POST":
        error = login_blocked()
        code = request.form.get("code", "").strip()
        if not error:
            room = find_room(code)
            if not room:
                left = record_fail()
                error = (f"密碼不對，還可以再試 {left} 次。" if left
                         else "錯誤太多次，請一小時後再試，或聯絡房東。")
            elif room.tenant:
                return _login_tenant(room, room.tenant)
            else:
                session["bind"] = {"room_id": room.id, "code": code,
                                   "at": tw_now().isoformat()}
                return redirect(url_for("tenant_bind"))
    return render_template("tenant_login.html", error=error)


@app.route("/tenant/bind", methods=["GET", "POST"])
def tenant_bind():
    info = session.get("bind")
    room = db.session.get(Room, info["room_id"]) if info else None
    fresh = info and tw_now() - datetime.fromisoformat(info["at"]) < timedelta(minutes=BIND_MINUTES)
    if not room or not room.active or not fresh or not room.matches(info["code"]):
        session.pop("bind", None)
        flash("請重新輸入房東提供的密碼。", "warn")
        return redirect(url_for("tenant_login"))
    if room.tenant:
        session.pop("bind", None)
        flash("這間房已經有人綁定，請聯絡房東。", "warn")
        return redirect(url_for("tenant_login"))
    error = None
    if request.method == "POST":
        name = request.form.get("name", "").strip()
        checkin = to_date(request.form.get("checkin"))
        if not name or not checkin:
            error = "請填寫姓名和入住日期。"
        else:
            tenant = Tenant(room_id=room.id, name=name, checkin=checkin,
                            phone=request.form.get("phone", "").strip())
            db.session.add(tenant)
            db.session.commit()
            return _login_tenant(room, tenant, "綁定完成，下次打開網址會直接進入你的房間。")
    return render_template("tenant_bind.html", room=room, error=error, form=request.form)


@app.route("/tenant/logout", methods=["POST"])
def tenant_logout():
    for k in ("room_id", "tenant_id", "pin_ver", "bind"):
        session.pop(k, None)
    return redirect(url_for("tenant_login"))


@app.route("/tenant/home")
def tenant_home():
    room, tenant, go = tenant_or_login()
    if go:
        return go
    period = this_period()
    bill = Bill.query.filter_by(tenant_id=tenant.id, period=period).first()
    this_items = bill.items if bill else []
    older = [i for i in unpaid_items(tenant) if not bill or i.bill_id != bill.id]
    payable = [i for i in this_items + older if i.state == "unpaid"]
    if not any(i.amount > 0 for i in payable):
        payable = []
    history = (Payment.query.filter_by(tenant_id=tenant.id)
               .order_by(Payment.created_at.desc()).limit(36).all())
    return render_template("tenant_home.html", room=room, tenant=tenant, period=period,
                           bill=bill, this_items=this_items, older=older,
                           payable_total=sum(i.amount for i in payable),
                           due=tenant.due_date(period), history=history)


@app.route("/tenant/profile", methods=["POST"])
def tenant_profile():
    room, tenant, go = tenant_or_login()
    if go:
        return go
    name = request.form.get("name", "").strip()
    if name:
        tenant.name = name
    tenant.phone = request.form.get("phone", "").strip()
    db.session.commit()
    flash("個人資料已儲存。", "ok")
    return redirect(url_for("tenant_home"))


@app.route("/tenant/password", methods=["GET", "POST"])
def tenant_password():
    room, tenant, go = tenant_or_login()
    if go:
        return go
    error = None
    if request.method == "POST":
        f = request.form
        new, new2 = f.get("code", "").strip(), f.get("code2", "").strip()
        key = f"change:{tenant.id}"
        if recent_fails(key, days=1) >= CHANGE_MAX_REJECTS:
            error = "今天修改密碼的次數太多，請明天再試，或請房東重設。"
        elif not room.matches(f.get("old_code", "").strip()):
            db.session.add(LoginFail(ip=key))
            db.session.commit()
            error = "目前的密碼不對。"
        elif not (new.isdigit() and len(new) == 4):
            error = "新密碼要是四位數字。"
        elif new != new2:
            error = "兩次輸入的新密碼不一樣。"
        elif new in WEAK_CODES:
            error = "這組密碼太容易被猜到，請換一組。"
        elif room.matches(new):
            error = "新密碼和目前的密碼一樣。"
        elif find_room(new, exclude_room_id=room.id):
            db.session.add(LoginFail(ip=key))
            db.session.commit()
            error = "這組密碼無法使用，請換一組。"
        else:
            room.code, room.code_hash = None, generate_password_hash(new)
            tenant.pin_ver = (tenant.pin_ver or 0) + 1
            db.session.commit()
            session["pin_ver"] = tenant.pin_ver
            flash("密碼已變更，下次請用新密碼登入。", "ok")
            return redirect(url_for("tenant_home"))
    return render_template("tenant_password.html", room=room, error=error)


@app.route("/tenant/rent")
@app.route("/tenant/elec")
def tenant_old_pages():
    return redirect(url_for("tenant_home"))


@app.route("/tenant/pay", methods=["GET", "POST"])
def tenant_pay():
    room, tenant, go = tenant_or_login()
    if go:
        return go
    payable = [i for i in unpaid_items(tenant) if i.state == "unpaid"]
    if not any(i.amount > 0 for i in payable):
        payable = []
    pending = (Payment.query.filter(Payment.tenant_id == tenant.id,
                                    Payment.status == "待確認").all())
    pending = [p for p in pending if p.is_bill]
    errors = {}
    if request.method == "POST":
        ids = {to_int(x) for x in request.form.getlist("item")}
        chosen = [i for i in payable if i.id in ids and i.amount > 0]
        if chosen:  # 溢繳折抵一律一起結算
            chosen += [i for i in payable if i.amount < 0]
        if not chosen:
            errors["item"] = "請勾選要繳的項目"
        fields = read_payment_fields(request.form, errors)
        if not errors:
            pay = Payment(tenant_id=tenant.id, room_id=room.id, period=this_period(),
                          kind="b" + secrets.token_hex(3),
                          rent_amount=sum(i.amount for i in chosen if i.kind == "rent"),
                          elec_amount=sum(i.amount for i in chosen if i.kind == "elec"),
                          status="待確認", **fields)
            db.session.add(pay)
            db.session.flush()
            for i in chosen:
                i.payment_id = pay.id
            db.session.commit()
            lines = "\n".join(f"・{i.label} {i.amount:,} 元" for i in chosen)
            sent = notify_landlords(
                f"【繳費通知】{room.name} {tenant.name}\n付款 {pay.total:,} 元（應付 {pay.due:,} 元）\n"
                f"{lines}\n{pay.pay_info}\n\n確認收款：{site_url('owner_home')}", "pay_notice")
            flash("已通知房東，等待房東確認。" if sent or not line_api.enabled()
                  else "已送出，等待房東確認。", "ok")
            return redirect(url_for("tenant_home"))
    return render_template("tenant_billpay.html", room=room, tenant=tenant,
                           payable=payable, pending=pending, errors=errors,
                           form=request.form if request.method == "POST" else None,
                           prev=prev_payment_info(tenant))


@app.route("/tenant/pay/<int:pid>/cancel", methods=["POST"])
def tenant_pay_cancel(pid):
    room, tenant, go = tenant_or_login()
    if go:
        return go
    p = db.get_or_404(Payment, pid)
    if p.tenant_id != tenant.id or p.confirmed or not p.is_bill:
        abort(404)
    for i in p.items:
        i.payment_id = None
    db.session.delete(p)
    db.session.commit()
    flash("已取消這次的付款通知，可以重新填寫。", "ok")
    return redirect(url_for("tenant_pay"))


# ================================================================ 屋主端（房東與管理者）
ROLE_LABEL = {"admin": "管理者", "landlord": "房東"}


def _role_ver(role):
    return get_setting(f"{role}_ver", "0") if role == "landlord" else get_setting("owner_ver", "0")


def owner_required():
    role = session.get("role")
    return (session.get("owner") is True and role in ROLE_LABEL
            and session.get("owner_ver") == _role_ver(role))


def admin_required():
    return owner_required() and session.get("role") == "admin"


def admin_password_ok(pw):
    saved = get_setting("owner_pw_hash")
    if saved:
        return check_password_hash(saved, pw)
    return hmac.compare_digest(pw, OWNER_PASSWORD)


def landlord_password_ok(pw):
    saved = get_setting("landlord_pw_hash")
    return bool(saved) and check_password_hash(saved, pw)


def owner_guard():
    if not owner_required():
        return redirect(url_for("owner_login"))
    return None


def admin_guard():
    if not owner_required():
        return redirect(url_for("owner_login"))
    if not admin_required():
        flash("這個功能只有管理者可以使用。", "warn")
        return redirect(url_for("owner_home"))
    return None


@app.route("/owner/login", methods=["GET", "POST"])
def owner_login():
    error = None
    if request.method == "POST":
        key = f"owner:{client_ip()}"
        fails = recent_fails(key, hours=1)
        pw = request.form.get("password", "")
        role = None
        if fails >= OWNER_MAX_FAILS:
            error = "錯誤太多次，請一小時後再試。"
        elif admin_password_ok(pw):
            role = "admin"
        elif landlord_password_ok(pw):
            role = "landlord"
        if role:
            session.permanent = True
            session["owner"] = True
            session["role"] = role
            session["owner_ver"] = _role_ver(role)
            return redirect(url_for("owner_home"))
        if not error:
            db.session.add(LoginFail(ip=key))
            db.session.commit()
            left = OWNER_MAX_FAILS - fails - 1
            error = f"密碼不對，還可以再試 {left} 次。" if left else "錯誤太多次，請一小時後再試。"
    return render_template("owner_login.html", error=error,
                           default_pw=(OWNER_PASSWORD == "admin" and not get_setting("owner_pw_hash")))


def strong_password_problem(new, new2):
    if len(new) < 10 or new.isdigit() or new.isalpha():
        return "新密碼至少 10 個字元，而且要同時有英文和數字。"
    if new != new2:
        return "兩次輸入的新密碼不一樣。"
    return None


@app.route("/owner/password", methods=["GET", "POST"])
def owner_password():
    if (g := owner_guard()):
        return g
    role = session.get("role")
    error = None
    if request.method == "POST":
        f = request.form
        new, new2 = f.get("new", ""), f.get("new2", "")
        ok = admin_password_ok if role == "admin" else landlord_password_ok
        if not ok(f.get("old", "")):
            error = "目前的密碼不對。"
        elif (error := strong_password_problem(new, new2)):
            pass
        elif role == "admin" and landlord_password_ok(new) or role == "landlord" and admin_password_ok(new):
            error = "房東和管理者的密碼不能相同。"
        else:
            key_hash, key_ver = ("owner_pw_hash", "owner_ver") if role == "admin" else ("landlord_pw_hash", "landlord_ver")
            ver = str(int(get_setting(key_ver, "0")) + 1)
            set_setting(key_hash, generate_password_hash(new))
            set_setting(key_ver, ver)
            db.session.commit()
            session["owner_ver"] = ver
            flash(f"{ROLE_LABEL[role]}密碼已變更，其他裝置需要用新密碼重新登入。", "ok")
            return redirect(url_for("owner_home"))
    return render_template("owner_password.html", error=error, role_label=ROLE_LABEL.get(role, ""))


@app.route("/owner/logout", methods=["POST"])
def owner_logout():
    for k in ("owner", "owner_ver", "role"):
        session.pop(k, None)
    return redirect(url_for("index"))


def room_status(room, period):
    """回傳 (文字, 樣式, 待確認的繳費 id)。樣式：empty none due late wait ok"""
    t = room.tenant
    if not t:
        return ("空房", "empty", None)
    pending = (Payment.query.filter_by(tenant_id=t.id, status="待確認")
               .order_by(Payment.id).first())
    if pending:
        return ("繳費待確認", "wait", pending.id)
    unpaid = [i for i in unpaid_items(t) if i.state == "unpaid"]
    total = sum(i.amount for i in unpaid)
    if total > 0 and any(i.amount > 0 for i in unpaid):
        late = any(i.month < period and i.amount > 0 for i in unpaid) or tw_today() > t.due_date(period)
        return ((f"逾期未繳 {total:,} 元" if late else f"費用待繳 {total:,} 元"),
                "late" if late else "due", None)
    if not Bill.query.filter_by(tenant_id=t.id, period=period).first():
        return ("尚未發繳費單", "none", None)
    return ("本月已繳清", "ok", None)


@app.route("/owner")
def owner_home():
    if (g := owner_guard()):
        return g
    period = this_period()
    rooms = Room.query.filter_by(active=True).order_by(Room.sort, Room.id).all()
    status = {r.id: room_status(r, period) for r in rooms}
    pending = (Payment.query.join(Room).filter(Payment.status == "待確認")
               .order_by(Payment.paid_date, Payment.id).all())
    recent = (Payment.query.filter_by(status="已確認")
              .order_by(Payment.confirmed_at.desc()).limit(10).all())
    inactive = Room.query.filter_by(active=False).order_by(Room.sort).all()
    month_rows = Payment.query.filter_by(period=period).all()
    income = {
        "confirmed": sum(p.received or 0 for p in month_rows if p.confirmed),
        "confirmed_n": sum(1 for p in month_rows if p.confirmed),
        "pending": sum(p.total or 0 for p in month_rows if not p.confirmed),
        "pending_n": sum(1 for p in month_rows if not p.confirmed),
        "expected": sum(r.rent or 0 for r in rooms if r.tenant),
    }
    unbilled = sum(1 for r in rooms if r.tenant and not
                   Bill.query.filter_by(tenant_id=r.tenant.id, period=period).first())
    entry = public_url(url_for("tenant_login"))
    return render_template("owner_home.html", unbilled=unbilled,
                           groups=group_by_building(rooms), rooms=rooms, status=status, period=period,
                           pending=pending, recent=recent, inactive=inactive, income=income,
                           rate=get_setting("elec_rate", "0"),
                           login_locked=get_setting("tenant_login_locked") == "1",
                           entry=entry, qr=qr_svg(entry))


@app.route("/owner/bills", methods=["GET", "POST"])
def owner_bills():
    if (g := owner_guard()):
        return g
    period = this_period()
    rooms = [r for r in Room.query.filter_by(active=True).order_by(Room.sort, Room.id).all()
             if r.tenant]
    f = request.form
    elec_month = f.get("elec_month") or request.args.get("elec_month") or period
    months = [period, prev_month(period), prev_month(prev_month(period))]
    if elec_month not in months:
        elec_month = period

    if request.method == "POST":
        try:
            rate = float(f.get("elec_rate", ""))
            if rate < 0:
                raise ValueError
        except ValueError:
            flash("台電公告電價要填數字。", "warn")
            return redirect(url_for("owner_bills", elec_month=elec_month))
        set_setting("elec_rate", f"{rate:g}")
        sent, skipped = [], []
        for r in rooms:
            t = r.tenant
            rent = max(to_int(f.get(f"rent_{r.id}"), 0), 0)
            kwh = max(to_int(f.get(f"kwh_{r.id}"), 0), 0)
            elec = max(to_int(f.get(f"elec_{r.id}"), 0), 0)
            if not elec and kwh:
                elec = round(kwh * rate)
            if not rent and not elec:
                continue
            bill = Bill.query.filter_by(tenant_id=t.id, period=period).first()
            if not bill:
                bill = Bill(tenant_id=t.id, room_id=r.id, period=period)
                db.session.add(bill)
                db.session.flush()
            for kind, month, amount, note in [
                    ("rent", period, rent, ""),
                    ("elec", elec_month, elec, f.get(f"note_{r.id}", "").strip()[:60])]:
                extra = {"kwh": kwh or None, "rate": rate} if kind == "elec" else {}
                item = BillItem.query.filter_by(tenant_id=t.id, kind=kind, month=month).first()
                if item and item.state != "unpaid":
                    if amount:
                        skipped.append(f"{r.name} {item.label}")
                    continue
                if not amount:
                    continue
                if item:
                    item.amount, item.note = amount, note or item.note
                    for k, v in extra.items():
                        setattr(item, k, v)
                else:
                    db.session.add(BillItem(bill_id=bill.id, tenant_id=t.id, kind=kind,
                                            month=month, amount=amount, note=note, **extra))
            sent.append(r.name)
        db.session.commit()
        if sent:
            flash(f"已發送繳費單給 {len(sent)} 間：{'、'.join(sent)}。", "ok")
            if request.form.get("announce", "1") == "1":
                if notify_group(f"{month_label(period)}份繳費單(含電費)已發出，請打開連結查看並繳費\n"
                                f"{site_url('tenant_login')}", "bill_group"):
                    flash("已在公告群組發出通知。", "ok")
                elif line_api.enabled() and not get_setting("line_group_id"):
                    flash("尚未設定公告群組，這次沒有發出群組通知。", "warn")
        else:
            flash("沒有填寫任何金額，沒有發送。", "warn")
        if skipped:
            flash(f"這些項目已付款或已通知房東，沒有更改：{'、'.join(skipped)}。", "warn")
        return redirect(url_for("owner_bills", elec_month=elec_month))

    rows = []
    for r in rooms:
        t = r.tenant
        bill = Bill.query.filter_by(tenant_id=t.id, period=period).first()
        rent_item = BillItem.query.filter_by(tenant_id=t.id, kind="rent", month=period).first()
        elec_item = BillItem.query.filter_by(tenant_id=t.id, kind="elec", month=elec_month).first()
        rows.append({"room": r, "tenant": t, "bill": bill,
                     "rent_item": rent_item, "elec_item": elec_item,
                     "carry": [i for i in unpaid_items(t)
                               if i.month < period and not (i.kind == "elec" and i.month == elec_month)]})
    grouped = {}
    for row in rows:
        grouped.setdefault(row["room"].building, []).append(row)
    order = buildings()
    grouped = sorted(grouped.items(), key=lambda kv: order.index(kv[0]) if kv[0] in order else 99)
    return render_template("owner_bills.html", rows=rows, grouped=grouped, period=period,
                           elec_month=elec_month, months=months,
                           rate=get_setting("elec_rate", "0"))


@app.route("/owner/bill-item/<int:iid>/delete", methods=["POST"])
def owner_bill_item_delete(iid):
    if (g := owner_guard()):
        return g
    item = db.get_or_404(BillItem, iid)
    back = request.form.get("back")
    room_id = item.bill.room_id
    if item.state != "unpaid":
        flash("這個項目已付款或房客已通知房東，不能刪除。", "warn")
    else:
        bill = item.bill
        label = item.label
        db.session.delete(item)
        db.session.flush()
        if not bill.items:
            db.session.delete(bill)
        db.session.commit()
        flash(f"已刪除 {label}。", "ok")
    if back == "room":
        return redirect(url_for("owner_room", rid=room_id))
    return redirect(url_for("owner_bills"))


@app.route("/owner/reopen", methods=["POST"])
def owner_reopen():
    if (g := admin_guard()):
        return g
    set_setting("tenant_login_locked", "0")
    set_setting("fails_reset_at", tw_now().isoformat())
    db.session.commit()
    flash("房客登入已重新開放。", "ok")
    return redirect(url_for("owner_home"))


def building_from_form(f, errors):
    b = f.get("building", "")
    if b == "__new__":
        b = f.get("new_building", "").strip()
    if not b:
        errors["building"] = "請選擇或輸入物件名稱"
    elif len(b) > 20:
        errors["building"] = "物件名稱最多 20 個字"
    return b


@app.route("/owner/rooms/new", methods=["GET", "POST"])
def owner_room_new():
    if (g := admin_guard()):
        return g
    errors = {}
    f = request.form
    if request.method == "POST":
        b = building_from_form(f, errors)
        name = f.get("name", "").strip()
        rtype = f.get("room_type", "")
        if not name:
            errors["name"] = "請填房號或名稱"
        elif len(name) > 20:
            errors["name"] = "名稱最多 20 個字"
        elif Room.query.filter_by(name=name).first():
            errors["name"] = "已經有同名的房間（包含已停用的）"
        if rtype not in room_types():
            errors["room_type"] = "請選擇房型"
        if not errors:
            last = Room.query.order_by(Room.sort.desc()).first()
            room = Room(building=b, name=name, room_type=rtype,
                        sort=(last.sort + 1) if last else 0,
                        rent=to_int(f.get("rent"), 0),
                        deposit_months=to_int(f.get("deposit_months"), 2),
                        meter_start=to_int(f.get("meter_start"), 0), active=True)
            room.set_owner_code(new_code())
            db.session.add(room)
            db.session.commit()
            flash(f"已新增 {name}，房客密碼是 {room.code}。", "ok")
            return redirect(url_for("owner_room", rid=room.id))
    return render_template("owner_room_new.html", errors=errors,
                           form=f if request.method == "POST" else None,
                           buildings=buildings(), types=room_types())


@app.route("/owner/room-types", methods=["POST"])
def owner_room_types():
    if (g := admin_guard()):
        return g
    types = room_types()
    name = request.form.get("name", "").strip()
    if request.form.get("action") == "add":
        if not name or len(name) > 10:
            flash("房型名稱請填 1 到 10 個字。", "warn")
        elif name in types:
            flash("這個房型已經存在。", "warn")
        else:
            types.append(name)
            flash(f"已加入房型「{name}」。", "ok")
    elif request.form.get("action") == "remove" and name in types:
        if Room.query.filter_by(room_type=name).first():
            flash(f"還有房間使用「{name}」，請先修改那些房間的房型。", "warn")
        elif len(types) <= 1:
            flash("至少要保留一個房型。", "warn")
        else:
            types.remove(name)
            flash(f"已刪除房型「{name}」。", "ok")
    set_setting("room_types", json.dumps(types, ensure_ascii=False))
    db.session.commit()
    return redirect(url_for("owner_room_new") + "#types")


@app.route("/owner/room/<int:rid>", methods=["GET", "POST"])
def owner_room(rid):
    if (g := owner_guard()):
        return g
    room = db.get_or_404(Room, rid)
    tenant = room.tenant
    errors = {}
    if request.method == "POST":
        if (g := admin_guard()):
            return g
        action = request.form.get("action")
        f = request.form
        if action == "save":
            b = building_from_form(f, errors)
            rtype = f.get("room_type", room.room_type)
            if rtype not in room_types():
                errors["room_type"] = "請選擇房型"
            if errors:
                flash("；".join(errors.values()), "warn")
                return redirect(url_for("owner_room", rid=room.id))
            room.building, room.room_type = b, rtype
            room.rent = to_int(f.get("rent"), room.rent)
            room.deposit_months = to_int(f.get("deposit_months"), 2)
            room.meter_start = to_int(f.get("meter_start"), room.meter_start)
            if tenant:
                tenant.name = f.get("name", tenant.name).strip() or tenant.name
                tenant.phone = f.get("phone", "").strip()
                tenant.checkin = to_date(f.get("checkin")) or tenant.checkin
                pd = to_int(f.get("pay_day"), 0)
                tenant.pay_day = pd if 1 <= pd <= 28 else None
            flash("已儲存。", "ok")
        elif action == "verify" and tenant:
            tenant.verified = True
            flash(f"已確認 {tenant.name} 是這間房的房客。", "ok")
        elif action == "new_code":
            room.set_owner_code(new_code(room.id))
            if tenant:
                tenant.pin_ver = (tenant.pin_ver or 0) + 1
            flash(f"{room.name} 的新密碼是 {room.code}，舊密碼已失效，房客要用新密碼重新登入。", "ok")
        elif action == "moveout" and tenant:
            tenant.active = False
            tenant.moved_out_at = tw_now()
            tenant.pin_ver = (tenant.pin_ver or 0) + 1
            LineUser.query.filter_by(tenant_id=tenant.id).delete()
            room.set_owner_code(new_code(room.id))
            flash(f"已解除 {tenant.name} 的綁定，房間變回空房，繳費紀錄已保留。新房客請用密碼 {room.code}。", "ok")
            left = Payment.query.filter_by(tenant_id=tenant.id, status="待確認").count()
            if left:
                flash(f"{tenant.name} 還有 {left} 筆待確認的回報，仍會顯示在總覽，請確認收款或刪除。", "warn")
        elif action == "delete":
            used = Tenant.query.filter_by(room_id=room.id).first() or \
                Payment.query.filter_by(room_id=room.id).first() or \
                Bill.query.filter_by(room_id=room.id).first()
            if used:
                flash("這間房有房客或繳費紀錄，不能刪除，只能停用。", "warn")
            else:
                name = room.name
                db.session.delete(room)
                db.session.commit()
                flash(f"已刪除 {name}。", "ok")
                return redirect(url_for("owner_home"))
        elif action == "deactivate":
            if tenant:
                flash("請先解除房客綁定，才能停用這間房。", "warn")
            else:
                room.active = False
                flash(f"已停用 {room.name}，資料會保留，不再出現在總覽。", "ok")
        elif action == "activate":
            room.active = True
            room.set_owner_code(new_code(room.id))
            flash(f"已重新啟用 {room.name}，房客密碼是 {room.code}。", "ok")
        db.session.commit()
        return redirect(url_for("owner_room", rid=room.id))

    history = []
    if tenant:
        history = (Payment.query.filter_by(tenant_id=tenant.id)
                   .order_by(Payment.period.desc(), Payment.id.desc()).all())
    past_pending = (Payment.query.join(Tenant)
                    .filter(Payment.room_id == room.id, Payment.status == "待確認",
                            Tenant.active.is_(False)).all())
    past = (Tenant.query.filter_by(room_id=room.id, active=False)
            .order_by(Tenant.moved_out_at.desc()).all())
    open_items = unpaid_items(tenant) if tenant else []
    deletable = not (Tenant.query.filter_by(room_id=room.id).first()
                     or Payment.query.filter_by(room_id=room.id).first())
    return render_template("owner_room.html", room=room, tenant=tenant, history=history,
                           past=past, past_pending=past_pending, deletable=deletable,
                           open_items=open_items, buildings=buildings(),
                           types=room_types(),
                           status=room_status(room, this_period()) if room.active else None)


def _add_diff_item(p, amount):
    """在房客本期繳費單加一筆差額（正數＝待補，負數＝溢繳折抵）。"""
    period = this_period()
    bill = Bill.query.filter_by(tenant_id=p.tenant_id, period=period).first()
    if not bill:
        bill = Bill(tenant_id=p.tenant_id, room_id=p.room_id, period=period)
        db.session.add(bill)
        db.session.flush()
    db.session.add(BillItem(bill_id=bill.id, tenant_id=p.tenant_id, kind="diff",
                            month=p.period, amount=amount, source_payment_id=p.id,
                            note=f"{p.paid_date:%m/%d} 付款{'少' if amount > 0 else '多'} {abs(amount):,} 元"
                            if p.paid_date else ""))


@app.route("/owner/pay/<int:pid>", methods=["POST"])
def owner_pay(pid):
    if (g := owner_guard()):
        return g
    p = db.get_or_404(Payment, pid)
    action = request.form.get("action")
    back = request.form.get("back")
    if action == "confirm":
        if p.confirmed:
            flash("這筆款項已經確認過了。", "warn")
        else:
            received = to_int(request.form.get("received"), p.total)
            diff = received - p.due
            choice = request.form.get("diff_choice", "")
            if diff and p.is_bill and choice not in ("carry", "waive", "credit", "ignore"):
                flash("實收金額和應付金額不同，請選擇差額的處理方式。", "warn")
                return redirect(url_for("owner_room", rid=p.room_id) if back == "room"
                                else url_for("owner_home") + f"#pay{p.id}")
            p.received = received
            p.confirm_note = request.form.get("note", "").strip()
            p.status = "已確認"
            p.confirmed_at = tw_now()
            for i in p.items:
                i.paid, i.paid_at = True, p.confirmed_at
            msg = f"{p.room.name} {p.kind_label} 已確認收款 {received:,} 元。"
            if diff < 0 and choice == "carry":
                _add_diff_item(p, -diff)
                msg += f" 少收的 {-diff:,} 元已轉到下期。"
            elif diff < 0 and choice == "waive":
                p.confirm_note = (p.confirm_note + f"｜免收差額 {-diff:,}").strip("｜")
                msg += f" 少收的 {-diff:,} 元不再追收。"
            elif diff > 0 and choice == "credit":
                _add_diff_item(p, -diff)
                msg += f" 多收的 {diff:,} 元會折抵下期。"
            elif diff > 0:
                p.confirm_note = (p.confirm_note + f"｜多收 {diff:,} 不處理").strip("｜")
            db.session.commit()
            flash(msg, "ok")
            if diff < 0 and choice == "carry":
                flash("請提醒房客到繳費單查看差額。", "warn")
    elif action == "delete":
        if p.confirmed:
            flash("已確認的款項不能刪除，請先取消確認。", "warn")
        else:
            label = f"{p.room.name} {p.period} {p.kind_label}（{p.tenant.name}）"
            room_id = p.room_id
            for i in p.items:
                i.payment_id = None
            db.session.delete(p)
            db.session.commit()
            flash(f"已刪除 {label} 的回報。", "ok")
            return redirect(url_for("owner_room", rid=room_id) if back == "room" else url_for("owner_home"))
    elif action == "undo":
        derived = BillItem.query.filter_by(source_payment_id=p.id).all()
        if any(d.state != "unpaid" for d in derived):
            flash("這筆付款產生的差額已經被結算，不能取消確認。", "warn")
        else:
            for d in derived:
                db.session.delete(d)
            p.status = "待確認"
            for i in p.items:
                i.paid, i.paid_at = False, None
            p.confirm_note = (p.confirm_note + f"｜{tw_now():%m/%d %H:%M} 取消確認").strip("｜")
            p.confirmed_at = None
            db.session.commit()
            flash(f"{p.room.name} {p.kind_label} 已取消確認。", "warn")
    db.session.commit()
    return redirect(url_for("owner_room", rid=p.room_id) if back == "room" else url_for("owner_home"))


@app.route("/owner/settings", methods=["POST"])
def owner_settings():
    if (g := admin_guard()):
        return g
    try:
        rate = float(request.form.get("elec_rate", ""))
        if rate < 0:
            raise ValueError
        set_setting("elec_rate", f"{rate:g}")
        db.session.commit()
        flash(f"電費單價已改為每度 {rate:g} 元，之後送出的回報才會用新單價。", "ok")
    except ValueError:
        flash("電費單價要填數字。", "warn")
    return redirect(url_for("owner_home"))


# ================================================================ LINE Bot
def make_bind_code(kind):
    code = f"{secrets.randbelow(1000000):06d}"
    set_setting(f"bind_{kind}", json.dumps({"code": code, "until": (tw_now() + timedelta(minutes=BIND_CODE_MINUTES)).isoformat()}))
    db.session.commit()
    return code


def use_bind_code(kind, code):
    try:
        info = json.loads(get_setting(f"bind_{kind}", "") or "{}")
    except ValueError:
        return False
    if not info or tw_now() > datetime.fromisoformat(info["until"]):
        return False
    if not hmac.compare_digest(info["code"], code):
        return False
    set_setting(f"bind_{kind}", "")
    return True


def help_text():
    contact = get_setting("landlord_contact", "").strip()
    return ("這是租屋小幫手的自動通知帳號，無法回覆訊息。\n\n"
            f"查看繳費單、繳費：{site_url('tenant_login')}\n"
            "繳費通知都會發在住戶群組，不需要另外綁定。\n\n"
            + (f"其他問題請聯絡房東：{contact}" if contact else "其他問題請直接聯絡房東。"))


def handle_line_event(ev):
    etype = ev.get("type")
    src = ev.get("source", {})
    stype = src.get("type")
    uid = src.get("userId", "")
    token_ = ev.get("replyToken")
    reply = (lambda t: line_api.reply(token_, t)) if token_ else (lambda t: None)

    if etype == "follow" and uid:
        reply("歡迎使用租屋小幫手！\n\n" + help_text())
        return
    if etype == "unfollow" and uid:
        LineUser.query.filter_by(user_id=uid).delete()
        db.session.commit()
        return
    if etype == "join" and stype in ("group", "room"):
        if not get_setting("line_group_id"):
            reply("大家好，我是租屋小幫手。\n請管理者在這裡傳：設定公告群組 六位數設定碼")
        return
    if etype == "leave" and stype in ("group", "room"):
        gid = src.get("groupId") or src.get("roomId")
        if gid == get_setting("line_group_id"):
            set_setting("line_group_id", "")
            db.session.commit()
            alert_admin("租屋小幫手已被移出公告群組，群組通知暫停。")
        return
    if etype != "message" or ev.get("message", {}).get("type") != "text":
        return
    text_ = ev["message"]["text"].strip()
    parts = text_.split()
    cmd = parts[0] if parts else ""
    arg = parts[1] if len(parts) > 1 else ""

    if stype in ("group", "room"):
        gid = src.get("groupId") or src.get("roomId")
        if cmd == "設定公告群組":
            if use_bind_code("group", arg):
                set_setting("line_group_id", gid)
                db.session.commit()
                reply("已設定為公告群組，之後繳費單發出時會在這裡通知大家。")
            else:
                reply("設定碼不正確或已過期，請管理者重新產生。")
        elif cmd == "綁定":
            reply("房客不需要綁定，通知都會發在這個群組。\n"
                  "提醒：房間密碼請勿傳到群組，若已傳出請聯絡房東更換。")
        elif cmd in ("房東", "管理者"):
            reply("請私訊租屋小幫手完成綁定，不要在群組傳綁定碼。")
        return

    if stype != "user" or not uid:
        return
    if cmd == "綁定":
        reply("房客不需要綁定，繳費通知都會發在住戶群組。\n\n"
              f"查看繳費單、繳費：{site_url('tenant_login')}\n\n"
              "提醒：請不要把房間密碼傳給任何人。")
        return
    if cmd in ("房東", "管理者"):
        kind = "landlord" if cmd == "房東" else "admin"
        key = f"line:{uid}"
        if recent_fails(key, hours=1) >= IP_MAX_FAILS:
            reply("錯誤太多次，請一小時後再試。")
        elif use_bind_code(kind, arg):
            LineUser.query.filter_by(user_id=uid).delete()
            db.session.add(LineUser(user_id=uid, role=kind))
            db.session.commit()
            reply(f"已綁定為{ROLE_LABEL[kind]}。" + ("房客付款時會通知你，每天早上也會收到待辦摘要。"
                                                  if kind == "landlord" else "系統異常時會通知你。"))
            if kind == "landlord":
                alert_admin("有新的房東完成 LINE 綁定。")
        else:
            db.session.add(LoginFail(ip=key))
            db.session.commit()
            reply("綁定碼不正確或已過期，請重新產生。")
        return
    if cmd == "解除綁定":
        n = LineUser.query.filter_by(user_id=uid).delete()
        db.session.commit()
        reply("已解除綁定，不會再收到個人通知。" if n else "你目前沒有綁定。")
        return
    reply(help_text())


@app.route("/line/callback", methods=["POST"])
def line_callback():
    body = request.get_data()
    if not line_api.verify_signature(body, request.headers.get("X-Line-Signature", "")):
        abort(400)
    try:
        events = json.loads(body.decode("utf-8")).get("events", [])
    except ValueError:
        abort(400)
    for ev in events:
        try:
            handle_line_event(ev)
        except Exception as e:  # 單一事件出錯不影響其他事件
            db.session.rollback()
            app.logger.exception("LINE event error")
            alert_admin(f"處理 LINE 訊息時發生錯誤：{type(e).__name__}")
    return "OK"


# ---------------------------------------------------------------- 每日排程
def daily_job(today):
    """每天檢查：群組提醒房客、私訊摘要給房東。"""
    period = today.strftime("%Y-%m")
    soon, late_rooms, unbilled_soon = [], [], []
    for room in Room.query.filter_by(active=True).order_by(Room.sort).all():
        t = room.tenant
        if not t:
            continue
        due = t.due_date(period)
        days = (due - today).days
        if not Bill.query.filter_by(tenant_id=t.id, period=period).first() and 0 <= days <= 3:
            unbilled_soon.append(f"{room.name}（{due.month}/{due.day}）")
        unpaid = [i for i in unpaid_items(t) if i.state == "unpaid"]
        if sum(i.amount for i in unpaid) <= 0:
            continue
        if days < 0 or any(i.month < period and i.amount > 0 for i in unpaid):
            late_rooms.append(room.name)
        elif 0 <= days <= 3:
            soon.append(room.name)

    pending = Payment.query.filter_by(status="待確認").count()
    lines = []
    if pending:
        lines.append(f"・待確認收款 {pending} 筆")
    if late_rooms:
        lines.append(f"・逾期未繳：{'、'.join(late_rooms)}")
    if soon:
        lines.append(f"・3 天內到期：{'、'.join(soon)}")
    if unbilled_soon:
        lines.append(f"・快到繳費日但還沒發繳費單：{'、'.join(unbilled_soon)}")
    if lines:
        notify_landlords(f"【租屋小幫手・每日摘要 {today.month}/{today.day}】\n" + "\n".join(lines)
                         + f"\n\n{site_url('owner_home')}", "daily_summary")
    return {"soon": soon, "late": late_rooms, "pending": pending,
            "unbilled_soon": unbilled_soon}


@app.route("/cron/daily")
def cron_daily():
    if not CRON_KEY or not hmac.compare_digest(request.args.get("key", ""), CRON_KEY):
        abort(404)
    today = tw_today()
    if get_setting("cron_last") == today.isoformat() and request.args.get("force") != "1":
        return "already ran today"
    set_setting("cron_last", today.isoformat())
    db.session.commit()
    result = daily_job(today)
    return json.dumps(result, ensure_ascii=False)


# ---------------------------------------------------------------- 管理者：LINE 與帳號設定
@app.route("/owner/admin", methods=["GET", "POST"])
def owner_admin():
    if (g := admin_guard()):
        return g
    codes = {}
    if request.method == "POST":
        f = request.form
        action = f.get("action")
        if action in ("code_landlord", "code_admin", "code_group"):
            kind = action[5:]
            codes[kind] = make_bind_code(kind)
        elif action == "landlord_pw":
            new, new2 = f.get("new", ""), f.get("new2", "")
            if (err := strong_password_problem(new, new2)):
                flash(err, "warn")
            elif admin_password_ok(new):
                flash("房東和管理者的密碼不能相同。", "warn")
            else:
                set_setting("landlord_pw_hash", generate_password_hash(new))
                set_setting("landlord_ver", str(int(get_setting("landlord_ver", "0")) + 1))
                db.session.commit()
                flash("已設定房東密碼，房東已登入的裝置需要重新登入。", "ok")
        elif action == "contact":
            set_setting("landlord_contact", f.get("contact", "").strip()[:80])
            db.session.commit()
            flash("已更新房東聯絡方式。", "ok")
        elif action == "unbind":
            LineUser.query.filter_by(user_id=f.get("uid", "")).delete()
            db.session.commit()
            flash("已解除該 LINE 綁定。", "ok")
        elif action == "clear_group":
            set_setting("line_group_id", "")
            db.session.commit()
            flash("已清除公告群組設定。", "ok")
        elif action == "test_group":
            ok = notify_group("【租屋小幫手】這是測試訊息，群組通知運作正常。", "test")
            flash("已送出測試訊息。" if ok else "測試訊息送出失敗，請看下方推播紀錄。", "ok" if ok else "warn")
        elif action == "test_me":
            users = LineUser.query.filter_by(role="admin").all()
            ok = all(notify(u.user_id, "【租屋小幫手】管理者測試訊息。", "test") for u in users) if users else False
            flash("已送出測試訊息。" if ok else "送出失敗，或尚未綁定管理者。", "ok" if ok else "warn")
        elif action == "run_daily":
            result = daily_job(tw_today())
            flash(f"已執行每日檢查：提醒 {result['reminders']} 位房客。", "ok")
        if action and not action.startswith("code_"):
            return redirect(url_for("owner_admin"))

    users = LineUser.query.order_by(LineUser.role, LineUser.created_at).all()
    notices = Notice.query.order_by(Notice.id.desc()).limit(20).all()
    cron_last = get_setting("cron_last")
    cron_stale = not cron_last or (tw_today() - date.fromisoformat(cron_last)).days > 1
    return render_template(
        "owner_admin.html", codes=codes, users=users, notices=notices,
        line_on=line_api.enabled(), group_set=bool(get_setting("line_group_id")),
        landlord_set=bool(get_setting("landlord_pw_hash")),
        contact=get_setting("landlord_contact", ""),
        callback_url=site_url("line_callback"),
        cron_url=(site_url("cron_daily") + "?key=" + CRON_KEY) if CRON_KEY else "",
        cron_last=cron_last, cron_stale=cron_stale, minutes=BIND_CODE_MINUTES)


if __name__ == "__main__":
    app.run(debug=True, port=5000)
