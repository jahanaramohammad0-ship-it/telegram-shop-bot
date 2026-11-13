import asyncio, logging, os, secrets, sqlite3, time
from datetime import datetime, timedelta, timezone

import httpx
from dotenv import load_dotenv
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

load_dotenv()
BOT_TOKEN = os.environ["BOT_TOKEN"]
OWNERS = {int(x) for x in os.environ["OWNER_IDS"].split(",") if x.strip()}
PANEL_URL = os.environ["PANEL_URL"].rstrip("/")
PANEL_USER = os.environ["PANEL_USER"]
PANEL_PASS = os.environ["PANEL_PASS"]
GROUP_IDS = [int(x) for x in os.getenv("PANEL_GROUP_IDS", "1").split(",") if x.strip()]
DB_PATH = os.getenv("DB_PATH", "bot.db")
BILLING_INTERVAL = 60  # ثانیه
GB = 1024 ** 3

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("bot")
bot = Bot(BOT_TOKEN)
router = Router()

# ───────────────────────── دیتابیس ─────────────────────────
db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS customers(
  tg_id INTEGER PRIMARY KEY, name TEXT, balance REAL DEFAULT 0,
  blocked INTEGER DEFAULT 0, suspended INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS admins(
  panel_user TEXT PRIMARY KEY, owner_id INTEGER, last_used INTEGER DEFAULT 0,
  last_tick REAL, bot_disabled INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS tests(tg_id INTEGER PRIMARY KEY, username TEXT, created REAL);
CREATE TABLE IF NOT EXISTS payments(
  id INTEGER PRIMARY KEY AUTOINCREMENT, tg_id INTEGER, amount INTEGER, status TEXT DEFAULT 'pending');
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
""")

DEFAULTS = {"price_gb": "5000", "price_hour": "10", "card": "6037-0000-0000-0000",
            "min_balance": "10000", "test_enabled": "1", "test_gb": "1", "test_hours": "24"}
LABELS = {"price_gb": "💵 قیمت هر گیگ (تومان)", "price_hour": "⏱ قیمت هر ساعت (تومان)",
          "min_balance": "🔒 حداقل موجودی برای خرید ادمین", "test_gb": "🎁 حجم تست (گیگ)",
          "test_hours": "🎁 مدت تست (ساعت)", "card": "💳 شماره کارت"}


def setting(k):
    r = db.execute("SELECT value FROM settings WHERE key=?", (k,)).fetchone()
    return r["value"] if r else DEFAULTS[k]


def set_setting(k, v):
    db.execute("INSERT OR REPLACE INTO settings VALUES(?,?)", (k, str(v)))
    db.commit()


def customer(uid):
    return db.execute("SELECT * FROM customers WHERE tg_id=?", (uid,)).fetchone()


def ensure_customer(user):
    db.execute("INSERT OR IGNORE INTO customers(tg_id,name) VALUES(?,?)", (user.id, user.full_name))
    db.commit()


# ───────────────────────── کلاینت پنل پاسارگارد (با دسترسی سودو) ─────────────────────────
class Panel:
    token = None

    async def _login(self):
        async with httpx.AsyncClient(timeout=30, verify=False) as c:
            r = await c.post(f"{PANEL_URL}/api/admin/token", data={"username": PANEL_USER, "password": PANEL_PASS})
            r.raise_for_status()
            self.token = r.json()["access_token"]

    async def req(self, method, path, **kw):
        if not self.token:
            await self._login()
        for attempt in (0, 1):
            async with httpx.AsyncClient(timeout=30, verify=False) as c:
                r = await c.request(method, PANEL_URL + path, headers={"Authorization": f"Bearer {self.token}"}, **kw)
            if r.status_code == 401 and attempt == 0:
                await self._login()
                continue
            r.raise_for_status()
            return r.json() if r.content else {}

    # ادمین (اپراتور = ادمین غیرسودو)
    async def create_admin(self, username, password):
        return await self.req("POST", "/api/admin", json={"username": username, "password": password, "is_sudo": False})

    async def get_admin(self, username):
        return await self.req("GET", f"/api/admin/{username}")

    async def modify_admin(self, username, **fields):
        return await self.req("PUT", f"/api/admin/{username}", json={"is_sudo": False, **fields})

    # کانفیگ تست
    async def create_test(self, username, gb, hours):
        body = {"username": username, "status": "active", "group_ids": GROUP_IDS,
                "data_limit": int(gb * GB),
                "expire": (datetime.now(timezone.utc) + timedelta(hours=hours)).isoformat()}
        return await self.req("POST", "/api/user", json=body)


panel = Panel()


def usage_of(admin):
    """مصرف کل ادمین (بایت). اسم فیلد بسته به نسخه پنل فرق می‌کنه."""
    for k in ("lifetime_used_traffic", "used_traffic", "users_usage"):
        if admin.get(k) is not None:
            return int(admin[k])
    return 0


# ───────────────────────── ابزار رابط ─────────────────────────
class S(StatesGroup):
    amount = State()
    receipt = State()
    new_admin = State()
    setting = State()
    adjust = State()


def kb(rows):
    """دکمه‌های شیشه‌ای (اینلاین). هر آیتم: (متن, callback) یا (متن, None, url)"""
    b = InlineKeyboardBuilder()
    for row in rows:
        btns = []
        for it in row:
            if len(it) == 3:
                btns.append(InlineKeyboardButton(text=it[0], url=it[2]))
            else:
                btns.append(InlineKeyboardButton(text=it[0], callback_data=it[1]))
        b.row(*btns)
    return b.as_markup()


def main_menu(uid):
    rows = [[("🎁 تست رایگان", "test"), ("🛒 خرید ادمین", "buy")],
            [("📦 سرویس‌های شما", "svc:0"), ("💰 شارژ کیف پول", "topup")],
            [("👤 حساب من", "me")]]
    if uid in OWNERS:
        rows.append([("⚙️ مدیریت", "mgmt")])
    return kb(rows)


def pricing_text():
    return (f"💵 هر گیگ مصرف: {float(setting('price_gb')):,.0f} تومان\n"
            f"⏱ هر ساعت (برای هر ادمین فعال): {float(setting('price_hour')):,.0f} تومان")


def fmt_gb(b):
    return f"{b / GB:.2f} GB"


async def show(c: CallbackQuery, text, markup, **kw):
    try:
        await c.message.edit_text(text, reply_markup=markup, **kw)
    except Exception:
        await c.message.answer(text, reply_markup=markup, **kw)


@router.message(CommandStart())
async def start(m: Message, state: FSMContext):
    await state.clear()
    ensure_customer(m.from_user)
    await m.answer("سلام 👋\nبه ربات فروش ادمین پنل پاسارگارد خوش اومدی.", reply_markup=main_menu(m.from_user.id))


@router.callback_query(F.data == "home")
async def cb_home(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, "منوی اصلی 👇", main_menu(c.from_user.id))


# ───────────────────────── تست رایگان ─────────────────────────
@router.callback_query(F.data == "test")
async def free_test(c: CallbackQuery):
    ensure_customer(c.from_user)
    if setting("test_enabled") != "1":
        return await c.answer("تست رایگان فعلاً غیرفعاله.", show_alert=True)
    if db.execute("SELECT 1 FROM tests WHERE tg_id=?", (c.from_user.id,)).fetchone():
        return await c.answer("قبلاً تست رایگان گرفتی.", show_alert=True)
    name = f"test{c.from_user.id}{secrets.token_hex(2)}"
    try:
        u = await panel.create_test(name, float(setting("test_gb")), float(setting("test_hours")))
    except Exception as e:
        return await c.answer(f"خطا در ساخت تست: {e}"[:190], show_alert=True)
    db.execute("INSERT INTO tests VALUES(?,?,?)", (c.from_user.id, name, time.time()))
    db.commit()
    await show(c, f"🎁 کانفیگ تست شما ({setting('test_gb')} گیگ / {setting('test_hours')} ساعت)\n\n"
                  f"👤 <code>{name}</code>\n🔗 <code>{u.get('subscription_url', '-')}</code>",
               kb([[("🔙 بازگشت", "home")]]), parse_mode="HTML")


# ───────────────────────── حساب و کیف پول ─────────────────────────
@router.callback_query(F.data == "me")
async def me(c: CallbackQuery):
    ensure_customer(c.from_user)
    a = customer(c.from_user.id)
    n = db.execute("SELECT COUNT(*) FROM admins WHERE owner_id=?", (a["tg_id"],)).fetchone()[0]
    await show(c, f"👤 حساب شما\n\n💰 موجودی: {a['balance']:,.0f} تومان\n📦 تعداد ادمین: {n}\n"
                  f"{'⛔️ تعلیق (موجودی صفر)' if a['suspended'] else '🟢 فعال'}\n\n{pricing_text()}",
               kb([[("🔙 بازگشت", "home")]]))


@router.callback_query(F.data == "topup")
async def topup(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.amount)
    await show(c, "مبلغ شارژ رو به تومان بفرست (فقط عدد):", kb([[("🔙 انصراف", "home")]]))


@router.message(S.amount)
async def topup_amount(m: Message, state: FSMContext):
    if not m.text or not m.text.strip().isdigit():
        return await m.answer("فقط عدد بفرست.")
    await state.update_data(amount=int(m.text))
    await state.set_state(S.receipt)
    await m.answer(f"💳 مبلغ {int(m.text):,} تومان رو به کارت زیر واریز کن:\n<code>{setting('card')}</code>\n\nسپس عکس رسید رو بفرست.",
                   parse_mode="HTML")


@router.message(S.receipt, F.photo)
async def topup_receipt(m: Message, state: FSMContext):
    amount = (await state.get_data())["amount"]
    pid = db.execute("INSERT INTO payments(tg_id,amount) VALUES(?,?)", (m.from_user.id, amount)).lastrowid
    db.commit()
    for o in OWNERS:
        await bot.send_photo(o, m.photo[-1].file_id,
                             caption=f"💳 شارژ #{pid}\nکاربر: {m.from_user.full_name} ({m.from_user.id})\nمبلغ: {amount:,} تومان",
                             reply_markup=kb([[("✅ تایید", f"pay_ok:{pid}"), ("❌ رد", f"pay_no:{pid}")]]))
    await state.clear()
    await m.answer("رسید ارسال شد؛ بعد از تایید مدیر کیف پولت شارژ می‌شه ✅", reply_markup=main_menu(m.from_user.id))


@router.callback_query(F.data.startswith("pay_"), F.from_user.id.in_(OWNERS))
async def pay_decision(c: CallbackQuery):
    act, pid = c.data.split(":")
    p = db.execute("SELECT * FROM payments WHERE id=?", (int(pid),)).fetchone()
    if not p or p["status"] != "pending":
        return await c.answer("قبلاً بررسی شده")
    if act == "pay_no":
        db.execute("UPDATE payments SET status='rejected' WHERE id=?", (p["id"],))
        db.commit()
        await bot.send_message(p["tg_id"], "❌ پرداخت شما تایید نشد.")
    else:
        db.execute("UPDATE payments SET status='ok' WHERE id=?", (p["id"],))
        await add_balance(p["tg_id"], p["amount"])
        await bot.send_message(p["tg_id"], f"✅ کیف پول شما {p['amount']:,} تومان شارژ شد.")
    await c.message.edit_caption(caption=(c.message.caption or "") + f"\n\nنتیجه: {'✅ تایید' if act == 'pay_ok' else '❌ رد'}")


async def add_balance(uid, amount):
    db.execute("UPDATE customers SET balance=balance+? WHERE tg_id=?", (amount, uid))
    db.commit()
    a = customer(uid)
    if a and a["suspended"] and a["balance"] > 0 and not a["blocked"]:
        await resume_customer(a)


# ───────────────────────── خرید ادمین ─────────────────────────
@router.callback_query(F.data == "buy")
async def buy(c: CallbackQuery, state: FSMContext):
    ensure_customer(c.from_user)
    a = customer(c.from_user.id)
    need = float(setting("min_balance"))
    if a["blocked"]:
        return await c.answer("حساب شما مسدوده.", show_alert=True)
    if a["balance"] < need:
        return await c.answer(f"برای خرید ادمین حداقل {need:,.0f} تومان موجودی لازمه 💰", show_alert=True)
    await state.set_state(S.new_admin)
    await show(c, f"نام کاربری ادمین جدید رو بفرست (انگلیسی/عدد، ۳ تا ۳۲ کاراکتر).\nرمز عبور خودکار ساخته می‌شه.\n\n{pricing_text()}",
               kb([[("🔙 انصراف", "home")]]))


@router.message(S.new_admin)
async def new_admin(m: Message, state: FSMContext):
    n = (m.text or "").strip().lower()
    if not n.replace("_", "").isalnum() or not (3 <= len(n) <= 32):
        return await m.answer("نام نامعتبر؛ ۳ تا ۳۲ کاراکتر انگلیسی/عدد.")
    pw = secrets.token_urlsafe(12)
    try:
        await panel.create_admin(n, pw)
    except httpx.HTTPStatusError as e:
        msg = "این نام قبلاً گرفته شده." if e.response.status_code == 409 else f"خطای پنل: {e.response.status_code}"
        return await m.answer(f"❌ {msg}\nنام دیگه‌ای بفرست.")
    except Exception as e:
        return await m.answer(f"❌ خطا: {e}")
    cur = usage_of(await panel.get_admin(n))
    db.execute("INSERT INTO admins(panel_user,owner_id,last_used,last_tick) VALUES(?,?,?,?)",
               (n, m.from_user.id, cur, time.time()))
    db.commit()
    await state.clear()
    await m.answer(f"✅ ادمین ساخته شد\n\n🌐 <code>{PANEL_URL}/dashboard/</code>\n👤 <code>{n}</code>\n🔑 <code>{pw}</code>",
                   parse_mode="HTML", reply_markup=main_menu(m.from_user.id))


# ───────────────────────── سرویس‌های شما ─────────────────────────
PAGE = 8


@router.callback_query(F.data.startswith("svc:"))
async def svc_list(c: CallbackQuery):
    page = int(c.data.split(":")[1])
    rows_db = db.execute("SELECT panel_user FROM admins WHERE owner_id=? ORDER BY panel_user", (c.from_user.id,)).fetchall()
    chunk = rows_db[page * PAGE:(page + 1) * PAGE]
    rows = [[(f"👤 {r['panel_user']}", f"a:{r['panel_user']}")] for r in chunk]
    nav = []
    if page > 0:
        nav.append(("⬅️ قبلی", f"svc:{page - 1}"))
    if (page + 1) * PAGE < len(rows_db):
        nav.append(("بعدی ➡️", f"svc:{page + 1}"))
    if nav:
        rows.append(nav)
    rows.append([("🔙 بازگشت", "home")])
    await show(c, f"📦 سرویس‌های شما ({len(rows_db)})" if rows_db else "هنوز ادمینی نخریدی.", kb(rows))


def owns(uid, name):
    return db.execute("SELECT 1 FROM admins WHERE panel_user=? AND owner_id=?", (name, uid)).fetchone()


async def show_admin(c: CallbackQuery, name, extra=""):
    ad = await panel.get_admin(name)
    off = bool(ad.get("is_disabled"))
    await show(c, f"👤 <code>{name}</code>\nوضعیت: {'🔴 غیرفعال' if off else '🟢 فعال'}\n"
                  f"📊 مصرف کل: {fmt_gb(usage_of(ad))}\n👥 تعداد کاربران: {ad.get('total_users', '-')}\n{extra}",
               kb([[("🟢 روشن کردن" if off else "🔴 خاموش کردن", f"tg:{name}"), ("🔑 تغییر رمز", f"pw:{name}")],
                   [("🌐 ورود به پنل", None, f"{PANEL_URL}/dashboard/")],
                   [("🔄 بروزرسانی", f"a:{name}"), ("🔙 لیست", "svc:0")]]), parse_mode="HTML")


@router.callback_query(F.data.startswith(("a:", "tg:", "pw:")))
async def admin_actions(c: CallbackQuery):
    act, name = c.data.split(":", 1)
    if not owns(c.from_user.id, name):
        return await c.answer("دسترسی نداری", show_alert=True)
    cu = customer(c.from_user.id)
    extra = ""
    try:
        if act == "tg":
            ad = await panel.get_admin(name)
            enable = bool(ad.get("is_disabled"))
            if enable and (cu["balance"] <= 0 or cu["blocked"]):
                return await c.answer("موجودی کافی نیست 💰", show_alert=True)
            await panel.modify_admin(name, is_disabled=not enable)
            db.execute("UPDATE admins SET bot_disabled=0, last_tick=? WHERE panel_user=?", (time.time(), name))
            db.commit()
            await c.answer("انجام شد ✅")
        elif act == "pw":
            pw = secrets.token_urlsafe(12)
            await panel.modify_admin(name, password=pw)
            extra = f"\n🔑 رمز جدید: <code>{pw}</code>"
            await c.answer("رمز تغییر کرد 🔑")
        await show_admin(c, name, extra)
    except Exception as e:
        await c.answer(f"خطا: {e}"[:190], show_alert=True)


# ───────────────────────── مدیریت (مالک) ─────────────────────────
def mgmt_menu():
    t = setting("test_enabled") == "1"
    return kb([[("💵 قیمت هر گیگ", "set:price_gb"), ("⏱ قیمت هر ساعت", "set:price_hour")],
               [("🔒 حداقل موجودی", "set:min_balance"), ("💳 شماره کارت", "set:card")],
               [(f"🎁 تست: {'روشن 🟢' if t else 'خاموش 🔴'}", "test_toggle")],
               [("🎁 حجم تست", "set:test_gb"), ("🎁 مدت تست", "set:test_hours")],
               [("🪙 شارژ/کسر دستی", "adj"), ("👥 مشتریان", "cl")],
               [("🔙 بازگشت", "home")]])


def mgmt_text():
    return (f"⚙️ مدیریت\n\n{pricing_text()}\n🔒 حداقل موجودی: {float(setting('min_balance')):,.0f}\n"
            f"🎁 تست: {setting('test_gb')} گیگ / {setting('test_hours')} ساعت\n💳 کارت: {setting('card')}")


@router.callback_query(F.data == "mgmt", F.from_user.id.in_(OWNERS))
async def mgmt(c: CallbackQuery, state: FSMContext):
    await state.clear()
    await show(c, mgmt_text(), mgmt_menu())


@router.callback_query(F.data == "test_toggle", F.from_user.id.in_(OWNERS))
async def test_toggle(c: CallbackQuery):
    set_setting("test_enabled", "0" if setting("test_enabled") == "1" else "1")
    await show(c, mgmt_text(), mgmt_menu())


@router.callback_query(F.data.startswith("set:"), F.from_user.id.in_(OWNERS))
async def set_start(c: CallbackQuery, state: FSMContext):
    key = c.data.split(":")[1]
    await state.set_state(S.setting)
    await state.update_data(key=key)
    await show(c, f"{LABELS[key]}\nمقدار فعلی: {setting(key)}\n\nمقدار جدید رو بفرست:", kb([[("🔙 انصراف", "mgmt")]]))


@router.message(S.setting, F.from_user.id.in_(OWNERS))
async def set_value(m: Message, state: FSMContext):
    key = (await state.get_data())["key"]
    v = (m.text or "").strip()
    if key != "card":
        try:
            float(v)
        except ValueError:
            return await m.answer("عدد بفرست.")
    set_setting(key, v)
    await state.clear()
    await m.answer("✅ ذخیره شد.\n\n" + mgmt_text(), reply_markup=mgmt_menu())


@router.callback_query(F.data == "adj", F.from_user.id.in_(OWNERS))
async def adj_start(c: CallbackQuery, state: FSMContext):
    await state.set_state(S.adjust)
    await show(c, "بفرست: <code>آیدی_عددی مبلغ</code>\nمثال: <code>123456 50000</code> (منفی = کسر)",
               kb([[("🔙 انصراف", "mgmt")]]), parse_mode="HTML")


@router.message(S.adjust, F.from_user.id.in_(OWNERS))
async def adj_value(m: Message, state: FSMContext):
    try:
        uid, amt = m.text.split()
        uid, amt = int(uid), float(amt)
        assert customer(uid)
    except Exception:
        return await m.answer("فرمت نادرسته یا کاربر پیدا نشد.")
    await add_balance(uid, amt)
    await state.clear()
    await m.answer("✅ انجام شد.", reply_markup=mgmt_menu())


@router.callback_query(F.data == "cl", F.from_user.id.in_(OWNERS))
async def customer_list(c: CallbackQuery):
    rows = [[(f"{'⛔️' if a['blocked'] else '🟢'} {a['name'][:15]} | {a['tg_id']} | {a['balance']:,.0f}", f"cb:{a['tg_id']}")]
            for a in db.execute("SELECT * FROM customers ORDER BY balance DESC LIMIT 40")]
    rows.append([("🔙 بازگشت", "mgmt")])
    await show(c, "👥 مشتریان (برای مسدود/آزادسازی لمس کن):", kb(rows))


@router.callback_query(F.data.startswith("cb:"), F.from_user.id.in_(OWNERS))
async def customer_block(c: CallbackQuery):
    uid = int(c.data.split(":")[1])
    db.execute("UPDATE customers SET blocked=1-blocked WHERE tg_id=?", (uid,))
    db.commit()
    a = customer(uid)
    try:
        if a["blocked"]:
            await set_admins(uid, disable=True)
        elif a["balance"] > 0:
            await set_admins(uid, disable=False)
    except Exception as e:
        log.warning(e)
    await customer_list(c)


# ───────────────────────── محاسبه مصرف (پرداخت به‌ازای مصرف) ─────────────────────────
async def set_admins(uid, disable):
    """غیرفعال/فعال‌سازی ادمین‌های یک مشتری توسط ربات"""
    if disable:
        for r in db.execute("SELECT panel_user FROM admins WHERE owner_id=?", (uid,)).fetchall():
            ad = await panel.get_admin(r["panel_user"])
            if not ad.get("is_disabled"):
                await panel.modify_admin(r["panel_user"], is_disabled=True)
                db.execute("UPDATE admins SET bot_disabled=1 WHERE panel_user=?", (r["panel_user"],))
    else:
        for r in db.execute("SELECT panel_user FROM admins WHERE owner_id=? AND bot_disabled=1", (uid,)).fetchall():
            await panel.modify_admin(r["panel_user"], is_disabled=False)
            db.execute("UPDATE admins SET bot_disabled=0, last_tick=? WHERE panel_user=?", (time.time(), r["panel_user"]))
    db.commit()


async def suspend_customer(a):
    await set_admins(a["tg_id"], True)
    db.execute("UPDATE customers SET suspended=1 WHERE tg_id=?", (a["tg_id"],))
    db.commit()
    await bot.send_message(a["tg_id"], "⛔️ موجودی کیف پولت تموم شد و ادمین‌هات غیرفعال شدن. شارژ کن تا دوباره فعال بشن.")


async def resume_customer(a):
    await set_admins(a["tg_id"], False)
    db.execute("UPDATE customers SET suspended=0 WHERE tg_id=?", (a["tg_id"],))
    db.commit()
    await bot.send_message(a["tg_id"], "✅ ادمین‌هات دوباره فعال شدن.")


async def bill_customer(a):
    price_gb, price_hour = float(setting("price_gb")), float(setting("price_hour"))
    now, cost = time.time(), 0.0
    for r in db.execute("SELECT * FROM admins WHERE owner_id=?", (a["tg_id"],)).fetchall():
        try:
            ad = await panel.get_admin(r["panel_user"])
        except Exception as e:
            log.warning("get_admin %s: %s", r["panel_user"], e)
            continue  # دور بعد جبران می‌شه
        used = usage_of(ad)
        cost += max(0, used - r["last_used"]) / GB * price_gb
        if not ad.get("is_disabled"):
            cost += max(0, now - (r["last_tick"] or now)) / 3600 * price_hour
        db.execute("UPDATE admins SET last_used=?, last_tick=? WHERE panel_user=?", (used, now, r["panel_user"]))
    db.execute("UPDATE customers SET balance=balance-? WHERE tg_id=?", (cost, a["tg_id"]))
    db.commit()
    a = customer(a["tg_id"])
    if a["balance"] <= 0 and not a["suspended"]:
        await suspend_customer(a)


async def billing_loop():
    while True:
        await asyncio.sleep(BILLING_INTERVAL)
        for a in db.execute("SELECT * FROM customers WHERE suspended=0 AND blocked=0").fetchall():
            if not db.execute("SELECT 1 FROM admins WHERE owner_id=?", (a["tg_id"],)).fetchone():
                continue
            try:
                await bill_customer(a)
            except Exception as e:
                log.error("billing %s: %s", a["tg_id"], e)


async def main():
    dp = Dispatcher()
    dp.include_router(router)
    asyncio.create_task(billing_loop())
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
