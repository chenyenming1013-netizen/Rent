"""租屋收費管理網頁（基本版）

角色：
  屋主端  /owner   用 OWNER_PASSWORD 登入，管理房間、確認收款
  房客端  /tenant  選空房自行綁定（自設四位數密碼）→ 查看資料、回報繳費
  房東可以確認新房客、重設密碼、解除綁定（退租）
"""
import hmac
import os
import secrets
from datetime import date, datetime

from flask import (Flask, abort, flash, redirect, render_template, request,
                   session, url_for)
from flask_sqlalchemy import SQLAlchemy
from werkzeug.security import check_password_hash, generate_password_hash
from sqlalchemy.exc import IntegrityError

# ---------------------------------------------------------------- 設定
ON_VERCEL = bool(os.environ.get("VERCEL"))

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY") or "dev-only-change-me"
app.config["PERMANENT_SESSION_LIFETIME"] = 60 * 60 * 24 * 90  # 登入記住 90 天

db_url = os.environ.get("DATABASE_URL") or os.environ.get("POSTGRES_URL")
if not db_url:
    if ON_VERCEL:
        # Vercel 的檔案系統不能保存資料，一定要接 Neon 等外部資料庫
        raise RuntimeError("請在 Vercel 安裝 Neon 並設定 DATABASE_URL")
    db_url = "sqlite:///rent.db"
if db_url.startswith("postgres://"):  # 部分服務給的是舊格式
    db_url = db_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = db_url
app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {"pool_pre_ping": True, "pool_recycle": 280}
if ON_VERCEL:
    app.config["SESSION_COOKIE_SECURE"] = True

OWNER_PASSWORD = os.environ.get("OWNER_PASSWORD", "admin")
MAX_ATTEMPTS = 5
DEFAULT_ROOMS = [
    ("輝煌", "輝煌"),
    ("民權", "民權 A"), ("民權", "民權 B"),
    ("憲政", "憲政 01"), ("憲政", "憲政 02"), ("憲政", "憲政 03"), ("憲政", "憲政 04"),
]

db = SQLAlchemy(app)


# ---------------------------------------------------------------- 資料表
class Room(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    building = db.Column(db.String(20), nullable=False)
    name = db.Column(db.String(20), unique=True, nullable=False)
    sort = db.Column(db.Integer, default=0)
    rent = db.Column(db.Integer, default=0)            # 月租
    deposit_months = db.Column(db.Integer, default=2)  # 押金月數
    meter_start = db.Column(db.Integer, default=0)     # 入住時電表度數
    failed = db.Column(db.Integer, default=0)
    locked = db.Column(db.Boolean, default=False)

    @property
    def tenant(self):
        return Tenant.query.filter_by(room_id=self.id, active=True).first()

    @property
    def deposit(self):
        return (self.rent or 0) * (self.deposit_months or 0)


class Tenant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    room_id = db.Column(db.Integer, db.ForeignKey("room.id"), nullable=False)
    name = db.Column(db.String(40), nullable=False)
    phone = db.Column(db.String(20), default="")
    checkin = db.Column(db.Date, nullable=False)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    moved_out_at = db.Column(db.DateTime)
    pin_hash = db.Column(db.String(255), nullable=False)  # 房客自設的四位數密碼（只存雜湊）
    pin_ver = db.Column(db.Integer, default=1)            # 重設密碼後讓舊登入失效
    verified = db.Column(db.Boolean, default=False)       # 房東是否已核對

    def set_pin(self, pin):
        self.pin_hash = generate_password_hash(pin)
        self.pin_ver = (self.pin_ver or 0) + 1

    def check_pin(self, pin):
        return check_password_hash(self.pin_hash, pin)

    @property
    def due_day(self):
        return min(self.checkin.day, 28)


class Payment(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    tenant_id = db.Column(db.Integer, db.ForeignKey("tenant.id"), nullable=False)
    room_id = db.Column(db.Integer, db.ForeignKey("room.id"), nullable=False)
    period = db.Column(db.String(7), nullable=False)   # YYYY-MM
    rent_amount = db.Column(db.Integer, default=0)
    meter_last = db.Column(db.Integer)
    meter_now = db.Column(db.Integer)
    usage = db.Column(db.Integer, default=0)
    rate = db.Column(db.Float, default=0)              # 當時的電費單價
    elec_amount = db.Column(db.Integer, default=0)
    other_amount = db.Column(db.Integer, default=0)
    other_note = db.Column(db.String(100), default="")
    total = db.Column(db.Integer, default=0)
    method = db.Column(db.String(20), default="銀行轉帳")
    last5 = db.Column(db.String(5), default="")
    paid_date = db.Column(db.Date)
    status = db.Column(db.String(10), default="待確認")
    received = db.Column(db.Integer)
    confirm_note = db.Column(db.String(200), default="")
    confirmed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    tenant = db.relationship("Tenant")
    room = db.relationship("Room")
    __table_args__ = (db.UniqueConstraint("tenant_id", "period"),)


class Setting(db.Model):
    key = db.Column(db.String(40), primary_key=True)
    value = db.Column(db.String(200))


def get_setting(key, default=""):
    s = db.session.get(Setting, key)
    return s.value if s else default


def set_setting(key, value):
    s = db.session.get(Setting, key) or Setting(key=key)
    s.value = str(value)
    db.session.add(s)


def new_code():
    return f"{secrets.randbelow(10000):04d}"


def init_db():
    db.create_all()
    if not Room.query.first():
        try:
            for i, (b, n) in enumerate(DEFAULT_ROOMS):
                db.session.add(Room(building=b, name=n, sort=i))
            set_setting("elec_rate", "5")
            db.session.commit()
        except IntegrityError:  # 同時有兩個執行個體在建立資料時
            db.session.rollback()


with app.app_context():
    init_db()


# ---------------------------------------------------------------- 共用工具
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


WEAK_PINS = {"0000", "1234", "4321", "1111", "2222", "3333", "4444", "5555",
             "6666", "7777", "8888", "9999", "1212", "0123", "9876"}


def pin_problem(pin, pin2):
    if not (pin.isdigit() and len(pin) == 4):
        return "密碼要是四位數字。"
    if pin != pin2:
        return "兩次輸入的密碼不一樣。"
    if pin in WEAK_PINS:
        return "這組密碼太容易被猜到，請換一組。"
    return None


def this_period():
    return date.today().strftime("%Y-%m")


def last_meter(room, tenant, exclude_period=None):
    """上期度數：最近一筆（不含本期）的本月度數，沒有就用入住度數。"""
    q = Payment.query.filter(Payment.tenant_id == tenant.id,
                             Payment.meter_now.isnot(None))
    if exclude_period:
        q = q.filter(Payment.period < exclude_period)
    p = q.order_by(Payment.period.desc()).first()
    return p.meter_now if p else (room.meter_start or 0)


@app.template_filter("money")
def money(v):
    return f"{int(v or 0):,}"


@app.context_processor
def inject():
    if "csrf" not in session:
        session["csrf"] = secrets.token_hex(16)
    return {"csrf": session["csrf"], "today": date.today()}


@app.before_request
def check_csrf():
    if request.method == "POST":
        token = request.form.get("csrf", "")
        if not hmac.compare_digest(token, session.get("csrf", "")):
            abort(400, "表單已過期，請重新整理頁面")


# ---------------------------------------------------------------- 入口
@app.route("/")
def index():
    return render_template("index.html")


@app.route("/ping")
def ping():
    return "ok"


# ---------------------------------------------------------------- 房客端
def current_room():
    rid = session.get("room_id")
    return db.session.get(Room, rid) if rid else None


def tenant_required():
    room = current_room()
    t = room.tenant if room else None
    if not t or t.id != session.get("tenant_id") or t.pin_ver != session.get("pin_ver"):
        for k in ("room_id", "tenant_id", "pin_ver"):
            session.pop(k, None)
        return None, None
    return room, t


@app.route("/tenant")
def tenant_rooms():
    rooms = Room.query.order_by(Room.sort).all()
    groups = {}
    for r in rooms:
        groups.setdefault(r.building, []).append(r)
    return render_template("tenant_rooms.html", groups=groups, mine=tenant_required()[0])


@app.route("/tenant/room/<int:rid>", methods=["GET", "POST"])
def tenant_room(rid):
    room = db.get_or_404(Room, rid)
    mine = tenant_required()[0]
    if mine and mine.id != room.id:
        flash(f"這台裝置已綁定 {mine.name}，要換房間請先登出。", "warn")
        return redirect(url_for("tenant_rooms"))
    if mine and mine.id == room.id:
        return redirect(url_for("tenant_home"))

    tenant = room.tenant
    mode = "login" if tenant else "bind"
    error = None

    if request.method == "POST":
        f = request.form
        pin = f.get("pin", "").strip()
        if mode == "bind":
            name = f.get("name", "").strip()
            checkin = to_date(f.get("checkin"))
            error = pin_problem(pin, f.get("pin2", "").strip())
            if not name or not checkin:
                error = "請填寫姓名和入住日期。"
            if not error and room.tenant:  # 剛好被別人搶先綁定
                error = "這間房剛剛已被綁定，請重新選擇。"
            if not error:
                tenant = Tenant(room_id=room.id, name=name, checkin=checkin,
                                phone=f.get("phone", "").strip())
                tenant.set_pin(pin)
                db.session.add(tenant)
                room.failed, room.locked = 0, False
                db.session.commit()
                return _login_tenant(room, tenant, "綁定完成，這間房已顯示為有房客。請記住你的密碼。")
        elif room.locked:
            error = "密碼錯誤太多次，這間房已暫停登入，請聯絡房東解鎖。"
        elif not tenant.check_pin(pin):
            room.failed = (room.failed or 0) + 1
            left = MAX_ATTEMPTS - room.failed
            if left <= 0:
                room.locked = True
                error = "錯誤太多次，這間房已暫停登入，請聯絡房東解鎖。"
            else:
                error = f"密碼不對，還可以再試 {left} 次。"
            db.session.commit()
        else:
            room.failed = 0
            db.session.commit()
            return _login_tenant(room, tenant, None)

    return render_template("tenant_room.html", room=room, mode=mode, error=error,
                           form=request.form)


def _login_tenant(room, tenant, msg):
    session.permanent = True
    session["room_id"] = room.id
    session["tenant_id"] = tenant.id
    session["pin_ver"] = tenant.pin_ver
    if msg:
        flash(msg, "ok")
    return redirect(url_for("tenant_home"))


@app.route("/tenant/logout", methods=["POST"])
def tenant_logout():
    for k in ("room_id", "tenant_id", "pin_ver"):
        session.pop(k, None)
    return redirect(url_for("tenant_rooms"))


@app.route("/tenant/home")
def tenant_home():
    room, tenant = tenant_required()
    if not room:
        return redirect(url_for("tenant_rooms"))
    period = this_period()
    current = Payment.query.filter_by(tenant_id=tenant.id, period=period).first()
    history = (Payment.query.filter_by(tenant_id=tenant.id)
               .order_by(Payment.period.desc()).limit(24).all())
    return render_template("tenant_home.html", room=room, tenant=tenant,
                           period=period, current=current, history=history)


@app.route("/tenant/profile", methods=["POST"])
def tenant_profile():
    room, tenant = tenant_required()
    if not room:
        return redirect(url_for("tenant_rooms"))
    name = request.form.get("name", "").strip()
    if name:
        tenant.name = name
    tenant.phone = request.form.get("phone", "").strip()
    db.session.commit()
    flash("個人資料已儲存。", "ok")
    return redirect(url_for("tenant_home"))


@app.route("/tenant/pin", methods=["POST"])
def tenant_pin():
    room, tenant = tenant_required()
    if not room:
        return redirect(url_for("tenant_rooms"))
    f = request.form
    if not tenant.check_pin(f.get("old_pin", "").strip()):
        flash("目前的密碼不對，密碼沒有變更。", "warn")
    else:
        problem = pin_problem(f.get("pin", "").strip(), f.get("pin2", "").strip())
        if problem:
            flash(problem + "密碼沒有變更。", "warn")
        else:
            tenant.set_pin(f["pin"].strip())
            db.session.commit()
            session["pin_ver"] = tenant.pin_ver
            flash("密碼已變更。", "ok")
    return redirect(url_for("tenant_home"))


@app.route("/tenant/pay", methods=["GET", "POST"])
def tenant_pay():
    room, tenant = tenant_required()
    if not room:
        return redirect(url_for("tenant_rooms"))
    period = request.values.get("period") or this_period()
    pay = Payment.query.filter_by(tenant_id=tenant.id, period=period).first()
    if pay and pay.status == "已確認":
        flash(f"{period} 已確認收款，不能再修改。", "warn")
        return redirect(url_for("tenant_home"))

    rate = float(get_setting("elec_rate", "0") or 0)
    meter_last = last_meter(room, tenant, exclude_period=period)
    errors = {}

    if request.method == "POST":
        f = request.form
        meter_now = f.get("meter_now", "").strip()
        rent_amount = to_int(f.get("rent_amount"), room.rent)
        other_amount = to_int(f.get("other_amount"), 0)
        method = f.get("method", "銀行轉帳")
        last5 = f.get("last5", "").strip()
        paid_date = to_date(f.get("paid_date"))
        total = to_int(f.get("total"), -1)

        usage = elec = 0
        mn = None
        if meter_now:
            mn = to_int(meter_now, -1)
            if mn < meter_last:
                errors["meter_now"] = f"不能小於上期度數 {meter_last:,}"
            else:
                usage = mn - meter_last
                elec = round(usage * rate)
        else:
            errors["meter_now"] = "請填本月電表度數"
        if method == "銀行轉帳" and not (last5.isdigit() and len(last5) == 5):
            errors["last5"] = "請填五位數字"
        if not paid_date:
            errors["paid_date"] = "請選擇付款日期"
        if total <= 0:
            errors["total"] = "請填實際付款金額"
        if other_amount and not f.get("other_note", "").strip():
            errors["other_note"] = "請說明其他費用是什麼"

        if not errors:
            pay = pay or Payment(tenant_id=tenant.id, room_id=room.id, period=period)
            pay.rent_amount = rent_amount
            pay.meter_last, pay.meter_now, pay.usage = meter_last, mn, usage
            pay.rate, pay.elec_amount = rate, elec
            pay.other_amount = other_amount
            pay.other_note = f.get("other_note", "").strip()
            pay.total = total
            pay.method = method
            pay.last5 = last5 if method == "銀行轉帳" else ""
            pay.paid_date = paid_date
            pay.status = "待確認"
            db.session.add(pay)
            db.session.commit()
            flash("已送出，等待房東確認。", "ok")
            return redirect(url_for("tenant_home"))

    # 末五碼預設帶入上次填的值
    prev = (Payment.query.filter(Payment.tenant_id == tenant.id, Payment.last5 != "")
            .order_by(Payment.created_at.desc()).first())
    return render_template("tenant_pay.html", room=room, tenant=tenant, period=period,
                           pay=pay, rate=rate, meter_last=meter_last, errors=errors,
                           form=request.form if request.method == "POST" else None,
                           prev_last5=prev.last5 if prev else "")


# ---------------------------------------------------------------- 屋主端
def owner_required():
    return session.get("owner") is True


@app.route("/owner/login", methods=["GET", "POST"])
def owner_login():
    error = None
    if request.method == "POST":
        if hmac.compare_digest(request.form.get("password", ""), OWNER_PASSWORD):
            session.permanent = True
            session["owner"] = True
            return redirect(url_for("owner_home"))
        error = "密碼不對。"
    return render_template("owner_login.html", error=error,
                           default_pw=(OWNER_PASSWORD == "admin"))


@app.route("/owner/logout", methods=["POST"])
def owner_logout():
    session.pop("owner", None)
    return redirect(url_for("index"))


@app.route("/owner")
def owner_home():
    if not owner_required():
        return redirect(url_for("owner_login"))
    rooms = Room.query.order_by(Room.sort).all()
    period = this_period()
    status = {}
    for r in rooms:
        t = r.tenant
        if not t:
            status[r.id] = ("空房", "empty")
            continue
        p = Payment.query.filter_by(tenant_id=t.id, period=period).first()
        if p and p.status == "已確認":
            status[r.id] = ("已確認", "ok")
        elif p:
            status[r.id] = ("待確認", "wait")
        elif date.today().day > t.due_day:
            status[r.id] = ("逾期", "late")
        else:
            status[r.id] = (f"{t.due_day} 號繳", "todo")
    pending = (Payment.query.filter_by(status="待確認")
               .order_by(Payment.paid_date, Payment.id).all())
    recent = (Payment.query.filter_by(status="已確認")
              .order_by(Payment.confirmed_at.desc()).limit(20).all())
    return render_template("owner_home.html", rooms=rooms, status=status,
                           pending=pending, recent=recent, period=period,
                           rate=get_setting("elec_rate", "0"))


@app.route("/owner/room/<int:rid>", methods=["GET", "POST"])
def owner_room(rid):
    if not owner_required():
        return redirect(url_for("owner_login"))
    room = db.get_or_404(Room, rid)
    tenant = room.tenant
    if request.method == "POST":
        action = request.form.get("action")
        f = request.form
        if action == "save":
            room.rent = to_int(f.get("rent"), room.rent)
            room.deposit_months = to_int(f.get("deposit_months"), 2)
            room.meter_start = to_int(f.get("meter_start"), room.meter_start)
            if tenant:
                tenant.name = f.get("name", tenant.name).strip() or tenant.name
                tenant.phone = f.get("phone", "").strip()
                tenant.checkin = to_date(f.get("checkin")) or tenant.checkin
            flash("已儲存。", "ok")
        elif action == "unlock":
            room.locked, room.failed = False, 0
            flash("已解鎖。", "ok")
        elif action == "verify" and tenant:
            tenant.verified = True
            flash(f"已確認 {tenant.name} 是這間房的房客。", "ok")
        elif action == "reset_pin" and tenant:
            pin = new_code()
            while pin in WEAK_PINS:
                pin = new_code()
            tenant.set_pin(pin)
            room.failed, room.locked = 0, False
            flash(f"{tenant.name} 的臨時密碼是 {pin}，請私下告訴房客，登入後可自行更改。這組密碼只顯示這一次。", "ok")
        elif action == "moveout" and tenant:
            tenant.active = False
            tenant.moved_out_at = datetime.now()
            room.failed, room.locked = 0, False
            flash(f"已解除 {tenant.name} 的綁定，房間變回空房，繳費紀錄已保留。", "ok")
        db.session.commit()
        return redirect(url_for("owner_room", rid=room.id))
    history = []
    if tenant:
        history = (Payment.query.filter_by(tenant_id=tenant.id)
                   .order_by(Payment.period.desc()).all())
    past = (Tenant.query.filter_by(room_id=room.id, active=False)
            .order_by(Tenant.moved_out_at.desc()).all())
    return render_template("owner_room.html", room=room, tenant=tenant,
                           history=history, past=past)


@app.route("/owner/pay/<int:pid>", methods=["POST"])
def owner_pay(pid):
    if not owner_required():
        return redirect(url_for("owner_login"))
    p = db.get_or_404(Payment, pid)
    if request.form.get("action") == "confirm":
        p.received = to_int(request.form.get("received"), p.total)
        p.confirm_note = request.form.get("note", "").strip()
        p.status = "已確認"
        p.confirmed_at = datetime.now()
        diff = p.received - p.total
        msg = f"{p.room.name} {p.period} 已確認收款 {p.received:,}。"
        if diff:
            msg += f" 與回報金額差 {diff:+,}，已記在備註。"
        flash(msg, "ok")
    elif request.form.get("action") == "undo":
        p.status = "待確認"
        p.confirm_note = (p.confirm_note + f"｜{datetime.now():%m/%d %H:%M} 取消確認").strip("｜")
        p.confirmed_at = None
        flash(f"{p.room.name} {p.period} 已取消確認。", "warn")
    db.session.commit()
    return redirect(request.referrer or url_for("owner_home"))


@app.route("/owner/settings", methods=["POST"])
def owner_settings():
    if not owner_required():
        return redirect(url_for("owner_login"))
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
