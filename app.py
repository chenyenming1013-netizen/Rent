"""租屋小幫手：租屋收費管理網頁

角色：
  屋主端  /owner   用屋主密碼登入：租屋總覽、確認收款、管理與新增房間
  房客端  /tenant  輸入房東提供的四位數密碼 → 空房先填資料綁定 → 進入自己的房間
                   可分別回報月租、電費，或兩者同一筆轉帳；可自行修改密碼
"""
import hmac
import io
import json
import os
import secrets
from datetime import date, datetime, timedelta

import qrcode
import qrcode.image.svg
from flask import (Flask, abort, flash, redirect, render_template, request,
                   session, url_for)
from flask_sqlalchemy import SQLAlchemy
from markupsafe import Markup
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash

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

DEFAULT_ROOMS = [
    ("輝煌", "輝煌", "套房"),
    ("民權", "民權 A", "套房"), ("民權", "民權 B", "套房"),
    ("憲政", "憲政 01", "套房"), ("憲政", "憲政 02", "套房"),
    ("憲政", "憲政 03", "套房"), ("憲政", "憲政 04", "套房"),
]
DEFAULT_ROOM_TYPES = ["套房", "雅房", "整層", "店面", "車位", "其他"]
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
    created_at = db.Column(db.DateTime, default=datetime.now)
    moved_out_at = db.Column(db.DateTime)
    pin_hash = db.Column(db.String(255), default="")   # 舊版欄位，保留相容
    pin_ver = db.Column(db.Integer, default=1)         # 換密碼時加一，讓舊登入失效
    verified = db.Column(db.Boolean, default=False)    # 房東是否已核對

    @property
    def due_day(self):
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
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    tenant = db.relationship("Tenant")
    room = db.relationship("Room")
    __table_args__ = (db.UniqueConstraint("tenant_id", "period", "kind"),)

    @property
    def due(self):
        return (self.rent_amount or 0) + (self.elec_amount or 0) + (self.other_amount or 0)

    @property
    def kind_label(self):
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
        parts = []
        if self.has_rent:
            parts.append(f"月租 {self.rent_amount:,}")
        if self.has_elec:
            parts.append(f"電費 {self.elec_amount:,}（{self.meter_last}→{self.meter_now}，{self.usage} 度）")
        return "＋".join(parts)


class Setting(db.Model):
    key = db.Column(db.String(40), primary_key=True)
    value = db.Column(db.String(2000))


class LoginFail(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    ip = db.Column(db.String(64), index=True)
    at = db.Column(db.DateTime, default=datetime.now, index=True)


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
    return date.today().strftime("%Y-%m")


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
    return {"csrf": session["csrf"], "today": date.today(),
            "PAY_METHODS": PAY_METHODS}


@app.before_request
def check_csrf():
    if request.method == "POST":
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
    since = datetime.now() - timedelta(**delta)
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
                                   "at": datetime.now().isoformat()}
                return redirect(url_for("tenant_bind"))
    return render_template("tenant_login.html", error=error)


@app.route("/tenant/bind", methods=["GET", "POST"])
def tenant_bind():
    info = session.get("bind")
    room = db.session.get(Room, info["room_id"]) if info else None
    fresh = info and datetime.now() - datetime.fromisoformat(info["at"]) < timedelta(minutes=BIND_MINUTES)
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
    pays = period_payments(tenant, period)
    history = (Payment.query.filter_by(tenant_id=tenant.id)
               .order_by(Payment.period.desc(), Payment.id.desc()).limit(36).all())
    return render_template("tenant_home.html", room=room, tenant=tenant, period=period,
                           rent_pay=rent_record(pays), elec_pay=elec_record(pays),
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


@app.route("/tenant/pay")
def tenant_pay():
    return redirect(url_for("tenant_home"))


@app.route("/tenant/rent", methods=["GET", "POST"])
def tenant_rent():
    room, tenant, go = tenant_or_login()
    if go:
        return go
    period = this_period()
    pays = period_payments(tenant, period)
    current = rent_record(pays)
    if current and current.confirmed:
        flash(f"{period} 的月租已確認收款，不能再修改。", "warn")
        return redirect(url_for("tenant_home"))
    separate_elec = pays.get("elec")          # 電費已分開回報
    rate = float(get_setting("elec_rate", "0") or 0)
    meter_last = last_meter(room, tenant, period)
    errors = {}

    if request.method == "POST":
        f = request.form
        combine = f.get("combine") == "1" and not separate_elec
        mn = None
        if combine:
            raw = f.get("meter_now", "").strip()
            mn = to_int(raw, -1) if raw else None
            if mn is None:
                errors["meter_now"] = "請填本月電表度數"
            elif mn < meter_last:
                errors["meter_now"] = f"不能小於上期度數 {meter_last:,}"
        fields = read_payment_fields(f, errors)
        if not errors:
            pay = current or Payment(tenant_id=tenant.id, room_id=room.id, period=period)
            pay.kind = "both" if combine else "rent"
            pay.rent_amount = room.rent or 0
            if combine:
                pay.meter_last, pay.meter_now = meter_last, mn
                pay.usage, pay.rate = mn - meter_last, rate
                pay.elec_amount = round((mn - meter_last) * rate)
            else:
                pay.meter_last = pay.meter_now = None
                pay.usage, pay.rate, pay.elec_amount = 0, 0, 0
            pay.other_amount, pay.other_note = 0, ""
            for k, v in fields.items():
                setattr(pay, k, v)
            pay.status = "待確認"
            db.session.add(pay)
            db.session.commit()
            flash("已送出並通知房東，等待確認。", "ok")
            return redirect(url_for("tenant_home"))

    return render_template("tenant_rent.html", room=room, tenant=tenant, period=period,
                           pay=current, separate_elec=separate_elec, rate=rate,
                           meter_last=meter_last, errors=errors,
                           form=request.form if request.method == "POST" else None,
                           prev=prev_payment_info(tenant))


@app.route("/tenant/elec", methods=["GET", "POST"])
def tenant_elec():
    room, tenant, go = tenant_or_login()
    if go:
        return go
    period = this_period()
    pays = period_payments(tenant, period)
    if pays.get("both"):
        return render_template("tenant_elec.html", room=room, period=period,
                               combined=pays["both"])
    current = pays.get("elec")
    if current and current.confirmed:
        flash(f"{period} 的電費已確認收款，不能再修改。", "warn")
        return redirect(url_for("tenant_home"))
    rate = float(get_setting("elec_rate", "0") or 0)
    meter_last = last_meter(room, tenant, period)
    errors = {}

    if request.method == "POST":
        f = request.form
        raw = f.get("meter_now", "").strip()
        mn = to_int(raw, -1) if raw else None
        if mn is None:
            errors["meter_now"] = "請填本月電表度數"
        elif mn < meter_last:
            errors["meter_now"] = f"不能小於上期度數 {meter_last:,}"
        fields = read_payment_fields(f, errors)
        if not errors:
            pay = current or Payment(tenant_id=tenant.id, room_id=room.id,
                                     period=period, kind="elec")
            pay.rent_amount = 0
            pay.meter_last, pay.meter_now = meter_last, mn
            pay.usage, pay.rate = mn - meter_last, rate
            pay.elec_amount = round((mn - meter_last) * rate)
            for k, v in fields.items():
                setattr(pay, k, v)
            pay.status = "待確認"
            db.session.add(pay)
            db.session.commit()
            flash("電費已送出並通知房東，等待確認。", "ok")
            return redirect(url_for("tenant_home"))

    return render_template("tenant_elec.html", room=room, period=period, combined=None,
                           pay=current, rate=rate, meter_last=meter_last, errors=errors,
                           form=request.form if request.method == "POST" else None,
                           prev=prev_payment_info(tenant))


# ================================================================ 屋主端
def owner_required():
    return (session.get("owner") is True
            and session.get("owner_ver") == get_setting("owner_ver", "0"))


def owner_password_ok(pw):
    saved = get_setting("owner_pw_hash")
    if saved:
        return check_password_hash(saved, pw)
    return hmac.compare_digest(pw, OWNER_PASSWORD)


def owner_guard():
    if not owner_required():
        return redirect(url_for("owner_login"))
    return None


@app.route("/owner/login", methods=["GET", "POST"])
def owner_login():
    error = None
    if request.method == "POST":
        key = f"owner:{client_ip()}"
        fails = recent_fails(key, hours=1)
        if fails >= OWNER_MAX_FAILS:
            error = "錯誤太多次，請一小時後再試。"
        elif owner_password_ok(request.form.get("password", "")):
            session.permanent = True
            session["owner"] = True
            session["owner_ver"] = get_setting("owner_ver", "0")
            return redirect(url_for("owner_home"))
        else:
            db.session.add(LoginFail(ip=key))
            db.session.commit()
            left = OWNER_MAX_FAILS - fails - 1
            error = f"密碼不對，還可以再試 {left} 次。" if left else "錯誤太多次，請一小時後再試。"
    return render_template("owner_login.html", error=error,
                           default_pw=(OWNER_PASSWORD == "admin" and not get_setting("owner_pw_hash")))


@app.route("/owner/password", methods=["GET", "POST"])
def owner_password():
    if (g := owner_guard()):
        return g
    error = None
    if request.method == "POST":
        f = request.form
        new, new2 = f.get("new", ""), f.get("new2", "")
        if not owner_password_ok(f.get("old", "")):
            error = "目前的密碼不對。"
        elif len(new) < 10 or new.isdigit() or new.isalpha():
            error = "新密碼至少 10 個字元，而且要同時有英文和數字。"
        elif new != new2:
            error = "兩次輸入的新密碼不一樣。"
        else:
            ver = str(int(get_setting("owner_ver", "0")) + 1)
            set_setting("owner_pw_hash", generate_password_hash(new))
            set_setting("owner_ver", ver)
            db.session.commit()
            session["owner_ver"] = ver
            flash("屋主密碼已變更，其他裝置需要用新密碼重新登入。", "ok")
            return redirect(url_for("owner_home"))
    return render_template("owner_password.html", error=error)


@app.route("/owner/logout", methods=["POST"])
def owner_logout():
    session.pop("owner", None)
    session.pop("owner_ver", None)
    return redirect(url_for("index"))


def room_status(room, period):
    """回傳 (文字, 樣式, 待確認的繳費 id)。"""
    t = room.tenant
    if not t:
        return ("空房", "empty", None)
    pays = period_payments(t, period)
    pending = [p for p in pays.values() if not p.confirmed]
    if pending:
        return ("待確認", "wait", pending[0].id)
    rent_p, elec_p = rent_record(pays), elec_record(pays)
    if rent_p and elec_p:
        return ("已完成", "ok", None)
    if rent_p:
        return ("待電費", "todo", None)
    if date.today() > t.due_date(period):
        return ("逾期", "late", None)
    return (f"{t.due_day} 號繳", "todo", None)


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
    entry = public_url(url_for("tenant_login"))
    return render_template("owner_home.html", rooms=rooms, status=status, period=period,
                           pending=pending, recent=recent, inactive=inactive,
                           rate=get_setting("elec_rate", "0"),
                           login_locked=get_setting("tenant_login_locked") == "1",
                           entry=entry, qr=qr_svg(entry))


@app.route("/owner/reopen", methods=["POST"])
def owner_reopen():
    if (g := owner_guard()):
        return g
    set_setting("tenant_login_locked", "0")
    set_setting("fails_reset_at", datetime.now().isoformat())
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
    if (g := owner_guard()):
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
    if (g := owner_guard()):
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
            tenant.moved_out_at = datetime.now()
            tenant.pin_ver = (tenant.pin_ver or 0) + 1
            room.set_owner_code(new_code(room.id))
            flash(f"已解除 {tenant.name} 的綁定，房間變回空房，繳費紀錄已保留。新房客請用密碼 {room.code}。", "ok")
            left = Payment.query.filter_by(tenant_id=tenant.id, status="待確認").count()
            if left:
                flash(f"{tenant.name} 還有 {left} 筆待確認的回報，仍會顯示在總覽，請確認收款或刪除。", "warn")
        elif action == "delete":
            used = Tenant.query.filter_by(room_id=room.id).first() or \
                Payment.query.filter_by(room_id=room.id).first()
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
    deletable = not (Tenant.query.filter_by(room_id=room.id).first()
                     or Payment.query.filter_by(room_id=room.id).first())
    return render_template("owner_room.html", room=room, tenant=tenant, history=history,
                           past=past, past_pending=past_pending, deletable=deletable, buildings=buildings(),
                           types=room_types(),
                           status=room_status(room, this_period()) if room.active else None)


@app.route("/owner/pay/<int:pid>", methods=["POST"])
def owner_pay(pid):
    if (g := owner_guard()):
        return g
    p = db.get_or_404(Payment, pid)
    if request.form.get("action") == "confirm":
        p.received = to_int(request.form.get("received"), p.total)
        p.confirm_note = request.form.get("note", "").strip()
        p.status = "已確認"
        p.confirmed_at = datetime.now()
        diff = p.received - p.due
        msg = f"{p.room.name} {p.period} {p.kind_label} 已確認收款 {p.received:,}。"
        if diff:
            msg += f" 與應付金額差 {diff:+,}。"
        flash(msg, "ok")
    elif request.form.get("action") == "delete":
        if p.confirmed:
            flash("已確認的款項不能刪除，請先取消確認。", "warn")
        else:
            label = f"{p.room.name} {p.period} {p.kind_label}（{p.tenant.name}）"
            db.session.delete(p)
            db.session.commit()
            flash(f"已刪除 {label} 的回報。", "ok")
            if request.form.get("back") == "room":
                return redirect(url_for("owner_room", rid=p.room_id))
            return redirect(url_for("owner_home"))
    elif request.form.get("action") == "undo":
        p.status = "待確認"
        p.confirm_note = (p.confirm_note + f"｜{datetime.now():%m/%d %H:%M} 取消確認").strip("｜")
        p.confirmed_at = None
        flash(f"{p.room.name} {p.period} {p.kind_label} 已取消確認。", "warn")
    db.session.commit()
    if request.form.get("back") == "room":
        return redirect(url_for("owner_room", rid=p.room_id))
    return redirect(url_for("owner_home"))


@app.route("/owner/settings", methods=["POST"])
def owner_settings():
    if (g := owner_guard()):
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


if __name__ == "__main__":
    app.run(debug=True, port=5000)
