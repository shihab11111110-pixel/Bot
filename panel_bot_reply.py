import os
import time
import sqlite3
import logging

import httpx
from telegram import (
    Update,
    InlineKeyboardButton as B,
    InlineKeyboardMarkup as M,
    ReplyKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

logging.basicConfig(level=logging.INFO)

# ------------------------------------------------------------------ config
BOT_TOKEN = os.environ["8839720993:AAGqRkDtId0km978h3mLLX3Rdpdpv__xptM"]
API_KEY = os.environ["MIAHSMS_1977552A19A5FB890F3671E4"]
ADMIN_IDS = {int(x) for x in os.environ.get("ADMIN_IDS", "7348756318").split(",") if x.strip()}

BOT_USERNAME = "Testallcoadfilebot"
BOT_URL = f"https://t.me/{BOT_USERNAME}"
CHANNEL_USERNAME = "@mathodhub"
CHANNEL_URL = "https://t.me/mathodhub"
GROUP_ID = -1003960082838
GROUP_URL = "https://t.me/stmotpgroup"

BASE = "https://miahsms.com/bot/api"
HEADERS = {"X-API-Key": API_KEY}

POLL_SECONDS = 5
NUMBER_TTL = 600
CHANGE_COOLDOWN = 5
MAX_ACTIVE = 1
DEFAULT_PAY = 0.50
DEFAULT_MIN_WD = 20.0

# ------------------------------------------------------------------ database
db = sqlite3.connect("panel.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript(
    """
CREATE TABLE IF NOT EXISTS users(
    id INTEGER PRIMARY KEY, name TEXT, balance REAL DEFAULT 0,
    banned INTEGER DEFAULT 0, joined INTEGER);
CREATE TABLE IF NOT EXISTS numbers(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, number TEXT,
    range TEXT, status TEXT, otp TEXT, pay REAL DEFAULT 0, created INTEGER);
CREATE TABLE IF NOT EXISTS withdrawals(
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER, method TEXT,
    account TEXT, amount REAL, status TEXT, created INTEGER);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""
)
db.commit()

last_change = {}


def now():
    return int(time.time())


def get_setting(key, default):
    r = db.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return type(default)(r["value"]) if r else default


def set_setting(key, value):
    db.execute("INSERT OR REPLACE INTO settings(key,value) VALUES(?,?)", (key, str(value)))
    db.commit()


def get_user(tg_user):
    r = db.execute("SELECT * FROM users WHERE id=?", (tg_user.id,)).fetchone()
    if not r:
        db.execute(
            "INSERT INTO users(id,name,joined) VALUES(?,?,?)",
            (tg_user.id, tg_user.full_name, now()),
        )
        db.commit()
        r = db.execute("SELECT * FROM users WHERE id=?", (tg_user.id,)).fetchone()
    return r


def waiting_number(uid):
    return db.execute(
        "SELECT * FROM numbers WHERE user_id=? AND status='waiting' ORDER BY id DESC LIMIT 1", (uid,)
    ).fetchone()


def cancel_waiting(uid):
    db.execute("UPDATE numbers SET status='cancelled' WHERE user_id=? AND status='waiting'", (uid,))
    db.commit()


async def api(method, path, **kw):
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.request(method, f"{BASE}/{path}", headers=HEADERS, **kw)
        return r.json()


# ------------------------------------------------------------------ keyboards (reply buttons)
BTN_NUMBER = "📱 নম্বর নিন"
BTN_BALANCE = "💰 ব্যালেন্স"
BTN_WITHDRAW = "💸 উইথড্র"
BTN_HISTORY = "📜 হিস্ট্রি"
BTN_GROUP = "👥 OTP Group"
BTN_SUPPORT = "🆘 সাপোর্ট"
BTN_BACK = "⬅️ Back"
BTN_CHANGE = "🔄 নম্বর বদলান"
BTN_VERIFY = "✅ Verify"
BTN_HOME = "🏠 মেইন মেনু"

MENU = ReplyKeyboardMarkup(
    [[BTN_NUMBER, BTN_BALANCE], [BTN_WITHDRAW, BTN_HISTORY], [BTN_GROUP, BTN_SUPPORT]],
    resize_keyboard=True,
)
VERIFY_KB = ReplyKeyboardMarkup([[BTN_VERIFY]], resize_keyboard=True)
NUMBER_KB = ReplyKeyboardMarkup([[BTN_CHANGE, BTN_BACK], [BTN_GROUP]], resize_keyboard=True)

AD_STATS, AD_USERS, AD_WD, AD_RATE = "📊 স্ট্যাটাস", "👤 ইউজার লিস্ট", "💸 উইথড্র রিকোয়েস্ট", "🪙 প্রতি OTP রেট"
AD_BAL, AD_BAN, AD_BC, AD_CFG = "➕ ব্যালেন্স যোগ/কাটা", "🚫 ব্যান/আনব্যান", "📣 ব্রডকাস্ট", "⚙️ তথ্য"
ADMIN_KB = ReplyKeyboardMarkup(
    [[AD_STATS, AD_USERS], [AD_WD, AD_RATE], [AD_BAL, AD_BAN], [AD_BC, AD_CFG], [BTN_HOME]],
    resize_keyboard=True,
)
ADMIN_BTNS = [AD_STATS, AD_USERS, AD_WD, AD_RATE, AD_BAL, AD_BAN, AD_BC, AD_CFG]

# লিংক-বাটন শুধু inline হতে পারে (Telegram-এর নিয়ম)
JOIN_LINKS = M([[B("📢 Main channel", url=CHANNEL_URL), B("👥 OTP group", url=GROUP_URL)]])
GROUP_LINK = M([[B("👥 OTP group-এ যান", url=GROUP_URL)]])
GROUP_POST_KB = M([[B("📢 Main channel", url=CHANNEL_URL), B("🤖 Bot-এ যান", url=BOT_URL)]])

# ------------------------------------------------------------------ join gate
async def is_joined(bot, uid):
    for chat in (CHANNEL_USERNAME, GROUP_ID):
        try:
            m = await bot.get_chat_member(chat, uid)
        except Exception:
            logging.warning("getChatMember failed for %s — বট কি ওই চ্যানেল/গ্রুপের অ্যাডমিন?", chat)
            return False
        if m.status in ("left", "kicked"):
            return False
        if m.status == "restricted" and not getattr(m, "is_member", True):
            return False
    return True


async def gate(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    u = get_user(user)
    if u["banned"]:
        return False
    if user.id in ADMIN_IDS or await is_joined(ctx.bot, user.id):
        return True
    msg = update.effective_message
    await msg.reply_text("বট ব্যবহার করতে আগে নিচের চ্যানেল ও গ্রুপে join করুন।", reply_markup=JOIN_LINKS)
    await msg.reply_text("join করা হলে নিচের Verify বাটন চাপুন।", reply_markup=VERIFY_KB)
    return False


async def go_home(msg, ctx, text=None):
    ctx.user_data["state"] = None
    pay = get_setting("pay", DEFAULT_PAY)
    await msg.reply_text(
        text or f"স্বাগতম!\n\nনম্বর নিন, সেই নম্বরে কোড পাঠান। প্রতিটি OTP-র জন্য ৳{pay:.2f} পাবেন।",
        reply_markup=MENU,
    )


async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await gate(update, ctx):
        await go_home(update.message, ctx)


async def on_verify(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    get_user(user)
    if user.id in ADMIN_IDS or await is_joined(ctx.bot, user.id):
        await go_home(update.message, ctx, "Verify হয়েছে! ✅")
    else:
        await update.message.reply_text("এখনো দুটোতেই join করেননি।", reply_markup=JOIN_LINKS)


async def on_home(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await gate(update, ctx):
        await go_home(update.message, ctx, "মেইন মেনু")

# ------------------------------------------------------------------ number flow
async def show_ranges(msg, ctx):
    try:
        res = await api("GET", "liveaccess")
    except Exception:
        logging.exception("liveaccess failed")
        await go_home(msg, ctx, "সার্ভারে সমস্যা, একটু পরে চেষ্টা করুন।")
        return
    rows = [
        [f"{s.get('sid')} • {rg}"]
        for s in res.get("services", [])
        for rg in s.get("ranges", [])
    ][:20]
    if not rows:
        await go_home(msg, ctx, "এখন কোনো সক্রিয় রেঞ্জ নেই, একটু পরে চেষ্টা করুন।")
        return
    ctx.user_data["state"] = "ranges"
    rows.append([BTN_BACK])
    await msg.reply_text("রেঞ্জ বেছে নিন:", reply_markup=ReplyKeyboardMarkup(rows, resize_keyboard=True))


async def menu_number(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await gate(update, ctx):
        await show_ranges(update.message, ctx)


async def allocate(msg, ctx, uid, rg):
    try:
        res = await api("POST", "getnum", json={"range": rg})
    except Exception:
        logging.exception("getnum failed")
        await msg.reply_text("সার্ভারে সমস্যা, একটু পরে চেষ্টা করুন।")
        await show_ranges(msg, ctx)
        return
    code = res.get("meta", {}).get("code")
    if code == 2946:
        await msg.reply_text("এই রেঞ্জে এখন স্টক নেই, অন্য রেঞ্জ চেষ্টা করুন।")
        await show_ranges(msg, ctx)
        return
    if code != 200 or not res.get("data"):
        await msg.reply_text("নম্বর পাওয়া যায়নি, আবার চেষ্টা করুন।")
        await show_ranges(msg, ctx)
        return
    d = res["data"]
    db.execute(
        "INSERT INTO numbers(user_id,number,range,status,created) VALUES(?,?,?,?,?)",
        (uid, d["full_number"], rg, "waiting", now()),
    )
    db.commit()
    ctx.user_data["state"] = "number"
    pay = get_setting("pay", DEFAULT_PAY)
    await msg.reply_text(
        f"নম্বর: {d['full_number']}\nদেশ: {d.get('country')}\nঅপারেটর: {d.get('operator')}\n\n"
        f"এই নম্বরে কোড পাঠান। OTP এলে ৳{pay:.2f} পাবেন।",
        reply_markup=NUMBER_KB,
    )


async def on_range_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    uid = update.effective_user.id
    rg = update.message.text.split("•")[-1].strip()
    active = db.execute(
        "SELECT COUNT(*) c FROM numbers WHERE user_id=? AND status='waiting'", (uid,)
    ).fetchone()["c"]
    if active >= MAX_ACTIVE:
        await update.message.reply_text(
            "আগের নম্বরের OTP আসা পর্যন্ত অপেক্ষা করুন, অথবা বদলান / Back চাপুন।", reply_markup=NUMBER_KB
        )
        return
    await allocate(update.message, ctx, uid, rg)


async def on_change(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    uid = update.effective_user.id
    row = waiting_number(uid)
    if not row:
        await update.message.reply_text("এখন কোনো চলমান নম্বর নেই, রেঞ্জ বেছে নিন।")
        await show_ranges(update.message, ctx)
        return
    wait = CHANGE_COOLDOWN - (time.time() - last_change.get(uid, 0))
    if wait > 0:
        await update.message.reply_text(f"{int(wait) + 1} সেকেন্ড পরে চেষ্টা করুন।")
        return
    last_change[uid] = time.time()
    cancel_waiting(uid)
    await allocate(update.message, ctx, uid, row["range"])


async def on_back(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    uid = update.effective_user.id
    if ctx.user_data.get("state") == "number" or waiting_number(uid):
        cancel_waiting(uid)
        await show_ranges(update.message, ctx)
    else:
        await go_home(update.message, ctx, "মেইন মেনু")

# ------------------------------------------------------------------ other user menus
async def menu_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    u = get_user(update.effective_user)
    done = db.execute(
        "SELECT COUNT(*) c FROM numbers WHERE user_id=? AND status='done'", (u["id"],)
    ).fetchone()["c"]
    await update.message.reply_text(f"ব্যালেন্স: ৳{u['balance']:.2f}\nমোট সফল OTP: {done}")


async def menu_history(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    rows = db.execute(
        "SELECT * FROM numbers WHERE user_id=? ORDER BY id DESC LIMIT 10", (update.effective_user.id,)
    ).fetchall()
    if not rows:
        await update.message.reply_text("কোনো হিস্ট্রি নেই।")
        return
    await update.message.reply_text(
        "\n".join(
            f"{r['number']} — {r['status']}" + (f" (৳{r['pay']:.2f})" if r["status"] == "done" else "")
            for r in rows
        )
    )


async def menu_group(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await gate(update, ctx):
        await update.message.reply_text("OTP group:", reply_markup=GROUP_LINK)


async def menu_support(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if await gate(update, ctx):
        await update.message.reply_text(
            "সাহায্যের জন্য আমাদের main channel-এ যোগাযোগ করুন।",
            reply_markup=M([[B("📢 Main channel", url=CHANNEL_URL)]]),
        )


async def menu_withdraw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    mn = get_setting("min_wd", DEFAULT_MIN_WD)
    await update.message.reply_text(
        f"উইথড্র করতে লিখুন:\n/withdraw bkash 01XXXXXXXXX 50\n\nসর্বনিম্ন: ৳{mn:.0f}\nমেথড: bkash / nagad"
    )


def wd_kb(wid):
    return M([[B("✅ পেমেন্ট করেছি", callback_data=f"wa:{wid}"), B("❌ বাতিল", callback_data=f"wr:{wid}")]])


async def withdraw(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not await gate(update, ctx):
        return
    u = get_user(update.effective_user)
    try:
        method, account, amount = ctx.args[0].lower(), ctx.args[1], float(ctx.args[2])
    except Exception:
        await menu_withdraw(update, ctx)
        return
    mn = get_setting("min_wd", DEFAULT_MIN_WD)
    if method not in ("bkash", "nagad"):
        await update.message.reply_text("মেথড হতে হবে bkash অথবা nagad।")
        return
    if amount < mn:
        await update.message.reply_text(f"সর্বনিম্ন উইথড্র ৳{mn:.0f}।")
        return
    cur = db.execute(
        "UPDATE users SET balance = balance - ? WHERE id=? AND balance >= ?", (amount, u["id"], amount)
    )
    if cur.rowcount == 0:
        db.rollback()
        await update.message.reply_text("ব্যালেন্স যথেষ্ট নয়।")
        return
    w = db.execute(
        "INSERT INTO withdrawals(user_id,method,account,amount,status,created) VALUES(?,?,?,?,?,?)",
        (u["id"], method, account, amount, "pending", now()),
    )
    db.commit()
    await update.message.reply_text("উইথড্র রিকোয়েস্ট জমা হয়েছে। অ্যাডমিন অ্যাপ্রুভ করলে জানানো হবে।")
    for aid in ADMIN_IDS:
        await ctx.bot.send_message(
            aid,
            f"নতুন উইথড্র #{w.lastrowid}\nইউজার: {u['id']} ({u['name']})\n{method}: {account}\nপরিমাণ: ৳{amount:.2f}",
            reply_markup=wd_kb(w.lastrowid),
        )


async def on_withdraw_action(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if q.from_user.id not in ADMIN_IDS:
        await q.answer("অনুমতি নেই", show_alert=True)
        return
    action, wid = q.data.split(":")
    w = db.execute("SELECT * FROM withdrawals WHERE id=? AND status='pending'", (int(wid),)).fetchone()
    if not w:
        await q.answer("ইতিমধ্যে প্রক্রিয়া হয়েছে")
        return
    if action == "wa":
        db.execute("UPDATE withdrawals SET status='paid' WHERE id=?", (w["id"],))
        msg = f"আপনার ৳{w['amount']:.2f} উইথড্র সম্পন্ন হয়েছে।"
    else:
        db.execute("UPDATE withdrawals SET status='rejected' WHERE id=?", (w["id"],))
        db.execute("UPDATE users SET balance = balance + ? WHERE id=?", (w["amount"], w["user_id"]))
        msg = f"আপনার ৳{w['amount']:.2f} উইথড্র বাতিল হয়েছে, টাকা ব্যালেন্সে ফেরত গেছে।"
    db.commit()
    await q.answer("হয়ে গেছে")
    await q.message.edit_text(q.message.text + f"\n\n→ {'পেমেন্ট সম্পন্ন' if action == 'wa' else 'বাতিল'}")
    await ctx.bot.send_message(w["user_id"], msg)

# ------------------------------------------------------------------ OTP polling
def mask_number(n):
    return n[:6] + "****" + n[-3:] if len(n) > 9 else n


def mask_otp(o):
    o = str(o)
    if len(o) >= 5:
        return o[:2] + "*" * (len(o) - 4) + o[-2:]
    return o[:1] + "*" * (len(o) - 1)


def reset_state(ctx, uid):
    try:
        ctx.application.user_data[uid]["state"] = None
    except Exception:
        pass


async def poll_otps(ctx: ContextTypes.DEFAULT_TYPE):
    t = now()
    for r in db.execute(
        "SELECT * FROM numbers WHERE status='waiting' AND created < ?", (t - NUMBER_TTL,)
    ).fetchall():
        db.execute("UPDATE numbers SET status='expired' WHERE id=?", (r["id"],))
        db.commit()
        reset_state(ctx, r["user_id"])
        try:
            await ctx.bot.send_message(r["user_id"], f"{r['number']} নম্বরে OTP আসেনি, সময় শেষ।", reply_markup=MENU)
        except Exception:
            pass

    last_id = get_setting("last_id", 0)
    try:
        res = await api("GET", "live-console", params={"since": last_id, "limit": 200})
        data = res.get("data", {})
    except Exception:
        logging.exception("poll failed")
        return

    max_id = data.get("max_id", last_id)
    if last_id == 0:
        set_setting("last_id", max_id)
        return

    pay = get_setting("pay", DEFAULT_PAY)
    for item in data.get("otps", []):
        number = item.get("number")
        row = db.execute(
            "SELECT * FROM numbers WHERE number=? AND status='waiting' ORDER BY id DESC LIMIT 1", (number,)
        ).fetchone()
        if not row:
            continue
        if int(item.get("time", 0)) < row["created"] * 1000 - 5000:
            continue
        cur = db.execute(
            "UPDATE numbers SET status='done', otp=?, pay=? WHERE id=? AND status='waiting'",
            (item.get("otp"), pay, row["id"]),
        )
        if cur.rowcount == 0:
            continue
        db.execute("UPDATE users SET balance = balance + ? WHERE id=?", (pay, row["user_id"]))
        db.commit()
        reset_state(ctx, row["user_id"])

        try:
            await ctx.bot.send_message(
                row["user_id"],
                f"✅ OTP পাওয়া গেছে!\nনম্বর: {number}\nকোড: {item.get('otp')}\n"
                f"প্ল্যাটফর্ম: {item.get('platform')}\n+৳{pay:.2f} যোগ হয়েছে।",
                reply_markup=MENU,
            )
        except Exception:
            logging.exception("user notify failed")

        try:
            await ctx.bot.send_message(
                GROUP_ID,
                f"{item.get('platform')} OTP\nদেশ: {item.get('country')}\n"
                f"নম্বর: {mask_number(number)}\nকোড: {mask_otp(item.get('otp'))}",
                reply_markup=GROUP_POST_KB,
            )
        except Exception:
            logging.exception("group post failed — বট কি গ্রুপে অ্যাডমিন?")
    set_setting("last_id", max_id)

# ------------------------------------------------------------------ admin (reply buttons)
def stats_text():
    users = db.execute("SELECT COUNT(*) c, COALESCE(SUM(balance),0) b FROM users").fetchone()
    done = db.execute("SELECT COUNT(*) c, COALESCE(SUM(pay),0) p FROM numbers WHERE status='done'").fetchone()
    pend = db.execute("SELECT COUNT(*) c FROM withdrawals WHERE status='pending'").fetchone()["c"]
    return (
        f"ইউজার: {users['c']}\nইউজারদের মোট ব্যালেন্স (দেনা): ৳{users['b']:.2f}\n"
        f"সফল OTP: {done['c']}\nমোট পরিশোধযোগ্য: ৳{done['p']:.2f}\nপেন্ডিং উইথড্র: {pend}"
    )


async def admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id in ADMIN_IDS:
        await update.message.reply_text("অ্যাডমিন প্যানেল", reply_markup=ADMIN_KB)


async def on_admin_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS:
        return
    msg, act = update.message, update.message.text
    if act == AD_STATS:
        await msg.reply_text(stats_text())
    elif act == AD_USERS:
        rows = db.execute("SELECT * FROM users ORDER BY joined DESC LIMIT 15").fetchall()
        await msg.reply_text(
            "\n".join(
                f"{r['id']} | {r['name']} | ৳{r['balance']:.2f}" + (" | banned" if r["banned"] else "")
                for r in rows
            )
            or "কোনো ইউজার নেই।"
        )
    elif act == AD_WD:
        rows = db.execute("SELECT * FROM withdrawals WHERE status='pending' ORDER BY id LIMIT 10").fetchall()
        if not rows:
            await msg.reply_text("কোনো পেন্ডিং উইথড্র নেই।")
        for w in rows:
            await msg.reply_text(
                f"উইথড্র #{w['id']}\nইউজার: {w['user_id']}\n{w['method']}: {w['account']}\nপরিমাণ: ৳{w['amount']:.2f}",
                reply_markup=wd_kb(w["id"]),
            )
    elif act == AD_RATE:
        await msg.reply_text(
            f"বর্তমান রেট: ৳{get_setting('pay', DEFAULT_PAY):.2f} প্রতি OTP\n"
            f"সর্বনিম্ন উইথড্র: ৳{get_setting('min_wd', DEFAULT_MIN_WD):.0f}\n\n"
            "বদলাতে: /setpay 0.75\n/setmin 30"
        )
    elif act == AD_BAL:
        await msg.reply_text(
            "ব্যালেন্স বদলাতে:\n/addbalance USER_ID পরিমাণ\nকাটতে ঋণাত্মক দিন, যেমন: /addbalance 123 -5"
        )
    elif act == AD_BAN:
        await msg.reply_text("/ban USER_ID\n/unban USER_ID")
    elif act == AD_BC:
        await msg.reply_text("সবাইকে মেসেজ পাঠাতে:\n/broadcast আপনার মেসেজ")
    elif act == AD_CFG:
        await msg.reply_text(
            f"Bot: @{BOT_USERNAME}\nMain channel: {CHANNEL_USERNAME}\nOTP group ID: {GROUP_ID}\n"
            f"OTP group link: {GROUP_URL}\nঅ্যাডমিন: {', '.join(str(a) for a in ADMIN_IDS)}\n\n"
            "বদলাতে ফাইলের উপরের কনফিগ অংশ এডিট করুন।"
        )


async def stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id in ADMIN_IDS:
        await update.message.reply_text(stats_text())


async def setpay(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id in ADMIN_IDS and ctx.args:
        set_setting("pay", float(ctx.args[0]))
        await update.message.reply_text(f"প্রতি OTP এখন ৳{float(ctx.args[0]):.2f}")


async def setmin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id in ADMIN_IDS and ctx.args:
        set_setting("min_wd", float(ctx.args[0]))
        await update.message.reply_text(f"সর্বনিম্ন উইথড্র এখন ৳{float(ctx.args[0]):.0f}")


async def addbalance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id in ADMIN_IDS and len(ctx.args) == 2:
        db.execute(
            "UPDATE users SET balance = MAX(0, balance + ?) WHERE id=?", (float(ctx.args[1]), int(ctx.args[0]))
        )
        db.commit()
        await update.message.reply_text("ঠিক আছে।")


async def ban(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id in ADMIN_IDS and ctx.args:
        flag = 1 if update.message.text.startswith("/ban") else 0
        db.execute("UPDATE users SET banned=? WHERE id=?", (flag, int(ctx.args[0])))
        db.commit()
        await update.message.reply_text("ঠিক আছে।")


async def broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id not in ADMIN_IDS or not ctx.args:
        return
    text = " ".join(ctx.args)
    n = 0
    for r in db.execute("SELECT id FROM users WHERE banned=0").fetchall():
        try:
            await ctx.bot.send_message(r["id"], text)
            n += 1
        except Exception:
            pass
    await update.message.reply_text(f"{n} জনকে পাঠানো হয়েছে।")

# ------------------------------------------------------------------ main
def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(CommandHandler("withdraw", withdraw))
    app.add_handler(CommandHandler("stats", stats))
    app.add_handler(CommandHandler("setpay", setpay))
    app.add_handler(CommandHandler("setmin", setmin))
    app.add_handler(CommandHandler("addbalance", addbalance))
    app.add_handler(CommandHandler(["ban", "unban"], ban))
    app.add_handler(CommandHandler("broadcast", broadcast))

    app.add_handler(MessageHandler(filters.Text([BTN_VERIFY]), on_verify))
    app.add_handler(MessageHandler(filters.Text([BTN_HOME]), on_home))
    app.add_handler(MessageHandler(filters.Text([BTN_NUMBER]), menu_number))
    app.add_handler(MessageHandler(filters.Text([BTN_BALANCE]), menu_balance))
    app.add_handler(MessageHandler(filters.Text([BTN_WITHDRAW]), menu_withdraw))
    app.add_handler(MessageHandler(filters.Text([BTN_HISTORY]), menu_history))
    app.add_handler(MessageHandler(filters.Text([BTN_GROUP]), menu_group))
    app.add_handler(MessageHandler(filters.Text([BTN_SUPPORT]), menu_support))
    app.add_handler(MessageHandler(filters.Text([BTN_CHANGE]), on_change))
    app.add_handler(MessageHandler(filters.Text([BTN_BACK]), on_back))
    app.add_handler(MessageHandler(filters.Text(ADMIN_BTNS), on_admin_text))
    app.add_handler(MessageHandler(filters.Regex(" • "), on_range_text))

    app.add_handler(CallbackQueryHandler(on_withdraw_action, pattern="^w[ar]:"))

    app.job_queue.run_repeating(poll_otps, interval=POLL_SECONDS, first=3)
    app.run_polling()


if __name__ == "__main__":
    main()
