#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VOLTX OTP HUB  (Telegram bot + Voltx panel + Firebase Firestore)

Setup: scroll to the "EDIT HERE" block below and fill
  1) BOT_TOKEN        (from @BotFather)
  2) OWNER_ID_LIST    (your numeric Telegram id)
  3) VOLTX_API_KEYS   (optional - you can also add keys later from Owner Panel > VOLTX CONTROL)
  4) Firebase         (optional - without it the bot keeps data in the local file voltx_data.json.
                       Later: Owner Panel > FIREBASE > UPLOAD JSON, or paste the JSON into FIREBASE_JSON)

Run only ONE instance of this bot (long polling).
"""
import os
import re
import sys
import json
import time
import hmac
import copy
import base64
import struct
import hashlib
import html
import asyncio
import logging
import threading
from collections import Counter, deque
from concurrent.futures import ThreadPoolExecutor

import requests

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("voltxbot")

# ----------------------------------------------------------------------------
# CONFIG
# ----------------------------------------------------------------------------
# ============================ EDIT HERE ============================
BOT_TOKEN = "8825717139:AAGGCznA_FlJPKQjE0ZUN69pmKYjOZVnd9w"      # BotFather token, example: "123456789:AAH...."
OWNER_ID_LIST = [8304747658]                         # your Telegram numeric id(s), example: [123456789] or [123456789, 987654321]
VOLTX_API_KEYS = []                         # optional, example: ["MAB12CD34EF..."]  (more keys can be added from the bot)
FIREBASE_JSON = ""                         # leave empty for testing; later paste the service-account JSON here (r""" ... """)
# ===================================================================

BOT_KEY = (os.environ.get("TELEGRAM_BOT_TOKEN") or BOT_TOKEN).strip()
ADMINS = {int(x) for x in OWNER_ID_LIST} | {int(x) for x in re.findall(r"\d+", os.environ.get("OWNER_ID", ""))}
VX_BASE = os.environ.get(
    "VOLTX_BASE_URL", "https://api.2oo9.cloud/MXS47FLFX0U/tnevs/@public/api"
).strip().rstrip("/")
TG_API = f"https://api.telegram.org/bot{BOT_KEY}"

KEEP_SECS = 30 * 60          # keep console hits this long (seconds)
RANGE_SECS = 20 * 60      # ranges used for Get Number
VIEW_SECS = 15 * 60    # traffic page window
HOLD_SECS = 60 * 60        # a number belongs to the user this long

HUE_A, HUE_B, HUE_C = "primary", "success", "danger"   # hue A, hue B, stop colour

BTN_GET = "☎️ GET NUMBER"
BTN_2FA = "🔐 2FA CODE"
BTN_TRF = "📊 LIVE TRAFFIC"
BTN_REF = "💸 REFER & EARN"
BTN_BAL = "💰 BALANCE"
BTN_BOARD = "🏆 LEADERBOARD"
BTN_OWN = "👑 OWNER PANEL"
MENU_LABELS = {BTN_GET, BTN_2FA, BTN_TRF, BTN_REF, BTN_BAL, BTN_BOARD, BTN_OWN}


# ---- fancy unicode fonts (like the reference bot)
def _alpha(t, up, lo):
    out = []
    for ch in str(t):
        if "A" <= ch <= "Z":
            out.append(chr(up + ord(ch) - 65))
        elif "a" <= ch <= "z":
            out.append(chr(lo + ord(ch) - 97))
        else:
            out.append(ch)
    return "".join(out)


def bd(t):      # serif bold  (messages)
    return _alpha(t, 0x1D5D4, 0x1D5EE)


def bdi(t):     # serif bold italic (messages)
    return _alpha(t, 0x1D608, 0x1D622)


def bt(t):      # sans bold italic (button labels)
    return _alpha(t, 0x1D670, 0x1D68A)


LINE = "┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈┈"
DASH = "◦ ◦ ◦ ◦ ◦ ◦"
THIN = "╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌╌"


def n1(x):
    t = f"{float(x):.4f}".rstrip("0")
    return t + "0" if t.endswith(".") else t


PREV_WELCOME = "👋 Welcome, {name}!\n\nNumber নিয়ে OTP রিসিভ করতে নিচের মেনু ব্যবহার করুন।"
WELCOME_TEXT = (
    "╭────────────────╮\n"
    "     ⚡ " + bd("VOLTX OTP HUB") + "\n"
    "╰────────────────╯\n\n"
    "⚡ " + bd("Fast Number & OTP Service") + "\n"
    + THIN + "\n"
    "✅ " + bd("Pick an option below") + "\n"
    + bd("to get started.") + "\n"
    + THIN + "\n"
    "💠 " + bd("Instant • Reliable • Premium")
)


CFG_DEFAULTS = {
    "welcome": WELCOME_TEXT,
    "vx_on": True,
    "vx_keys": [],
    "services": {},          # key -> {"name": str, "rate": float|None}
    "num_count": 1,
    "otp_reward": 0.10,
    "min_withdraw": 30.0,
    "ref_reward": 0.50,
    "w_methods": ["bKash", "Nagad"],
    "w_group": "",
    "main_channel": "",
    "otp_group_link": "",
    "fj_on": False,
    "fj_channels": [],       # [{"chat": "@x" | -100.., "link": "https://t.me/x"}]
    "fw_on": True,
    "live_on": False,
    "fw_groups": [],         # chat ids / @usernames of OTP groups
}

CFG_DEFAULTS.update({
    "num_time": 15,          # minutes shown on the number message
    "cooldown": 5,           # seconds between two number requests of one user
    "ref_commission": 0.0,   # % of a referred user's OTP rate paid to the referrer on every OTP
    "maint": False,          # maintenance mode
})
DB_COLLECTIONS = ["vx_settings", "vx_users", "vx_assigned", "vx_withdraws"]

# ----------------------------------------------------------------------------
# COUNTRY TABLE  (calling code -> (ISO, name))
# ----------------------------------------------------------------------------
DIAL = {}
for _row in """1:US:United States;7:RU:Russia;20:EG:Egypt;27:ZA:South Africa;30:GR:Greece;31:NL:Netherlands;
32:BE:Belgium;33:FR:France;34:ES:Spain;36:HU:Hungary;39:IT:Italy;40:RO:Romania;41:CH:Switzerland;
43:AT:Austria;44:GB:United Kingdom;45:DK:Denmark;46:SE:Sweden;47:NO:Norway;48:PL:Poland;49:DE:Germany;
51:PE:Peru;52:MX:Mexico;53:CU:Cuba;54:AR:Argentina;55:BR:Brazil;56:CL:Chile;57:CO:Colombia;
58:VE:Venezuela;60:MY:Malaysia;61:AU:Australia;62:ID:Indonesia;63:PH:Philippines;64:NZ:New Zealand;
65:SG:Singapore;66:TH:Thailand;81:JP:Japan;82:KR:South Korea;84:VN:Vietnam;86:CN:China;90:TR:Turkey;
91:IN:India;92:PK:Pakistan;93:AF:Afghanistan;94:LK:Sri Lanka;95:MM:Myanmar;98:IR:Iran;
211:SS:South Sudan;212:MA:Morocco;213:DZ:Algeria;216:TN:Tunisia;218:LY:Libya;220:GM:Gambia;
221:SN:Senegal;223:ML:Mali;224:GN:Guinea;225:CI:Ivory Coast;226:BF:Burkina Faso;227:NE:Niger;
228:TG:Togo;229:BJ:Benin;230:MU:Mauritius;231:LR:Liberia;232:SL:Sierra Leone;233:GH:Ghana;
234:NG:Nigeria;235:TD:Chad;236:CF:Central African Republic;237:CM:Cameroon;238:CV:Cape Verde;
240:GQ:Equatorial Guinea;241:GA:Gabon;242:CG:Congo;243:CD:DR Congo;244:AO:Angola;245:GW:Guinea-Bissau;
248:SC:Seychelles;249:SD:Sudan;250:RW:Rwanda;251:ET:Ethiopia;252:SO:Somalia;253:DJ:Djibouti;
254:KE:Kenya;255:TZ:Tanzania;256:UG:Uganda;257:BI:Burundi;258:MZ:Mozambique;260:ZM:Zambia;
261:MG:Madagascar;263:ZW:Zimbabwe;264:NA:Namibia;265:MW:Malawi;266:LS:Lesotho;267:BW:Botswana;
351:PT:Portugal;352:LU:Luxembourg;353:IE:Ireland;354:IS:Iceland;355:AL:Albania;356:MT:Malta;
357:CY:Cyprus;358:FI:Finland;359:BG:Bulgaria;370:LT:Lithuania;371:LV:Latvia;372:EE:Estonia;
373:MD:Moldova;374:AM:Armenia;375:BY:Belarus;380:UA:Ukraine;381:RS:Serbia;385:HR:Croatia;
386:SI:Slovenia;387:BA:Bosnia;420:CZ:Czechia;421:SK:Slovakia;502:GT:Guatemala;503:SV:El Salvador;
504:HN:Honduras;505:NI:Nicaragua;506:CR:Costa Rica;507:PA:Panama;591:BO:Bolivia;593:EC:Ecuador;
595:PY:Paraguay;598:UY:Uruguay;673:BN:Brunei;852:HK:Hong Kong;853:MO:Macau;855:KH:Cambodia;
856:LA:Laos;880:BD:Bangladesh;886:TW:Taiwan;960:MV:Maldives;961:LB:Lebanon;962:JO:Jordan;
963:SY:Syria;964:IQ:Iraq;965:KW:Kuwait;966:SA:Saudi Arabia;967:YE:Yemen;968:OM:Oman;970:PS:Palestine;
971:AE:UAE;972:IL:Israel;973:BH:Bahrain;974:QA:Qatar;975:BT:Bhutan;976:MN:Mongolia;977:NP:Nepal;
992:TJ:Tajikistan;993:TM:Turkmenistan;994:AZ:Azerbaijan;995:GE:Georgia;996:KG:Kyrgyzstan;998:UZ:Uzbekistan
""".replace("\n", "").split(";"):
    _row = _row.strip()
    if _row:
        _c, _i, _n = _row.split(":")
        DIAL[_c] = (_i, _n)
ISO_TITLES = {i: n for i, n in DIAL.values()}
ISO_TITLES["ZZ"] = "Unknown"
ISO_DIAL = {}
for _c, (_i, _n) in DIAL.items():
    ISO_DIAL.setdefault(_i, _c)

ICON_MAP = {"facebook": "📘", "whatsapp": "🟢", "telegram": "✈️", "tiktok": "🎵", "instagram": "📸",
             "google": "🔍", "gmail": "📧", "twitter": "🐦", "snapchat": "👻", "discord": "🎮",
             "viber": "📞", "imo": "💬", "line": "💚", "signal": "🔵", "amazon": "📦", "paypal": "💰",
             "uber": "🚗", "microsoft": "🪟", "apple": "🍎", "netflix": "🎬", "linkedin": "💼",
             "wechat": "💬", "botim": "📱"}


def icon_for(name):
    n = re.sub(r"[^a-z0-9]", "", str(name).lower())
    for k, v in ICON_MAP.items():
        if k in n:
            return v
    return "📲"


def guess_lang(text):
    t = str(text or "")
    for rx, nm in ((r"[\u0600-\u06FF]", "Arabic"), (r"[\u0400-\u04FF]", "Russian"),
                   (r"[\u4E00-\u9FFF]", "Chinese"), (r"[\u0980-\u09FF]", "Bangla"),
                   (r"[\u0900-\u097F]", "Hindi"), (r"[\u0E00-\u0E7F]", "Thai")):
        if re.search(rx, t):
            return nm
    return "English"


def iso_of(only_digits):
    d = re.sub(r"\D", "", str(only_digits))
    for n in (3, 2, 1):
        if d[:n] in DIAL:
            return DIAL[d[:n]][0]
    return "ZZ"


def flag_of(iso):
    iso = (iso or "").upper()
    if len(iso) == 2 and iso.isalpha() and iso != "ZZ":
        return "".join(chr(0x1F1E6 + ord(c) - 65) for c in iso)
    return "🌍"


def country_name(iso):
    return ISO_TITLES.get((iso or "").upper(), "Unknown")


# ----------------------------------------------------------------------------
# SMALL HELPERS
# ----------------------------------------------------------------------------
def htm(x):
    return html.escape(str(x), quote=False)


def clock():
    return time.time()


def only_digits(x):
    return re.sub(r"\D", "", str(x or ""))


def squash(s):
    return re.sub(r"[^a-z0-9]", "", str(s or "").lower())


def svc_key(name):
    return squash(name)[:24] or hashlib.md5(str(name).encode()).hexdigest()[:10]


def fmt_money(x):
    return f"{float(x):.2f}"


def fmt_rate(x):
    return f"{float(x):g}"


def hide_num(num):
    n = only_digits(num)
    if len(n) >= 10:
        return f"+{n[:5]}••{n[-4:]}"
    return f"+{n[:3]}••{n[-2:]}"


def clean_url(s):
    s = str(s or "").strip()
    if not s:
        return ""
    if s.startswith("@"):
        return "https://t.me/" + s[1:]
    if s.lower().startswith(("t.me/", "telegram.me/")):
        return "https://" + s
    if s.lower().startswith(("http://", "https://")):
        return s
    return ""


def read_amount(text):
    t = str(text or "").strip().replace(",", "")
    if re.fullmatch(r"\d+(\.\d+)?", t):
        return float(t)
    return None


def is_admin(who):
    return who in ADMINS


def find_code(text):
    t = str(text or "")
    m = re.search(r"(?:code|otp|pin|password|verification|কোড)[^\d]{0,25}(\d{4,8})", t, re.I)
    if m:
        return m.group(1)
    m = re.search(r"(?<!\d)(\d{3})[\s-](\d{3})(?!\d)", t)
    if m:
        return m.group(1) + m.group(2)
    m = re.search(r"(?<!\d)(\d{4,8})(?!\d)", t)
    return m.group(1) if m else ""


def totp_calc(secret, step=30, nd=6, at=None):
    """RFC 6238 TOTP. Returns (code, seconds_left). Raises ValueError for a bad key."""
    s = str(secret).strip()
    m = re.search(r"secret=([A-Za-z2-7=]+)", s)
    if m:
        s = m.group(1)
    s = re.sub(r"[\s-]", "", s).upper().rstrip("=")
    if not s or not re.fullmatch(r"[A-Z2-7]+", s):
        raise ValueError("bad key")
    s += "=" * ((8 - len(s) % 8) % 8)
    try:
        key = base64.b32decode(s)
    except Exception:
        raise ValueError("bad key")
    t = int(at if at is not None else time.time())
    h = hmac.new(key, struct.pack(">Q", t // step), hashlib.sha1).digest()
    o = h[-1] & 0x0F
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7FFFFFFF) % (10 ** nd)
    return str(code).zfill(nd), step - (t % step)


# ----------------------------------------------------------------------------
# STORAGE  (Firebase Firestore)
# ----------------------------------------------------------------------------
class DataStore:
    """Firestore when connected, otherwise a local file (voltx_data.json). Can be switched from the Owner Panel."""

    def __init__(self):
        self.fs = None
        self.mem = {}
        self.lock = threading.Lock()
        self.path = os.environ.get("LOCAL_DB_FILE", "voltx_data.json")
        self.cfg_path = os.environ.get("LOCAL_CFG_FILE", "voltx_config.json")
        self.fixed = False          # Firebase JSON is written in the code / environment
        self._app_name = None

    # ---- local files
    def _load_local(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self.mem = json.load(f)
        except FileNotFoundError:
            self.mem = {}
        except Exception as e:
            log.warning("local data file unreadable (%s) - starting empty", e)
            self.mem = {}

    def _flush_local(self):
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.mem, f, ensure_ascii=False)
        os.replace(tmp, self.path)

    def cfg_read(self):
        try:
            with open(self.cfg_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def cfg_write(self, d):
        tmp = self.cfg_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(d, f)
        try:
            os.chmod(tmp, 0o600)
        except Exception:
            pass
        os.replace(tmp, self.cfg_path)

    def saved_cred(self):
        c = self.cfg_read().get("firebase") or {}
        return c.get("cred"), bool(c.get("on"))

    # ---- firebase helpers
    def _make_client(self, cred):
        import firebase_admin
        from firebase_admin import credentials, firestore
        name = "vx%d" % int(time.time() * 1000)
        app = firebase_admin.initialize_app(credentials.Certificate(cred), name=name)
        old, self._app_name = self._app_name, name
        if old:
            try:
                firebase_admin.delete_app(firebase_admin.get_app(old))
            except Exception:
                pass
        return firestore.client(app)

    @staticmethod
    def _ping(client):
        ref = client.collection("meta").document("ping")
        ref.set({"t": time.time()})
        ref.get()

    def _push_local(self, client):
        n = 0
        with self.lock:
            snap = copy.deepcopy(self.mem)
        for col, docs in snap.items():
            for doc, data in docs.items():
                client.collection(col).document(doc).set(data)
                n += 1
        return n

    def _pull_all(self, client):
        data = {}
        for col in DB_COLLECTIONS:
            data[col] = {d.id: d.to_dict() for d in client.collection(col).stream()}
        with self.lock:
            self.mem = data
            self._flush_local()

    def init(self):
        self._load_local()
        raw = (os.environ.get("FIREBASE_CREDENTIALS_JSON") or FIREBASE_JSON or "").strip()
        cred = None
        if raw:
            cred = json.loads(raw)
            self.fixed = True
        else:
            c, on = self.saved_cred()
            if c and on:
                cred = c
        if cred:
            try:
                self.fs = self._make_client(cred)
                self._ping(self.fs)
                log.info("Firebase connected")
                if self.fixed:
                    self._migrate_local_to_firebase()
                return
            except Exception as e:
                if self.fixed:
                    raise
                log.warning("saved Firebase could not connect (%s) - using local file", e)
                self.fs = None
        log.warning("=" * 60)
        log.warning("TEST MODE: Firebase is not connected. Data is saved in the local file '%s'.", self.path)
        log.warning("Connect Firebase later from Owner Panel > FIREBASE (upload the JSON file).")
        log.warning("=" * 60)

    def _migrate_local_to_firebase(self):
        """Firebase JSON written in code: copy old local test data once."""
        if not os.path.exists(self.path):
            return
        try:
            if self.fs.collection("vx_settings").document("main").get().exists:
                return
            self._load_local()
            n = self._push_local(self.fs)
            os.replace(self.path, self.path + ".migrated")
            self.mem = {}
            log.info("Local test data copied to Firebase (%d documents).", n)
        except Exception as e:
            log.warning("local -> Firebase copy failed: %s", e)

    def _connect_sync(self, cred):
        if self.fs is not None:
            self._pull_all(self.fs)            # newest data first
        client = self._make_client(cred)
        self._ping(client)
        n = self._push_local(client)
        self.fs = client
        cfg = self.cfg_read()
        cfg["firebase"] = {"cred": cred, "on": True}
        self.cfg_write(cfg)
        return n

    def _disconnect_sync(self):
        if self.fs is not None:
            self._pull_all(self.fs)
        self.fs = None
        cfg = self.cfg_read()
        fb = cfg.get("firebase") or {}
        fb["on"] = False
        cfg["firebase"] = fb
        self.cfg_write(cfg)

    def _test_sync(self):
        t0 = time.time()
        self._ping(self.fs)
        return int((time.time() - t0) * 1000)

    async def connect_firebase(self, cred):
        return await asyncio.to_thread(self._connect_sync, cred)

    async def disconnect_firebase(self):
        await asyncio.to_thread(self._disconnect_sync)

    async def test_firebase(self):
        return await asyncio.to_thread(self._test_sync)

    # ---- generic operations
    def _get(self, col, doc):
        if self.fs is None:
            with self.lock:
                return copy.deepcopy(self.mem.get(col, {}).get(doc))
        s_ = self.fs.collection(col).document(doc).get()
        return s_.to_dict() if s_.exists else None

    def _set(self, col, doc, data):
        if self.fs is None:
            with self.lock:
                self.mem.setdefault(col, {})[doc] = copy.deepcopy(data)
                self._flush_local()
            return
        self.fs.collection(col).document(doc).set(data)

    def _del(self, col, doc):
        if self.fs is None:
            with self.lock:
                self.mem.get(col, {}).pop(doc, None)
                self._flush_local()
            return
        self.fs.collection(col).document(doc).delete()

    def _all(self, col):
        if self.fs is None:
            with self.lock:
                return [(k, copy.deepcopy(v)) for k, v in self.mem.get(col, {}).items()]
        return [(d.id, d.to_dict()) for d in self.fs.collection(col).stream()]

    async def get(self, col, doc):
        return await asyncio.to_thread(self._get, col, str(doc))

    async def set(self, col, doc, data):
        await asyncio.to_thread(self._set, col, str(doc), data)

    async def delete(self, col, doc):
        await asyncio.to_thread(self._del, col, str(doc))

    async def all(self, col):
        return await asyncio.to_thread(self._all, col)


datastore = DataStore()
TASKS = set()


def spawn(coro):
    """run a coroutine in the background, never crash the bot"""
    async def runner():
        try:
            await coro
        except Exception as e:
            log.warning("background task error: %s", e)
    t = asyncio.ensure_future(runner())
    TASKS.add(t)
    t.add_done_callback(TASKS.discard)
    return t


# ----------------------------------------------------------------------------
# GLOBAL STATE
# ----------------------------------------------------------------------------
CFG = {}                 # settings
USERS = {}                 # users: uid -> dict
HELD = {}          # number(no +) -> {uid, service, sname, iso, rate, ts, range}
WDQ = {}                # pending withdraws: id -> dict
TRAFFIC = []              # console hits (dict)
HIT_SEEN = {}         # hit key -> time
SMS_SEEN = {}          # otp id -> time
CODE_BOOK = {}        # otp code -> {"num","t"}  (to avoid duplicate live post)
INPUT = {}                # per-user input state
TOTP_KEYS = {}               # "chat:mid" -> secret
WORKING = set()
REQ_TIME = {}          # uid -> time of last number request
JOIN_OK = {}            # uid -> ts of last successful join check
Q_USER = deque()
Q_LIVE = deque(maxlen=60)
SENT_AT = {}
ME = {"username": ""}


def persist_cfg():
    spawn(datastore.set("vx_settings", "main", copy.deepcopy(CFG)))


def persist_user(who):
    if who in USERS:
        spawn(datastore.set("vx_users", who, copy.deepcopy(USERS[who])))


def make_user(frm):
    return {
        "id": frm["id"],
        "name": (frm.get("first_name") or "User")[:60],
        "username": frm.get("username") or "",
        "balance": 0.0,
        "total_otp": 0,
        "total_numbers": 0,
        "ref_by": 0,
        "ref_count": 0,
        "ref_earn": 0.0,
        "ref_paid": False,
        "joined": int(clock()),
        "d_key": "",
        "d_otp": 0,
        "w_key": "",
        "w_otp": 0,
    }


# ----------------------------------------------------------------------------
# TELEGRAM API
# ----------------------------------------------------------------------------
class TgFail(Exception):
    def __init__(self, desc, code=0):
        super().__init__(desc)
        self.desc = desc or ""
        self.code = code


TG_HTTP = requests.Session()
VX_HTTP = requests.Session()


def _sanitize(obj, drop_style=False, drop_icon=False):
    """remove private keys; optionally drop button colours / premium icons (older apps, non-premium owner)"""
    if isinstance(obj, list):
        return [_sanitize(x, drop_style, drop_icon) for x in obj]
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if k == "_fb" or (k == "style" and drop_style) or (k == "icon_custom_emoji_id" and drop_icon):
                continue
            out[k] = _sanitize(v, drop_style, drop_icon)
        if drop_icon and "icon_custom_emoji_id" in obj and obj.get("_fb"):
            out["text"] = obj["_fb"] + " " + str(obj.get("text", ""))
        return out
    return obj


def _tg_http(method, params, level=0, rl=True):
    p = dict(params)
    if "reply_markup" in p:
        p["reply_markup"] = _sanitize(params["reply_markup"], drop_style=level >= 2, drop_icon=level >= 1)
    timeout = (10, 45) if method == "getUpdates" else (10, 30)
    try:
        r = TG_HTTP.post(f"{TG_API}/{method}", json=p, timeout=timeout)
        d = r.json()
    except Exception as e:
        raise TgFail(f"network: {e}", 0)
    if d.get("ok"):
        return d.get("result")
    code = d.get("error_code", 0)
    desc = d.get("description", "")
    if code == 429 and rl:
        time.sleep(min(int(d.get("parameters", {}).get("retry_after", 2)), 30) + 0.5)
        return _tg_http(method, params, level, False)
    if code == 400 and level < 2 and "reply_markup" in params and \
            any(w in desc.lower() for w in ("button", "style", "emoji", "icon", "premium")):
        return _tg_http(method, params, level + 1, rl)
    raise TgFail(desc, code)


async def tg_call(method, **params):
    return await asyncio.to_thread(_tg_http, method, params)


async def tg_try(method, **params):
    try:
        return await tg_call(method, **params)
    except TgFail as e:
        log.warning("tg %s failed: %s", method, e.desc)
        return None


def Btn(text, cb=None, url=None, copy_text=None, style=None, icon=None, fb=""):
    b = {"text": text}
    if cb is not None:
        b["callback_data"] = cb
    if url:
        b["url"] = url
    if copy_text is not None:
        b["copy_text"] = {"text": str(copy_text)[:256]}
    if style:
        b["style"] = style
    if icon:
        b["icon_custom_emoji_id"] = str(icon)
        b["_fb"] = fb
    return b


BTN_X = Btn("✖ " + bt("CLOSE"), "x", style=HUE_C)
BTN_X2 = Btn("❌ Close", "x", style=HUE_C)          # number message (like the reference picture)
HUES = [HUE_A, HUE_C, HUE_B]                                # colour cycle
BACK_LABEL = "⬅️ " + bt("BACK")


def kbd(btns):
    return {"inline_keyboard": btns}


async def put_msg(chat, text, btns=None, reply_markup=None):
    p = {"chat_id": chat, "text": text[:4090], "parse_mode": "HTML", "disable_web_page_preview": True}
    if btns is not None:
        p["reply_markup"] = kbd(btns)
    elif reply_markup is not None:
        p["reply_markup"] = reply_markup
    return await tg_try("sendMessage", **p)


class Click:
    def __init__(self, cb):
        self.cbid = cb["id"]
        self.who = cb["from"]["id"]
        self.chat = cb["message"]["chat"]["id"]
        self.mid = cb["message"]["message_id"]
        self.name = cb["from"].get("first_name", "User")
        self.done = False

    async def ans(self, text=None, alert=False):
        if self.done:
            return
        self.done = True
        p = {"callback_query_id": self.cbid}
        if text:
            p["text"] = text[:190]
            p["show_alert"] = bool(alert)
        await tg_try("answerCallbackQuery", **p)


async def revise(ev, text, btns):
    p = {"chat_id": ev.chat, "message_id": ev.mid, "text": text[:4090], "parse_mode": "HTML",
         "disable_web_page_preview": True, "reply_markup": kbd(btns)}
    try:
        await tg_call("editMessageText", **p)
    except TgFail as e:
        if "not modified" in e.desc:
            return
        log.info("edit failed (%s) -> sending new message", e.desc)
        await put_msg(ev.chat, text, btns)


def reply_menu(who):
    def kb_(t, s):
        return {"text": t, "style": s}
    btns = [[kb_(BTN_GET, HUE_A)],
            [kb_(BTN_2FA, HUE_B), kb_(BTN_TRF, HUE_B)],
            [kb_(BTN_REF, HUE_B), kb_(BTN_BAL, HUE_B)],
            [kb_(BTN_BOARD, HUE_B)]]
    if is_admin(who):
        btns.append([kb_(BTN_OWN, HUE_C)])
    return {"keyboard": btns, "resize_keyboard": True, "is_persistent": True}


# ----------------------------------------------------------------------------
# VOLTX API
# ----------------------------------------------------------------------------
def _vx_http(method, path, key, body):
    try:
        r = VX_HTTP.request(method, VX_BASE + path, headers={"mauthapi": key},
                                 json=body, timeout=(8, 20))
        return r.json()
    except Exception as e:
        log.warning("voltx %s %s error: %s", method, path, str(e)[:120])
        return None


async def vx_call(method, path, key, body=None):
    return await asyncio.to_thread(_vx_http, method, path, key, body)


def vx_ok(resp):
    return isinstance(resp, dict) and isinstance(resp.get("meta"), dict) and resp["meta"].get("code") == 200


async def vx_getnum(rid):
    for key in list(CFG["vx_keys"]):
        resp = await vx_call("POST", "/getnum", key, {"rid": str(rid)})
        if vx_ok(resp) and isinstance(resp.get("data"), dict):
            d = resp["data"]
            if only_digits(d.get("no_plus_number") or d.get("full_number") or d.get("national_number")):
                return d
    return None


# ----------------------------------------------------------------------------
# TRAFFIC HELPERS
# ----------------------------------------------------------------------------
def sid_matches(sn, h):
    """sn = normalised service name"""
    if not sn:
        return False
    hn = h["nk"]
    if hn and (hn == sn or (len(hn) >= 3 and (hn in sn or sn in hn))):
        return True
    return sn in h["nm"]


OTHER_ID = "other"


def service_of(k):
    if k == OTHER_ID:
        return {"name": "Other", "rate": None, "icon": None}
    return CFG["services"].get(k)


def in_service(k, h):
    if k == OTHER_ID:
        return not any(sid_matches(squash(v["name"]), h) for v in CFG["services"].values())
    sv = CFG["services"].get(k)
    return bool(sv) and sid_matches(squash(sv["name"]), h)


def other_exists():
    cut = clock() - RANGE_SECS
    return any(h["t"] >= cut and in_service(OTHER_ID, h) for h in TRAFFIC)


def countries_for(k):
    if not service_of(k):
        return []
    cut = clock() - RANGE_SECS
    c = Counter()
    for h in TRAFFIC:
        if h["t"] >= cut and in_service(k, h):
            c[h["iso"]] += 1
    return c.most_common()


def ranges_of(k, iso):
    if not service_of(k):
        return []
    cut = clock() - RANGE_SECS
    c = Counter()
    for h in TRAFFIC:
        if h["t"] >= cut and h["iso"] == iso and in_service(k, h):
            c[h["rd"]] += 1
    return [r for r, _ in c.most_common()]


def rate_of(k):
    sv = service_of(k) or {}
    r = sv.get("rate")
    return float(r) if r is not None else float(CFG["otp_reward"])


def traffic_by_sid():
    cut = clock() - VIEW_SECS
    g = {}
    for h in TRAFFIC:
        if h["t"] < cut:
            continue
        e = g.setdefault(h["nk"][:24] or "unknown", {"names": Counter(), "n": 0})
        e["names"][h["sid"]] += 1
        e["n"] += 1
    out = sorted(g.items(), key=lambda kv: -kv[1]["n"])
    return [(k, e["names"].most_common(1)[0][0], e["n"]) for k, e in out]


def sid_ranges(nk):
    cut = clock() - VIEW_SECS
    c = Counter()
    last = {}
    for h in TRAFFIC:
        if h["t"] >= cut and (h["nk"][:24] or "unknown") == nk:
            c[h["rd"]] += 1
            last[h["rd"]] = max(last.get(h["rd"], 0), h["t"])
    return [(r, n, last[r]) for r, n in c.most_common(15)]


def since(t):
    s = int(clock() - t)
    return f"{s}s" if s < 60 else f"{s // 60}m"


# ----------------------------------------------------------------------------
# USER UI BUILDERS
# ----------------------------------------------------------------------------
def service_btn(sv, cb, style):
    name = str(sv["name"]).upper()
    fb = "🌐" if sv["name"] == "Other" else icon_for(sv["name"])
    icon = sv.get("icon")
    if icon:
        return Btn(name, cb, style=style, icon=icon, fb=fb)
    return Btn(f"{fb} {name}", cb, style=style)


def view_services():
    items = list(CFG["services"].items())
    if other_exists():
        items.append((OTHER_ID, {"name": "Other", "icon": None}))
    if not items:
        return "⚠️ " + bd("No service available right now."), [[BTN_X]]
    text = "<blockquote>" + THIN + "\n   ⚡ CHOOSE A SERVICE\n" + THIN + "</blockquote>"
    btns, row = [], []
    for i, (k, v) in enumerate(items):
        row.append(service_btn(v, f"gs:{k}", HUES[i % 3]))
        if len(row) == 2:
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    btns.append([BTN_X])
    return text, btns


def view_countries(k):
    sv = service_of(k)
    back = Btn(BACK_LABEL, "gn", style=HUE_B)
    if not sv:
        return "❌ " + bd("Service not found."), [[back]]
    cc = countries_for(k)
    if not cc:
        return ("😕 " + bd("No traffic for ") + htm(sv["name"].upper()) + bd(" right now.") +
                "\n" + bd("Please try again later.")), [[back]]
    r = rate_of(k)
    btns = []
    for iso, _n in cc[:30]:
        btns.append([Btn(f"{bt(country_name(iso).upper())} {iso} - {n1(r)} {bt('TK')}/OTP", f"gc:{k}:{iso}", style=HUE_A)])
    btns.append([back])
    return "📌 " + bd("Select a country for ") + bd(htm(sv["name"].upper())) + ":", btns


def view_numbers(k, iso, nums):
    sv = service_of(k) or {"name": k}
    text = (f"{flag_of(iso)} {bd(country_name(iso))} | {htm(sv['name'])}\n"
            f"⏳ {bd('Waiting for OTP...')}")
    btns = [[Btn(f"⚡ Active {int(CFG['num_time'])}min ▸ Awaiting OTP signal", "nop", style=HUE_A)],
            [service_btn(sv, "nop", HUE_A)]]
    for i, n in enumerate(nums):
        btns.append([Btn(f"{flag_of(iso)} +{n}", copy_text=f"+{n}", style=(HUE_B if i % 2 == 0 else HUE_C))])
    last = [Btn("🔄 Change Number", f"chg:{k}:{iso}", style=HUE_C)]
    gl = clean_url(CFG["otp_group_link"])
    if gl:
        last.append(Btn("🛡 OTP Group", url=gl, style=HUE_B))
    btns.append(last)
    btns.append([BTN_X2])
    return text, btns


def view_traffic():
    g = traffic_by_sid()
    if not g:
        return LINE + "\n📊 " + bd("LIVE TRAFFIC") + "\n" + LINE + "\n" + bd("No traffic right now."), [[BTN_X]]
    lines = [LINE, "📊 " + bd("LIVE TRAFFIC") + " (15 min)", LINE]
    for i, (k, name, n) in enumerate(g[:10], 1):
        lines.append(f"{i}. {htm(name[:24].upper())} — {n} hits")
    lines.append(LINE)
    btns, row = [], []
    for k, name, n in g[:8]:
        row.append(Btn("🔎 " + name[:16].upper(), f"tx:{k}", style=HUE_B))
        if len(row) == 2:
            btns.append(row)
            row = []
    if row:
        btns.append(row)
    btns.append([BTN_X])
    return "\n".join(lines), btns


def view_explore(nk):
    data = sid_ranges(nk)
    back = Btn(BACK_LABEL, "tr", style=HUE_B)
    if not data:
        return "😕 " + bd("No traffic for this service."), [[back]]
    g = {k: nm for k, nm, _ in traffic_by_sid()}
    title = str(g.get(nk, nk))[:24].upper()
    lines = [LINE, f"🔎 {htm(title)} — " + bd("TOP RANGES"), LINE]
    for r, n, last in data:
        iso = iso_of(r)
        lines.append(f"{flag_of(iso)} {iso} • <code>{r}XXX</code> — {n} hits ({since(last)})")
    return "\n".join(lines), [[back]]


def view_wallet(who):
    u = USERS[who]
    t = (f"{DASH}\n【 💸 {bd('WITHDRAWAL')} 】\n{DASH}\n"
         f"👤 {bd('Name')}: {htm(u['name'])}\n"
         f"👋 {bd('Total Otp')}: {u['total_otp']}\n"
         f"📱 {bd('Total Number')}: {u['total_numbers']}\n"
         f"👥 {bd('Total Reffer')} :{u['ref_count']}\n{DASH}\n"
         f"📅 {bd('BALANCE')}: {n1(u['balance'])}৳\n"
         f"🔐 {bd('MINIMUM')}: {n1(CFG['min_withdraw'])} ৳\n{DASH}\n"
         f"📌 {bd('SELECT METHOD')}:")
    btns = [[Btn(bt(m.upper()), f"wm:{i}", style=HUE_B)] for i, m in enumerate(CFG["w_methods"])]
    btns.append([Btn("✖ " + bt("CANCEL"), "x", style=HUE_C)])
    return t, btns


def view_invite(who):
    u = USERS[who]
    link = f"https://t.me/{ME['username']}?start={who}"
    com = ""
    if float(CFG["ref_commission"]) > 0:
        com = f"🤝 {bd('COMMISSION')}: {n1(CFG['ref_commission'])}%\n{DASH}\n"
    t = (f"{DASH}\n【 🎁 {bd('REFER & EARN')} 】\n{DASH}\n"
         f"🔗 {bd('YOUR LINK')}:\n<code>{link}</code>\n{DASH}\n"
         f"👤 {bd('TOTAL REFERS')}: {u['ref_count']}\n{DASH}\n"
         f"💸 {bd('PER REFER')}: {n1(CFG['ref_reward'])} TK\n{DASH}\n" + com).rstrip("\n")
    return t, [[Btn(bt("COPY LINK"), copy_text=link, style=HUE_A)], [BTN_X]]


BOARD_TZ = 6 * 3600        # board days / weeks follow Bangladesh time


def board_stamp():
    t = time.gmtime(clock() + BOARD_TZ)
    return time.strftime("%Y-%m-%d", t), time.strftime("%G-W%V", t)


def board_add(user):
    dk, wk = board_stamp()
    if user.get("d_key") != dk:
        user["d_key"], user["d_otp"] = dk, 0
    if user.get("w_key") != wk:
        user["w_key"], user["w_otp"] = wk, 0
    user["d_otp"] += 1
    user["w_otp"] += 1


def board_of(u, mode):
    dk, wk = board_stamp()
    if mode == "t":
        return u.get("d_otp", 0) if u.get("d_key") == dk else 0
    if mode == "w":
        return u.get("w_otp", 0) if u.get("w_key") == wk else 0
    return u.get("total_otp", 0)


def view_board(mode, who):
    if mode not in ("t", "w", "a"):
        mode = "t"
    label = {"t": "TODAY", "w": "THIS WEEK", "a": "ALL TIME"}[mode]
    ranked = [(board_of(u, mode), u) for u in USERS.values()]
    ranked = sorted([x for x in ranked if x[0] > 0], key=lambda x: -x[0])
    medals = ["🥇", "🥈", "🥉"]
    lines = [LINE, "【 🏆 " + bd("LEADERBOARD") + " 】", LINE, f"🗓 {bd(label)}", ""]
    if not ranked:
        lines.append(bd("No OTP yet. Be the first!"))
    for i, (n, u) in enumerate(ranked[:10], 1):
        mark = medals[i - 1] if i <= 3 else f"{i}."
        you = "  ◀" if u["id"] == who else ""
        lines.append(f"{mark} {htm(u['name'][:14])} — <b>{n}</b> OTP{you}")
    pos = next((i for i, (n, u) in enumerate(ranked, 1) if u["id"] == who), None)
    me = USERS.get(who)
    mine = f"#{pos} • {board_of(me, mode)} OTP" if (pos and me) else "—"
    lines += ["", LINE, f"🎯 {bd('Your rank')}: {mine}"]
    tabs = []
    for k, nm in (("t", "TODAY"), ("w", "WEEK"), ("a", "ALL")):
        tabs.append(Btn(("✅ " if mode == k else "") + bt(nm), f"lb:{k}", style=(HUE_A if mode == k else HUE_B)))
    return "\n".join(lines), [tabs, [BTN_X]]


def view_totp():
    return (f"{LINE}\n【 🔐 {bd('2FA ONLINE')} 】\n{LINE}\n"
            f"{bdi('Generate your')} 2FA {bdi('security code instantly using your secret key.')}\n{LINE}"), \
        [[Btn(bt("GENERATE") + " 2FA " + bt("CODE"), "tf:g", style=HUE_A)], [BTN_X]]


def view_totp_code(secret):
    code, left = totp_calc(secret)
    t = (f"{LINE}\n【 🔐 {bd('2FA CODE')} 】\n{LINE}\n"
         f"🔢 {bd('Code')}: <code>{code}</code>\n"
         f"⏳ {bd('Expires in')}: {left}s\n{LINE}")
    return code, t, [[Btn(f"🛡 {code}", copy_text=code, style=HUE_A)],
                     [Btn("🔄 " + bt("REFRESH"), "tf:r", style=HUE_B), BTN_X]]


# ----------------------------------------------------------------------------
# OWNER UI BUILDERS
# ----------------------------------------------------------------------------
def sw_text(v):
    return "🟢 ON" if v else "🔴 OFF"


def nav_back(to):
    """nested pages: only a Back button (root pages have the Close button)"""
    return [Btn(BACK_LABEL, to, style=HUE_B)]


def view_admin():
    fb = "🔥 Firebase" if datastore.fs is not None else "💾 Local file"
    t = (f"👑 <b>OWNER PANEL</b>\n{LINE}\n"
         f"👥 Users: {len(USERS)}\n"
         f"🛠 Voltx: {sw_text(CFG['vx_on'])}\n"
         f"🧩 Services: {len(CFG['services'])}\n"
         f"💸 Pending withdraw: {len(WDQ)}\n"
         f"🗄 Database: {fb}")
    btns = [
        [Btn("🛠 VOLTX CONTROL", "pm", style=HUE_A), Btn("🧩 SERVICE CONTROL", "sm", style=HUE_C)],
        [Btn("🚪 FORCE JOIN", "jm", style=HUE_B), Btn("📢 BROADCAST", "bc", style=HUE_A)],
        [Btn("👥 OTP GROUP", "gm", style=HUE_C), Btn("💵 VX CONTROL", "rc", style=HUE_B)],
        [Btn("🔥 FIREBASE", "fb", style=HUE_A), Btn("⚙️ SETTINGS", "se", style=HUE_C)],
        [Btn(f"💸 WITHDRAW REQUESTS ({len(WDQ)})", "wq", style=HUE_B)],
        [BTN_X],
    ]
    return t, btns


def view_vx():
    keys = CFG["vx_keys"]
    t = (f"🛠 <b>VOLTX CONTROL</b>\n{LINE}\n"
         f"🌐 Panel: <b>Voltx</b>\n"
         f"⚡ Status: {sw_text(CFG['vx_on'])}\n"
         f"🔑 API keys: {len(keys)}\n")
    for i, k in enumerate(keys, 1):
        t += f"{i}. …{htm(k[-6:])}\n"
    btns = [
        [Btn("🔴 TURN OFF" if CFG["vx_on"] else "🟢 TURN ON", "pm:t", style=HUE_C if CFG["vx_on"] else HUE_A)],
        [Btn("➕ ADD KEY", "pm:a", style=HUE_A), Btn("🗑 REMOVE KEY", "pm:r", style=HUE_C)],
        [Btn("🧪 TEST CONNECTION", "pm:x", style=HUE_B)],
        nav_back("op"),
    ]
    return t, btns


def view_fj():
    t = f"🚪 <b>FORCE JOIN</b>\n{LINE}\n🔒 Status: {sw_text(CFG['fj_on'])}\n"
    if CFG["fj_channels"]:
        for i, c in enumerate(CFG["fj_channels"], 1):
            t += f"{i}. {htm(c['chat'])}\n"
    else:
        t += "কোনো channel/group নেই।\n"
    btns = [
        [Btn("🔴 TURN OFF" if CFG["fj_on"] else "🟢 TURN ON", "jm:t", style=HUE_C if CFG["fj_on"] else HUE_A)],
        [Btn("➕ ADD CHANNEL", "jm:a", style=HUE_A), Btn("🗑 REMOVE", "jm:r", style=HUE_C)],
        nav_back("op"),
    ]
    return t, btns


def view_svc_admin():
    t = f"🧩 <b>SERVICE CONTROL</b>\n{LINE}\n"
    if CFG["services"]:
        for v in CFG["services"].values():
            r = v.get("rate")
            ic = "🖼" if v.get("icon") else "▫️"
            t += f"{ic} {htm(v['name'])} — {fmt_rate(r) if r is not None else fmt_rate(CFG['otp_reward']) + ' (default)'} TK\n"
    else:
        t += "কোনো service নেই।\n"
    t += "\nService-এর নাম লিখলে বট panel traffic থেকে নিজেই range খুঁজে নেয়।"
    btns = [[Btn("➕ ADD SERVICE", "sm:a", style=HUE_A), Btn("🗑 REMOVE SERVICE", "sm:r", style=HUE_C)],
            [Btn("🖼 SET ICON", "sm:i", style=HUE_B)],
            nav_back("op")]
    return t, btns


def view_groups():
    t = (f"👥 <b>OTP GROUP</b>\n{LINE}\n"
         f"📤 Forward: {sw_text(CFG['fw_on'])}\n"
         f"📡 Live OTP: {sw_text(CFG['live_on'])}\n"
         f"📢 Main Channel: {htm(CFG['main_channel'] or '—')}\n"
         f"👥 OTP Group Link: {htm(CFG['otp_group_link'] or '—')}\n"
         f"💸 Withdraw Channel: {htm(CFG['w_group'] or 'Owner inbox')}\n\n<b>Forward groups:</b>\n")
    if CFG["fw_groups"]:
        for i, g in enumerate(CFG["fw_groups"], 1):
            t += f"{i}. {htm(g)}\n"
    else:
        t += "কোনো group নেই।\n"
    btns = [
        [Btn("📤 FORWARD " + ("OFF" if CFG["fw_on"] else "ON"), "gm:f", style=HUE_C if CFG["fw_on"] else HUE_A),
         Btn("📡 LIVE OTP " + ("OFF" if CFG["live_on"] else "ON"), "gm:l", style=HUE_C if CFG["live_on"] else HUE_A)],
        [Btn("➕ ADD GROUP", "gm:a", style=HUE_A), Btn("🗑 REMOVE GROUP", "gm:r", style=HUE_C)],
        [Btn("📢 MAIN CHANNEL", "gm:m", style=HUE_B), Btn("👥 OTP GROUP LINK", "gm:g", style=HUE_B)],
        [Btn("💸 WITHDRAW CHANNEL", "gm:w", style=HUE_A)],
        nav_back("op"),
    ]
    return t, btns


def view_welcome():
    t = f"👋 <b>WELCOME MESSAGE</b>\n{LINE}\n{htm(CFG['welcome'])}\n\n<i>{{name}} লিখলে user-এর নাম বসবে।</i>"
    return t, [[Btn("✏️ SET WELCOME", "wl:s", style=HUE_A)], nav_back("se")]


def view_numcount():
    btns = [[Btn(("✅ " if CFG["num_count"] == n else "") + str(n), f"nc:{n}", style=HUE_A if CFG["num_count"] == n else HUE_B)
             for n in range(1, 6)], nav_back("rc")]
    return "🔢 <b>একসাথে কয়টা number আসবে?</b>", btns


def view_rates():
    t = (f"💵 <b>VX CONTROL</b>\n{LINE}\n"
         f"💵 OTP Rate: {n1(CFG['otp_reward'])} TK\n"
         f"💸 Withdraw Min: {n1(CFG['min_withdraw'])} TK\n"
         f"🎁 Refer Reward: {n1(CFG['ref_reward'])} TK\n"
         f"🤝 Refer Commission: {n1(CFG['ref_commission'])}%\n"
         f"⏱ Cooldown: {int(CFG['cooldown'])}s\n"
         f"🔢 Numbers at once: {CFG['num_count']}")
    btns = [
        [Btn("💵 OTP RATE", "rc:o", style=HUE_A), Btn("💸 WITHDRAW MIN", "rc:m", style=HUE_C)],
        [Btn("🎁 REFER REWARD", "rc:r", style=HUE_B), Btn("🤝 REFER COMMISSION", "rc:c", style=HUE_A)],
        [Btn("⏱ COOLDOWN", "rc:d", style=HUE_C), Btn("🔢 NUMBER SET", "nc", style=HUE_B)],
        nav_back("op"),
    ]
    return t, btns


def view_settings():
    t = (f"⚙️ <b>SETTINGS</b>\n{LINE}\n"
         f"⏱ Number time: {int(CFG['num_time'])} min\n"
         f"🏦 Methods: {htm(', '.join(CFG['w_methods']) or '—')}\n"
         f"🛠 Maintenance: {sw_text(CFG['maint'])}")
    btns = [
        [Btn("👤 USER CONTROL", "uc", style=HUE_A), Btn("💰 BALANCE CONTROL", "bl", style=HUE_C)],
        [Btn("⏱ NUMBER TIME", "se:t", style=HUE_B), Btn("👋 WELCOME MESSAGE", "wl", style=HUE_A)],
        [Btn("🏦 WITHDRAW METHODS", "se:w", style=HUE_C),
         Btn("🛠 MAINTENANCE " + ("OFF" if CFG["maint"] else "ON"), "se:m", style=HUE_C if CFG["maint"] else HUE_A)],
        [Btn("📈 STATS", "st", style=HUE_B)],
        nav_back("op"),
    ]
    return t, btns


def view_fb():
    c, on = datastore.saved_cred()
    if datastore.fs is not None:
        st = "🟢 CONNECTED (Firestore)"
    elif c:
        st = "🔴 OFF (saved file ready)"
    else:
        st = "⚪ NOT SET (local file)"
    t = (f"🔥 <b>FIREBASE</b>\n{LINE}\n"
         f"Status: {st}\n")
    if datastore.fixed:
        t += "\n🔒 Firebase JSON is written in the code, so it can only be changed there."
    else:
        t += ("\nUPLOAD JSON চাপুন, তারপর Firebase থেকে নামানো <b>.json ফাইলটা</b> এখানে পাঠান। "
              "বট নিজে connect করে পুরোনো ডাটা Firebase-এ তুলে দেবে।")
    btns = []
    if not datastore.fixed:
        btns.append([Btn("⬆️ UPLOAD JSON", "fb:u", style=HUE_A)])
        if c:
            btns.append([Btn("🔴 TURN OFF" if datastore.fs is not None else "🟢 TURN ON", "fb:t",
                           style=HUE_C if datastore.fs is not None else HUE_A)])
    if datastore.fs is not None:
        btns.append([Btn("🧪 TEST", "fb:x", style=HUE_B)])
    btns.append(nav_back("op"))
    return t, btns


def view_member(tid, mode):
    u = USERS.get(tid)
    if not u:
        return "❌ User পাওয়া যায়নি।", [nav_back("se")]
    un = f"@{htm(u['username'])}" if u["username"] else "—"
    t = (f"👤 <b>USER</b>\n{LINE}\n"
         f"🆔 <code>{tid}</code>\n"
         f"📛 {htm(u['name'])} {un}\n"
         f"💵 Balance: {n1(u['balance'])} TK\n"
         f"🔑 OTP: {u['total_otp']} | 📱 Numbers: {u['total_numbers']}\n"
         f"👥 Refers: {u['ref_count']} | 🎁 Earn: {n1(u['ref_earn'])} TK\n"
         f"🚫 Banned: {'YES' if u.get('banned') else 'no'}")
    if mode == "u":
        btns = [[Btn("✅ UNBAN" if u.get("banned") else "🚫 BAN", f"ub:{tid}", style=HUE_A if u.get("banned") else HUE_C)]]
    else:
        btns = [[Btn("➕ ADD BALANCE", f"ba:{tid}", style=HUE_A), Btn("➖ REMOVE BALANCE", f"br:{tid}", style=HUE_C)]]
    btns.append(nav_back("se"))
    return t, btns


def view_stats():
    tb = sum(u["balance"] for u in USERS.values())
    to = sum(u["total_otp"] for u in USERS.values())
    tn = sum(u["total_numbers"] for u in USERS.values())
    t = (f"📈 <b>STATS</b>\n{LINE}\n"
         f"👥 Users: {len(USERS)}\n🔑 Total OTP: {to}\n📱 Total Numbers: {tn}\n"
         f"💵 Total user balance: {fmt_money(tb)} TK\n⏳ Pending withdraw: {len(WDQ)}\n"
         f"📊 Hits in memory: {len(TRAFFIC)}")
    return t, [nav_back("se")]


# ----------------------------------------------------------------------------
# FORCE JOIN
# ----------------------------------------------------------------------------
async def missing_chats(who):
    if not CFG["fj_on"] or is_admin(who) or not CFG["fj_channels"]:
        return []
    if clock() - JOIN_OK.get(who, 0) < 60:
        return []
    out = []
    for ch in CFG["fj_channels"]:
        try:
            r = await tg_call("getChatMember", chat_id=ch["chat"], user_id=who)
            st = r.get("status")
            if st in ("left", "kicked") or (st == "restricted" and not r.get("is_member", True)):
                out.append(ch)
        except TgFail:
            pass   # bot not admin / bad id -> do not lock users
    if not out:
        JOIN_OK[who] = clock()
    return out


async def join_gate(who, chat):
    miss = await missing_chats(who)
    if not miss:
        return True
    btns = []
    for i, ch in enumerate(miss, 1):
        url = clean_url(ch.get("link"))
        if url:
            btns.append([Btn(bt("JOIN CHANNEL"), url=url)])
    btns.append([Btn(bt("CHECK JOINED"), "fj:v", style=HUE_A)])
    await put_msg(chat, "⚠️ " + bd("Please join our channels to use the bot!"), btns)
    return False


def greeting(name):
    return htm(CFG["welcome"]).replace("{name}", htm(name))


# ----------------------------------------------------------------------------
# NUMBER DELIVERY
# ----------------------------------------------------------------------------
async def grab_numbers(ev, k, iso):
    sv = service_of(k)
    if not sv:
        await ev.ans("❌ Service নেই", True)
        return
    if not CFG["vx_on"] or not CFG["vx_keys"]:
        await ev.ans("⚠️ Panel এখন বন্ধ আছে, পরে চেষ্টা করুন।", True)
        return
    if ev.who in WORKING:
        await ev.ans("⏳ একটু অপেক্ষা করুন...")
        return
    wait = float(CFG["cooldown"]) - (clock() - REQ_TIME.get(ev.who, 0))
    if wait > 0 and not is_admin(ev.who):
        await ev.ans(f"⏳ Please wait {int(wait) + 1}s", True)
        return
    REQ_TIME[ev.who] = clock()
    WORKING.add(ev.who)
    try:
        await ev.ans()
        back = Btn(BACK_LABEL, f"gs:{k}", style=HUE_B)
        ranges = ranges_of(k, iso)
        if not ranges:
            await revise(ev, "😕 " + bd("No range available right now.") + "\n" + bd("Please try again."), [[back]])
            return
        await revise(ev, "⏳ " + bd("Fetching number..."), [])
        want = int(CFG["num_count"])
        got, used_range, dead, dup = [], {}, set(), Counter()
        tries = 0
        while len(got) < want and tries < want * 4 + 4:
            live = [r for r in ranges if r not in dead][:6]
            if not live:
                break
            r = live[0]
            tries += 1
            d = await vx_getnum(r)
            if not d:
                dead.add(r)
                continue
            n = only_digits(d.get("no_plus_number") or d.get("full_number") or d.get("national_number"))
            if not n:
                dead.add(r)
                continue
            if n in got:
                dup[r] += 1
                if dup[r] >= 2:
                    dead.add(r)
                continue
            got.append(n)
            used_range[n] = r
        if not got:
            await revise(ev, "❌ " + bd("Number out of stock!") + "\n" + bd("Try another country or try later."), [[back]])
            return
        rate = rate_of(k)
        for n in got:
            rec = {"uid": ev.who, "service": k, "sname": sv["name"], "iso": iso,
                   "rate": rate, "ts": clock(), "range": used_range.get(n, "")}
            HELD[n] = rec
            spawn(datastore.set("vx_assigned", n, copy.deepcopy(rec)))
        USERS[ev.who]["total_numbers"] += len(got)
        persist_user(ev.who)
        text, btns = view_numbers(k, iso, got)
        await revise(ev, text, btns)
    finally:
        WORKING.discard(ev.who)


# ----------------------------------------------------------------------------
# OTP HANDLING
# ----------------------------------------------------------------------------
def owner_of_number(num):
    if num in HELD:
        return num, HELD[num]
    if len(num) >= 9:
        for n, rec in HELD.items():
            if len(n) >= 9 and n[-9:] == num[-9:]:
                return n, rec
    return None, None


def queue_post(chat, text, btns, user=True):
    item = (chat, text, btns)
    if user:
        Q_USER.append(item)
    else:
        Q_LIVE.append(item)


def post_buttons(code=None):
    btns, row = [], []
    mc = clean_url(CFG["main_channel"])
    if mc:
        row.append(Btn("🔔 Channel", url=mc, style=HUE_B))
    if code:
        row.append(Btn(f"🛡 {code}", copy_text=code, style=HUE_A))
    if row:
        btns.append(row)
    if ME["username"]:
        btns.append([Btn("📞 Get Number", url=f"https://t.me/{ME['username']}", style=HUE_B)])
    return btns


def post_head(sname, iso, num):
    d = only_digits(num)
    cc = ISO_DIAL.get(iso) or d[:3]
    tail = d[-3:] if len(d) > 8 else ""
    return f"{icon_for(sname)} | {flag_of(iso)} {iso} {cc}****{tail}"


async def handle_sms(it):
    num = only_digits(it.get("number"))
    msg = str(it.get("message") or "")
    n, rec = owner_of_number(num)
    if not rec:
        return
    who = rec["uid"]
    user = USERS.get(who)
    if not user:
        return
    code = find_code(msg) or "N/A"
    rate = float(rec.get("rate", CFG["otp_reward"]))
    iso = rec.get("iso") or iso_of(n)
    sname = rec.get("sname", "Service")

    user["balance"] = round(user["balance"] + rate, 6)
    user["total_otp"] += 1
    board_add(user)
    ref_note = None
    if user.get("ref_by") and not user.get("ref_paid"):
        user["ref_paid"] = True
        ref = USERS.get(user["ref_by"])
        bonus = float(CFG["ref_reward"])
        if ref and bonus > 0:
            ref["balance"] = round(ref["balance"] + bonus, 6)
            ref["ref_earn"] = round(ref["ref_earn"] + bonus, 6)
            persist_user(ref["id"])
            ref_note = (ref["id"], bonus)
    com = float(CFG["ref_commission"])
    if com > 0 and user.get("ref_by"):
        ref = USERS.get(user["ref_by"])
        if ref:
            amt = round(rate * com / 100.0, 6)
            ref["balance"] = round(ref["balance"] + amt, 6)
            ref["ref_earn"] = round(ref["ref_earn"] + amt, 6)
            persist_user(ref["id"])
    persist_user(who)
    if code != "N/A":
        CODE_BOOK[code] = {"num": n, "t": clock()}

    text = (f"{LINE}\n{icon_for(sname)} {htm(sname)} | {flag_of(iso)} {bd(country_name(iso))}\n{LINE}\n"
            f"📱 <code>+{n}</code>\n🔑 {bd('OTP')}: <code>{htm(code)}</code>\n"
            f"💬 {htm(msg[:150])}\n{LINE}\n💰 +{n1(rate)} TK")
    btns = []
    if code != "N/A":
        btns.append([Btn(f"🛡 {code}", copy_text=code, style=HUE_A)])
    btns.append([BTN_X])
    await put_msg(who, text, btns)
    if ref_note:
        await put_msg(ref_note[0], f"🎁 {bd('Referral bonus!')} +{n1(ref_note[1])} TK", [[BTN_X]])

    if CFG["fw_on"] and CFG["fw_groups"]:
        gtext = f"{post_head(sname, iso, n)} | 🔊 {guess_lang(msg)}\n\n💬 {htm(msg[:200])}"
        for g in CFG["fw_groups"]:
            queue_post(g, gtext, post_buttons(code if code != "N/A" else None), user=True)


async def live_delay(h):
    await asyncio.sleep(15)
    if not (CFG["fw_on"] and CFG["live_on"] and CFG["fw_groups"]):
        return
    code = find_code(h["msg"])
    uc = CODE_BOOK.get(code) if code else None
    if uc and h["rd"] and uc["num"].startswith(h["rd"]):
        return   # already posted as a user OTP
    cc = ISO_DIAL.get(h["iso"]) or h["rd"][:3]
    text = (f"{icon_for(h['sid'])} | {flag_of(h['iso'])} {h['iso']} {cc}**** | 🔊 {guess_lang(h['msg'])}\n\n"
            f"💬 {bd('Live OTP RCV')} • {htm(h['sid'][:24])}")
    for g in CFG["fw_groups"]:
        queue_post(g, text, post_buttons(), user=False)


async def feed_worker():
    while True:
        try:
            item = None
            if Q_USER:
                item = Q_USER.popleft()
            elif Q_LIVE:
                item = Q_LIVE.popleft()
            if not item:
                await asyncio.sleep(0.5)
                continue
            chat, text, btns = item
            wait = SENT_AT.get(str(chat), 0) + 3.2 - clock()
            if wait > 0:
                await asyncio.sleep(wait)
            SENT_AT[str(chat)] = clock()
            await put_msg(chat, text, btns)
        except Exception as e:
            log.warning("group_worker error: %s", e)
            await asyncio.sleep(1)


# ----------------------------------------------------------------------------
# BACKGROUND LOOPS
# ----------------------------------------------------------------------------
async def console_poll():
    primed = False
    while True:
        try:
            if CFG["vx_on"] and CFG["vx_keys"]:
                hits = None
                for key in list(CFG["vx_keys"]):
                    resp = await vx_call("GET", "/console", key)
                    if vx_ok(resp) and isinstance(resp.get("data"), dict):
                        hits = resp["data"].get("hits") or []
                        break
                if hits is not None:
                    t_now = clock()
                    new = []
                    for h in hits:
                        rd = only_digits(str(h.get("range", "")).split("X")[0].split("x")[0])
                        if not rd:
                            continue
                        sid = str(h.get("sid") or "Unknown")
                        msg = str(h.get("message") or "")
                        ts = h.get("time")
                        try:
                            ts = float(ts) / 1000.0
                        except Exception:
                            ts = t_now
                        if ts <= 0 or ts > t_now + 60:
                            ts = t_now
                        hk = f"{rd}|{sid}|{h.get('time')}"
                        if hk in HIT_SEEN:
                            continue
                        HIT_SEEN[hk] = t_now
                        rec = {"rd": rd, "sid": sid, "msg": msg, "t": ts, "iso": iso_of(rd),
                               "nk": squash(sid), "nm": squash(msg)[:300]}
                        TRAFFIC.append(rec)
                        new.append(rec)
                    if primed:
                        for rec in new:
                            if CFG["live_on"] and CFG["fw_on"] and CFG["fw_groups"]:
                                spawn(live_delay(rec))
                    primed = True
        except Exception as e:
            log.warning("console_loop error: %s", e)
        await asyncio.sleep(6)


async def access_poll():
    """Voltx /liveaccess: recently active services and the ranges they hit (cached 60s on the panel)"""
    while True:
        try:
            if CFG["vx_on"] and CFG["vx_keys"]:
                for key in list(CFG["vx_keys"]):
                    resp = await vx_call("GET", "/liveaccess", key)
                    if not (vx_ok(resp) and isinstance(resp.get("data"), dict)):
                        continue
                    t_now = clock()
                    for sv in resp["data"].get("services") or []:
                        sid = str(sv.get("sid") or "Unknown")
                        try:
                            ts = float(sv.get("last_at")) / 1000.0
                        except Exception:
                            ts = t_now
                        if ts <= 0 or ts > t_now + 60:
                            ts = t_now
                        if t_now - ts > KEEP_SECS:
                            continue
                        for r in sv.get("ranges") or []:
                            rd = only_digits(str(r).split("X")[0].split("x")[0])
                            if not rd:
                                continue
                            hk = f"la|{rd}|{sid}|{sv.get('last_at')}"
                            if hk in HIT_SEEN:
                                continue
                            HIT_SEEN[hk] = t_now
                            TRAFFIC.append({"rd": rd, "sid": sid, "msg": "", "t": ts, "iso": iso_of(rd),
                                         "nk": squash(sid), "nm": ""})
                    break
        except Exception as e:
            log.warning("liveaccess_loop error: %s", e)
        await asyncio.sleep(60)


async def sms_poll():
    primed = set()
    while True:
        try:
            if CFG["vx_on"]:
                for key in list(CFG["vx_keys"]):
                    resp = await vx_call("GET", "/success-otp", key)
                    if not (vx_ok(resp) and isinstance(resp.get("data"), dict)):
                        continue
                    items = resp["data"].get("otps") or []
                    first = key not in primed
                    items = sorted(items, key=lambda x: x.get("time") or 0)
                    for it in items:
                        oid = str(it.get("otp_id") or f"{it.get('number')}{it.get('time')}")
                        if oid in SMS_SEEN:
                            continue
                        SMS_SEEN[oid] = clock()
                        if first:
                            continue    # old OTPs at start-up are only marked as seen
                        try:
                            await handle_sms(it)
                        except Exception as e:
                            log.warning("process_otp error: %s", e)
                    primed.add(key)
        except Exception as e:
            log.warning("otp_loop error: %s", e)
        await asyncio.sleep(4)


async def gc_loop():
    while True:
        await asyncio.sleep(300)
        try:
            t = clock()
            for n in [n for n, r in HELD.items() if t - r["ts"] > HOLD_SECS]:
                HELD.pop(n, None)
                spawn(datastore.delete("vx_assigned", n))
            TRAFFIC[:] = [h for h in TRAFFIC if t - h["t"] <= KEEP_SECS]
            for d, ttl in ((HIT_SEEN, 2400), (SMS_SEEN, 6 * 3600)):
                for k in [k for k, v in d.items() if t - v > ttl]:
                    d.pop(k, None)
            for c in [c for c, v in CODE_BOOK.items() if t - v["t"] > 600]:
                CODE_BOOK.pop(c, None)
            for u in [u for u, s in INPUT.items() if t - s.get("ts", t) > 1800]:
                INPUT.pop(u, None)
            if len(TOTP_KEYS) > 500:
                for k in list(TOTP_KEYS)[:250]:
                    TOTP_KEYS.pop(k, None)
        except Exception as e:
            log.warning("cleanup error: %s", e)


async def ping_server():
    port = os.environ.get("PORT")
    if not port:
        return

    async def handler(reader, writer):
        try:
            await asyncio.wait_for(reader.read(1024), 5)
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/plain\r\nContent-Length: 2\r\nConnection: close\r\n\r\nOK")
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()
    await asyncio.start_server(handler, "0.0.0.0", int(port))
    log.info("health server on %s", port)
    while True:
        await asyncio.sleep(3600)


# ----------------------------------------------------------------------------
# USER CREATION / REFERRAL
# ----------------------------------------------------------------------------
def touch_user(frm, text=""):
    who = frm["id"]
    if who in USERS:
        u = USERS[who]
        nm = (frm.get("first_name") or u["name"])[:60]
        un = frm.get("username") or ""
        if nm != u["name"] or un != u["username"]:
            u["name"], u["username"] = nm, un
            persist_user(who)
        return u
    u = make_user(frm)
    parts = text.split(maxsplit=1)
    if parts and parts[0].startswith("/start") and len(parts) > 1 and re.fullmatch(r"(ref)?\d+", parts[1]):
        try:
            rid = int(parts[1].replace("ref", ""))
            if rid != who and rid in USERS:
                u["ref_by"] = rid
                USERS[rid]["ref_count"] += 1
                persist_user(rid)
        except ValueError:
            pass
    USERS[who] = u
    persist_user(who)
    return u


# ----------------------------------------------------------------------------
# MESSAGE HANDLING
# ----------------------------------------------------------------------------
async def notice(chat, text, back_to):
    await put_msg(chat, text, [[BTN_X]])


def read_chat_ref(text):
    t = str(text).strip()
    if re.fullmatch(r"-?\d+", t):
        return int(t)
    if t.startswith("@") and len(t) > 2:
        return t
    m = re.match(r"(?:https?://)?t\.me/([A-Za-z][A-Za-z0-9_]{3,})/?$", t)
    return "@" + m.group(1) if m else None


def fetch_tg_file(path):
    r = TG_HTTP.get(f"https://api.telegram.org/file/bot{BOT_KEY}/{path}", timeout=30)
    r.raise_for_status()
    return r.content


async def probe_chat(ref):
    """(ok, saved_ref, note) - numeric ids (private channels) are always accepted"""
    info = await tg_try("getChat", chat_id=ref)
    if info:
        return True, ref, ""
    if isinstance(ref, int):
        return True, ref, "\n⚠️ বট এখনো verify করতে পারেনি। বটকে ওই chat-এ admin বানাতে ভুলবেন না।"
    return False, ref, ""


RATE_FIELDS = {"rc_o": "otp_reward", "rc_m": "min_withdraw", "rc_r": "ref_reward",
             "rc_c": "ref_commission", "rc_d": "cooldown"}
ADMIN_INPUTS = {"addkey", "addsvc", "addch", "addgrp", "setwel", "se_w", "se_t", "gm_m", "gm_g",
                 "gm_w", "bcast", "uc_find", "bal_add", "bal_sub", "fbup", "svcicon"} | set(RATE_FIELDS)


async def on_input(m, st):
    who = m["from"]["id"]
    chat = m["chat"]["id"]
    text = (m.get("text") or "").strip()
    a = st["a"]
    if a in ADMIN_INPUTS and not is_admin(who):
        await end_input(who)
        return

    # ---------------- 2FA
    if a == "2fa":
        try:
            code, t, btns = view_totp_code(text)
        except ValueError:
            await put_msg(chat, "❌ Key সঠিক নয়। Base32 secret key দিন (যেমন <code>JBSWY3DPEHPK3PXP</code>)।", [[BTN_X]])
            return
        await end_input(who)
        res = await put_msg(chat, t, btns)
        await tg_try("deleteMessage", chat_id=chat, message_id=m["message_id"])
        if res:
            TOTP_KEYS[f"{chat}:{res['message_id']}"] = text
        return

    # ---------------- withdraw
    if a == "wd_acc":
        if len(text) < 3 or len(text) > 80:
            await put_msg(chat, "❌ সঠিক account/number দিন।", [[BTN_X]])
            return
        await end_input(who)
        u = USERS[who]
        res = await put_msg(chat, "💸 " + bd("Enter withdraw amount") +
                         f"\n{bd('Minimum')}: {n1(CFG['min_withdraw'])} TK | {bd('Balance')}: {n1(u['balance'])} TK", [[BTN_X]])
        INPUT[who] = {"a": "wd_amt", "m": st["m"], "acc": text, "ts": clock(),
                   "pm": res["message_id"] if res else 0, "pc": chat}
        return
    if a == "wd_amt":
        amt = read_amount(text)
        u = USERS[who]
        if amt is None or amt <= 0:
            await put_msg(chat, "❌ শুধু সংখ্যা লিখুন।", [[BTN_X]])
            return
        amt = round(amt, 2)
        if amt < CFG["min_withdraw"]:
            await put_msg(chat, f"❌ Minimum withdraw {n1(CFG['min_withdraw'])} TK", [[BTN_X]])
            return
        if amt > u["balance"] + 1e-9:
            await put_msg(chat, f"❌ আপনার balance {n1(u['balance'])} TK", [[BTN_X]])
            return
        await end_input(who)
        WD_DRAFT[who] = {"m": st["m"], "acc": st["acc"], "amt": amt, "ts": clock()}
        t = (f"💸 {bd('CONFIRM WITHDRAW')}\n{DASH}\n"
             f"🏦 {bd('Method')}: {htm(st['m'])}\n"
             f"📱 {bd('Account')}: <code>{htm(st['acc'])}</code>\n"
             f"💵 {bd('Amount')}: {n1(amt)} TK\n"
             f"💰 {bd('Balance after')}: {n1(u['balance'] - amt)} TK\n{DASH}")
        await put_msg(chat, t, [[Btn("✅ " + bt("CONFIRM"), "wok", style=HUE_A), Btn("✖ " + bt("CANCEL"), "x", style=HUE_C)]])
        return

    # ---------------- VOLTX CONTROL
    if a == "addkey":
        if len(text) < 6 or " " in text:
            await put_msg(chat, "❌ API key সঠিক নয়। আবার পাঠান।", [[BTN_X]])
            return
        await end_input(who)
        if text not in CFG["vx_keys"]:
            CFG["vx_keys"].append(text)
            persist_cfg()
        await tg_try("deleteMessage", chat_id=chat, message_id=m["message_id"])
        await notice(chat, "✅ API key যোগ হয়েছে।", "pm")
        return

    # ---------------- SERVICE CONTROL
    if a == "addsvc":
        await end_input(who)
        added = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            mt = re.match(r"^(.*\S)\s+(\d+(?:\.\d+)?)$", line)
            name, rate = (mt.group(1), float(mt.group(2))) if mt else (line, None)
            if len(name) > 30 or squash(name) == "other":
                continue
            old = CFG["services"].get(svc_key(name)) or {}
            CFG["services"][svc_key(name)] = {"name": name, "rate": rate, "icon": old.get("icon")}
            added.append(name)
        persist_cfg()
        await notice(chat, ("✅ যোগ/আপডেট হয়েছে: " + htm(", ".join(added))) if added else "❌ কিছু যোগ হয়নি।", "sm")
        return
    if a == "svcicon":
        icon = None
        if text != "0":
            for e in (m.get("entities") or []):
                if e.get("type") == "custom_emoji":
                    icon = e.get("custom_emoji_id")
                    break
            if not icon:
                await put_msg(chat, "❌ একটা <b>Premium emoji</b> সহ মেসেজ পাঠান (মুছতে <code>0</code> লিখুন)।", [[BTN_X]])
                return
        sv = CFG["services"].get(st.get("k"))
        await end_input(who)
        if sv:
            sv["icon"] = icon
            persist_cfg()
        await tg_try("deleteMessage", chat_id=chat, message_id=m["message_id"])
        await notice(chat, "✅ Icon সেভ হয়েছে।\n(Premium icon দেখাতে bot owner-এর Telegram Premium লাগে, না থাকলে সাধারণ emoji দেখাবে।)"
                       if icon else "✅ Icon মুছে ফেলা হয়েছে।", "sm")
        return

    # ---------------- FORCE JOIN
    if a == "addch":
        parts = text.split()
        ref = read_chat_ref(parts[0]) if parts else None
        if ref is None:
            await put_msg(chat, "❌ <code>@username</code> বা <code>-100…</code> id দিন।", [[BTN_X]])
            return
        link = clean_url(parts[1]) if len(parts) > 1 else (clean_url(ref) if isinstance(ref, str) else "")
        info = await tg_try("getChat", chat_id=ref)
        if not info and not isinstance(ref, int):
            await put_msg(chat, "❌ Chat পাওয়া যায়নি। বটকে ওই channel/group-এ admin বানিয়ে আবার দিন।", [[BTN_X]])
            return
        if not link and info and info.get("username"):
            link = "https://t.me/" + info["username"]
        if not link:
            await put_msg(chat, "❌ Private chat-এর জন্য invite link লাগবে। এভাবে দিন:\n<code>-100123456789 https://t.me/+xxxx</code>", [[BTN_X]])
            return
        await end_input(who)
        CFG["fj_channels"].append({"chat": ref, "link": link})
        persist_cfg()
        await notice(chat, "✅ যোগ হয়েছে।" + ("" if info else "\n⚠️ verify করা যায়নি, বটকে admin বানান।"), "jm")
        return

    # ---------------- OTP GROUP
    if a == "addgrp":
        ref = read_chat_ref(text)
        if ref is None:
            await put_msg(chat, "❌ <code>-100…</code> id বা @username দিন।", [[BTN_X]])
            return
        ok, ref, note = await probe_chat(ref)
        if not ok:
            await put_msg(chat, "❌ Group পাওয়া যায়নি। বটকে group-এ admin বানিয়ে আবার দিন।", [[BTN_X]])
            return
        await end_input(who)
        if ref not in CFG["fw_groups"]:
            CFG["fw_groups"].append(ref)
            persist_cfg()
        await notice(chat, "✅ OTP group যোগ হয়েছে।" + note, "gm")
        return
    if a == "gm_w":
        if text == "0":
            await end_input(who)
            CFG["w_group"] = ""
            persist_cfg()
            await notice(chat, "✅ Withdraw channel মুছে ফেলা হয়েছে (request owner inbox-এ যাবে)।", "gm")
            return
        ref = read_chat_ref(text)
        if ref is None:
            await put_msg(chat, "❌ <code>-100…</code> id বা @username দিন। মুছতে <code>0</code> লিখুন।", [[BTN_X]])
            return
        ok, ref, note = await probe_chat(ref)
        if not ok:
            await put_msg(chat, "❌ Chat পাওয়া যায়নি। বটকে admin বানিয়ে আবার দিন।", [[BTN_X]])
            return
        await end_input(who)
        CFG["w_group"] = ref
        persist_cfg()
        await notice(chat, "✅ Withdraw channel সেট হয়েছে।" + note, "gm")
        return
    if a in ("gm_m", "gm_g"):
        v = "" if text == "0" else text
        if v and not clean_url(v):
            await put_msg(chat, "❌ সঠিক link দিন (https://t.me/... বা @username)", [[BTN_X]])
            return
        await end_input(who)
        CFG["main_channel" if a == "gm_m" else "otp_group_link"] = v
        persist_cfg()
        await notice(chat, "✅ সেভ হয়েছে।", "gm")
        return

    # ---------------- VX CONTROL
    if a in RATE_FIELDS:
        v = read_amount(text)
        if v is None:
            await put_msg(chat, "❌ শুধু সংখ্যা লিখুন।", [[BTN_X]])
            return
        if a == "rc_c" and v > 100:
            await put_msg(chat, "❌ Commission সর্বোচ্চ 100%।", [[BTN_X]])
            return
        if a == "rc_d":
            v = int(min(v, 3600))
        await end_input(who)
        CFG[RATE_FIELDS[a]] = v
        persist_cfg()
        t, btns = view_rates()
        await put_msg(chat, "✅ " + bd("Saved") + "\n\n" + t, btns)
        return

    # ---------------- SETTINGS
    if a == "setwel":
        if not text:
            return
        await end_input(who)
        CFG["welcome"] = text[:1500]
        persist_cfg()
        await notice(chat, "✅ Welcome message সেট হয়েছে।", "wl")
        return
    if a == "se_w":
        ms = [x.strip() for x in re.split(r"[,\n]", text) if x.strip()]
        if not ms:
            await put_msg(chat, "❌ কমা দিয়ে method লিখুন। যেমন: bKash, Nagad", [[BTN_X]])
            return
        await end_input(who)
        CFG["w_methods"] = ms[:10]
        persist_cfg()
        await notice(chat, "✅ সেভ হয়েছে।", "se")
        return
    if a == "se_t":
        v = read_amount(text)
        if v is None or not (1 <= v <= 120):
            await put_msg(chat, "❌ ১ থেকে ১২০ এর মধ্যে মিনিট লিখুন।", [[BTN_X]])
            return
        await end_input(who)
        CFG["num_time"] = int(v)
        persist_cfg()
        await notice(chat, f"✅ Number time {int(v)} min সেট হয়েছে।", "se")
        return
    if a == "uc_find":
        q = text.lstrip("@").lower()
        tid = int(q) if q.isdigit() else next((k for k, u in USERS.items() if u["username"].lower() == q and q), None)
        if tid is None or tid not in USERS:
            await put_msg(chat, "❌ User পাওয়া যায়নি। User ID বা @username দিন।", [[BTN_X]])
            return
        await end_input(who)
        t, btns = view_member(tid, st.get("mode", "u"))
        await put_msg(chat, t, btns)
        return
    if a in ("bal_add", "bal_sub"):
        v = read_amount(text)
        tid = st.get("tid")
        if v is None or v <= 0 or tid not in USERS:
            await put_msg(chat, "❌ শুধু সংখ্যা লিখুন।", [[BTN_X]])
            return
        u = USERS[tid]
        if a == "bal_sub" and v > u["balance"] + 1e-9:
            await put_msg(chat, f"❌ User-এর balance মাত্র {n1(u['balance'])} TK।", [[BTN_X]])
            return
        await end_input(who)
        u["balance"] = round(u["balance"] + (v if a == "bal_add" else -v), 6)
        persist_user(tid)
        await put_msg(tid, f"💰 {bd('Balance updated')}: {'+' if a == 'bal_add' else '-'}{n1(v)} TK\n{bd('Balance')}: {n1(u['balance'])} TK", [[BTN_X]])
        t, btns = view_member(tid, "b")
        await put_msg(chat, "✅ " + bd("Done") + "\n\n" + t, btns)
        return

    # ---------------- FIREBASE upload
    if a == "fbup":
        raw = None
        doc = m.get("document")
        try:
            if doc:
                if doc.get("file_size", 0) > 200000:
                    await put_msg(chat, "❌ ফাইলটা অনেক বড়। Firebase service-account .json ফাইল পাঠান।", [[BTN_X]])
                    return
                info = await tg_call("getFile", file_id=doc["file_id"])
                raw = await asyncio.to_thread(fetch_tg_file, info["file_path"])
            elif text.startswith("{"):
                raw = text.encode("utf-8")
        except Exception as e:
            log.warning("firebase file download failed: %s", e)
        if not raw:
            await put_msg(chat, "❌ .json ফাইলটা document হিসেবে পাঠান।", [[BTN_X]])
            return
        await tg_try("deleteMessage", chat_id=chat, message_id=m["message_id"])     # it holds a private key
        try:
            cred = json.loads(raw.decode("utf-8"))
            assert cred.get("type") == "service_account" and cred.get("private_key") \
                and cred.get("client_email") and cred.get("project_id")
        except Exception:
            await put_msg(chat, "❌ এটা সঠিক Firebase service-account JSON না। Firebase থেকে নামানো আসল ফাইলটা দিন।", [[BTN_X]])
            return
        await end_input(who)
        wait = await put_msg(chat, "⏳ " + bd("Connecting to Firebase..."))
        try:
            n = await datastore.connect_firebase(cred)
            msg = (f"✅ {bd('Firebase connected!')}\n📁 Project: <code>{htm(cred['project_id'])}</code>\n"
                   f"🔄 {n} টা ডাটা Firebase-এ তোলা হয়েছে।\n\n"
                   "ℹ️ Credentials বটের ফোল্ডারের <code>voltx_config.json</code> ফাইলে সেভ আছে। "
                   "Hosting-এ restart দিলে ফাইল মুছে গেলে JSON টা কোডের <code>FIREBASE_JSON</code>-এ বসিয়ে দিন।")
        except Exception as e:
            log.warning("firebase connect failed: %s", e)
            msg = f"❌ Firebase connect হয়নি।\n<code>{htm(str(e)[:200])}</code>\nFirestore Database বানানো আছে কিনা দেখুন।"
        if wait:
            await tg_try("deleteMessage", chat_id=chat, message_id=wait["message_id"])
        await put_msg(chat, msg, [[BTN_X]])
        return

    # ---------------- BROADCAST
    if a == "bcast":
        await end_input(who)
        spawn(mass_send(chat, m["message_id"], who))
        await put_msg(chat, "📢 Broadcast শুরু হয়েছে...", [[BTN_X]])
        return


async def mass_send(from_chat, mid, owner):
    ok = fail = 0
    for u in list(USERS):
        r = await tg_try("copyMessage", chat_id=u, from_chat_id=from_chat, message_id=mid)
        if r:
            ok += 1
        else:
            fail += 1
        await asyncio.sleep(0.06)
    await put_msg(owner, f"📢 Broadcast শেষ।\n✅ Sent: {ok}\n❌ Failed: {fail}", [[BTN_X]])


async def on_msg(m):
    if m["chat"]["type"] != "private" or "from" not in m or m["from"].get("is_bot"):
        return
    who = m["from"]["id"]
    chat = m["chat"]["id"]
    text = (m.get("text") or "").strip()
    u = touch_user(m["from"], text)
    if not is_admin(who):
        if u.get("banned"):
            await put_msg(chat, "🚫 " + bd("You are banned from using this bot."))
            return
        if CFG["maint"]:
            await put_msg(chat, "🛠 " + bd("Bot is under maintenance. Please try again later."))
            return

    if text.startswith("/start") or text in ("/menu", "/cancel"):
        await end_input(who)
        if not await join_gate(who, chat):
            return
        await put_msg(chat, greeting(m["from"].get("first_name", "User")), reply_markup=reply_menu(who))
        return

    if text == "/owner" and is_admin(who):
        text = BTN_OWN

    if text in MENU_LABELS:
        INPUT.pop(who, None)
        if not await join_gate(who, chat):
            return
        if text == BTN_GET:
            t, btns = view_services()
        elif text == BTN_2FA:
            t, btns = view_totp()
        elif text == BTN_TRF:
            t, btns = view_traffic()
        elif text == BTN_REF:
            t, btns = view_invite(who)
        elif text == BTN_BAL:
            t, btns = view_wallet(who)
        elif text == BTN_BOARD:
            t, btns = view_board("t", who)
        elif text == BTN_OWN and is_admin(who):
            t, btns = view_admin()
        else:
            return
        await show_page(who, chat, t, btns)
        return

    st = INPUT.get(who)
    if st:
        if not await join_gate(who, chat):
            return
        await on_input(m, st)


# ----------------------------------------------------------------------------
# CALLBACK HANDLING
# ----------------------------------------------------------------------------
def ask_input(ev, action, **kw):
    kw.update({"a": action, "ts": clock(), "pm": ev.mid, "pc": ev.chat})
    INPUT[ev.who] = kw


async def end_input(who):
    """end an input step and delete its prompt message (Telegram delete animation)"""
    st = INPUT.pop(who, None)
    if st and st.get("pm"):
        await tg_try("deleteMessage", chat_id=st["pc"], message_id=st["pm"])


PAGE_OF = {}


async def show_page(who, chat, text, btns):
    """send a menu page; the previous page of this user is deleted first"""
    old = PAGE_OF.pop(who, None)
    if old:
        await tg_try("deleteMessage", chat_id=old[0], message_id=old[1])
    res = await put_msg(chat, text, btns)
    if res:
        PAGE_OF[who] = (chat, res["message_id"])


async def on_btn(cb):
    if "message" not in cb or "data" not in cb:
        return
    ev = Click(cb)
    who, data = ev.who, cb["data"]
    u0 = touch_user(cb["from"])
    if not is_admin(who):
        if u0.get("banned"):
            await ev.ans("🚫 You are banned.", True)
            return
        if CFG["maint"]:
            await ev.ans("🛠 Bot is under maintenance.", True)
            return
    try:
        if data == "x":
            INPUT.pop(who, None)
            await ev.ans()
            await tg_try("deleteMessage", chat_id=ev.chat, message_id=ev.mid)
            return
        if data == "nop":
            await ev.ans("⏳ OTP আসার অপেক্ষা করা হচ্ছে...")
            return
        if data == "fj:v":
            JOIN_OK.pop(who, None)
            miss = await missing_chats(who)
            if miss:
                await ev.ans("❌ আপনি এখনো সব channel-এ join করেননি।", True)
                return
            await ev.ans("✅ Verified")
            await tg_try("deleteMessage", chat_id=ev.chat, message_id=ev.mid)
            await put_msg(ev.chat, greeting(ev.name), reply_markup=reply_menu(who))
            return
        if not await gate_btn(ev):
            return
        INPUT.pop(who, None)
        await dispatch(ev, data)
    except Exception as e:
        log.exception("callback error: %s", e)
    finally:
        await ev.ans()


async def gate_btn(ev):
    miss = await missing_chats(ev.who)
    if not miss:
        return True
    await ev.ans("🔒 আগে channel-এ join করুন, তারপর /start দিন।", True)
    return False


async def dispatch(ev, data):
    who = ev.who
    p = data.split(":")
    cmd = p[0]

    # ---------------- user side
    if data == "gn":
        t, btns = view_services()
        await revise(ev, t, btns)
    elif cmd == "gs" and len(p) == 2:
        t, btns = view_countries(p[1])
        await revise(ev, t, btns)
    elif cmd in ("gc", "chg") and len(p) == 3:
        await grab_numbers(ev, p[1], p[2])
    elif cmd == "lb" and len(p) == 2:
        t, btns = view_board(p[1], who)
        await revise(ev, t, btns)
    elif data == "tr":
        t, btns = view_traffic()
        await revise(ev, t, btns)
    elif cmd == "tx" and len(p) == 2:
        t, btns = view_explore(p[1])
        await revise(ev, t, btns)
    elif data == "tf":
        t, btns = view_totp()
        await revise(ev, t, btns)
    elif data == "tf:g":
        ask_input(ev, "2fa")
        await revise(ev, "🔑 আপনার <b>2FA secret key</b> পাঠান।\n(key দেওয়ার পর বট নিজেই আপনার মেসেজ মুছে দেবে)", [nav_back("tf")])
    elif data == "tf:r":
        secret = TOTP_KEYS.get(f"{ev.chat}:{ev.mid}")
        if not secret:
            await ev.ans("Key পাওয়া যায়নি, আবার 2FA Online থেকে key দিন।", True)
            return
        code, t, btns = view_totp_code(secret)
        await revise(ev, t, btns)
    elif data in ("bal", "wd"):
        t, btns = view_wallet(who)
        await revise(ev, t, btns)
    elif cmd == "wm" and len(p) == 2:
        u = USERS[who]
        try:
            mth = CFG["w_methods"][int(p[1])]
        except (ValueError, IndexError):
            await ev.ans("Method পাওয়া যায়নি", True)
            return
        if open_requests(who):
            await ev.ans("⏳ আপনার আগের withdraw request এখনো pending আছে।", True)
            return
        if u["balance"] < CFG["min_withdraw"]:
            await ev.ans(f"❌ Minimum withdraw {n1(CFG['min_withdraw'])} TK। আপনার balance {n1(u['balance'])} TK", True)
            return
        ask_input(ev, "wd_acc", m=mth)
        await revise(ev, f"🏦 {bd(mth.upper())}\n{bd('Send your')} {htm(mth)} {bd('number/account:')}", nav_back("bal"))

    # ---------------- withdraw confirm (user)
    elif data == "wok":
        pd = WD_DRAFT.pop(who, None)
        u = USERS[who]
        if not pd or clock() - pd["ts"] > 600:
            await ev.ans("⌛ সময় শেষ, আবার Balance থেকে শুরু করুন।", True)
            await tg_try("deleteMessage", chat_id=ev.chat, message_id=ev.mid)
            return
        if open_requests(who):
            await ev.ans("⏳ আপনার আগের request এখনো pending আছে।", True)
            return
        if pd["amt"] < CFG["min_withdraw"] or pd["amt"] > u["balance"] + 1e-9:
            await ev.ans("❌ Balance বা minimum মিলছে না, আবার চেষ্টা করুন।", True)
            await tg_try("deleteMessage", chat_id=ev.chat, message_id=ev.mid)
            return
        u["balance"] = round(u["balance"] - pd["amt"], 6)
        persist_user(who)
        wid = make_wid(who)
        rec = {"id": wid, "uid": who, "name": u["name"], "method": pd["m"], "acc": pd["acc"],
               "amount": pd["amt"], "status": "pending", "ts": int(clock())}
        WDQ[wid] = rec
        spawn(datastore.set("vx_withdraws", wid, copy.deepcopy(rec)))
        await revise(ev, f"✅ {bd('Withdraw request sent!')}\n🆔 <code>#{wid[-6:]}</code>\n💵 {n1(pd['amt'])} TK via {htm(pd['m'])}\n"
                        f"⏳ {bd('Please wait for approval.')}", [[BTN_X]])
        await announce_wd(rec)

    # ---------------- withdraw approval (owner)
    elif cmd in ("wa", "wr") and len(p) == 2:
        if not is_admin(who):
            await ev.ans("❌ শুধু owner approve/reject করতে পারবে।", True)
            return
        rec = await settle_withdraw(p[1], cmd == "wa")
        if not rec:
            await ev.ans("এই request আগেই প্রসেস হয়েছে।", True)
            return
        tag = "✅ Approved" if cmd == "wa" else "❌ Rejected"
        await revise(ev, wd_card(rec) + f"\n\n<b>{tag}</b>", [[BTN_X]])

    # ---------------- owner side
    elif is_admin(who):
        await dispatch_admin(ev, data, p)
    else:
        await ev.ans("❌ Not allowed", True)


WD_DRAFT = {}      # uid -> withdraw waiting for the Confirm button


def open_requests(who):
    return [r for r in WDQ.values() if r["uid"] == who]


def make_wid(who):
    base = int(clock())
    while f"{who}{base}" in WDQ:
        base += 1
    return f"{who}{base}"


def wd_card(rec):
    return (f"💸 <b>Withdraw</b> <code>#{rec['id'][-6:]}</code>\n👤 {htm(rec['name'])} (<code>{rec['uid']}</code>)\n"
            f"🏦 {htm(rec['method'])}: <code>{htm(rec['acc'])}</code>\n💵 {n1(rec['amount'])} TK")


async def announce_wd(rec):
    """send the request to the withdraw channel; if that fails, to every owner"""
    note = wd_card(rec)
    btns = [[Btn("✅ Approve", f"wa:{rec['id']}", style=HUE_A), Btn("❌ Reject", f"wr:{rec['id']}", style=HUE_C)]]
    sent = False
    if CFG["w_group"]:
        sent = bool(await put_msg(CFG["w_group"], note, btns))
    if not sent:
        extra = "\n\n⚠️ Withdraw channel-এ পাঠানো যায়নি (বট admin কিনা দেখুন)।" if CFG["w_group"] else ""
        for o in sorted(ADMINS):
            await put_msg(o, note + extra, btns)


async def settle_withdraw(wid, approve):
    rec = WDQ.pop(wid, None)          # popped first, so a double click can never pay twice
    if not rec or rec.get("status") != "pending":
        return None
    if approve:
        rec["status"] = "approved"
        await put_msg(rec["uid"], f"✅ {bd('WITHDRAW APPROVED')}\n💵 {n1(rec['amount'])} TK • {htm(rec['method'])}", [[BTN_X]])
    else:
        rec["status"] = "rejected"
        u = USERS.get(rec["uid"])
        if u:
            u["balance"] = round(u["balance"] + rec["amount"], 6)
            persist_user(u["id"])
        await put_msg(rec["uid"], f"❌ {bd('WITHDRAW REJECTED')}\n💵 {n1(rec['amount'])} TK " + "টাকা balance-এ ফেরত দেওয়া হয়েছে।", [[BTN_X]])
    spawn(datastore.set("vx_withdraws", rec["id"], copy.deepcopy(rec)))
    return rec


def view_wd_queue():
    items = sorted(WDQ.values(), key=lambda r: r["ts"])
    t = f"💸 <b>WITHDRAW REQUESTS</b>\n{LINE}\n"
    btns = []
    if not items:
        t += "কোনো pending request নেই।"
    for i, r in enumerate(items[:8], 1):
        t += (f"{i}. {htm(r['name'])} (<code>{r['uid']}</code>) — <b>{n1(r['amount'])} TK</b>\n"
              f"    🏦 {htm(r['method'])}: <code>{htm(r['acc'])}</code>\n")
        btns.append([Btn(f"✅ #{i} Approve", f"qa:{r['id']}", style=HUE_A), Btn(f"❌ #{i} Reject", f"qr:{r['id']}", style=HUE_C)])
    if len(items) > 8:
        t += f"\n… আরও {len(items) - 8} টা আছে"
    btns.append(nav_back("op"))
    return t, btns


async def dispatch_admin(ev, data, p):
    who = ev.who
    cmd = p[0]
    sub = p[1] if len(p) > 1 else ""

    if data == "op":
        t, btns = view_admin()
        await revise(ev, t, btns)

    # ---------------- VOLTX CONTROL
    elif cmd == "pm":
        if sub == "t":
            CFG["vx_on"] = not CFG["vx_on"]
            persist_cfg()
        elif sub == "a":
            ask_input(ev, "addkey")
            await revise(ev, "🔑 Voltx <b>API key</b> পাঠান:", [nav_back("pm")])
            return
        elif sub == "r":
            btns = [[Btn(f"🗑 Key {i + 1}: …{k[-4:]}", f"pm:d:{i}", style=HUE_C)] for i, k in enumerate(CFG["vx_keys"])]
            btns.append(nav_back("pm"))
            await revise(ev, "কোন key মুছবেন?" if CFG["vx_keys"] else "কোনো key নেই।", btns)
            return
        elif sub == "d" and len(p) == 3:
            try:
                CFG["vx_keys"].pop(int(p[2]))
                persist_cfg()
            except (ValueError, IndexError):
                pass
        elif sub == "x":
            await ev.ans("⏳ Testing...")
            lines = []
            for i, k in enumerate(CFG["vx_keys"], 1):
                resp = await vx_call("GET", "/console", k)
                if vx_ok(resp):
                    n = len((resp.get("data") or {}).get("hits") or [])
                    lines.append(f"✅ Key {i}: OK ({n} hits)")
                else:
                    msg = ""
                    if isinstance(resp, dict):
                        msg = str((resp.get("meta") or {}).get("status") or resp.get("message") or "")
                    lines.append(f"❌ Key {i}: failed {htm(msg[:60])}")
            if not lines:
                lines = ["কোনো key নেই।"]
            await revise(ev, "🧪 <b>Test Result</b>\n" + "\n".join(lines), [nav_back("pm")])
            return
        t, btns = view_vx()
        await revise(ev, t, btns)

    # ---------------- SERVICE CONTROL
    elif cmd == "sm":
        if sub == "a":
            ask_input(ev, "addsvc")
            await revise(ev, ("➕ Service-এর নাম লিখুন (এক লাইনে একটা)।\n"
                             "Rate দিতে চাইলে নামের পরে লিখুন, যেমন:\n<code>WhatsApp 0.15\nFacebook\nTelegram 0.2</code>\n"
                             "আগের service-এর rate বদলাতে একই নামে আবার লিখুন।"), [nav_back("sm")])
            return
        elif sub == "r":
            btns = [[Btn(f"🗑 {v['name'].upper()}", f"sm:d:{k}", style=HUE_C)] for k, v in CFG["services"].items()]
            btns.append(nav_back("sm"))
            await revise(ev, "কোনটা মুছবেন?" if CFG["services"] else "কোনো service নেই।", btns)
            return
        elif sub == "d" and len(p) == 3:
            CFG["services"].pop(p[2], None)
            persist_cfg()
        elif sub == "i":
            btns = [[Btn(f"🖼 {v['name'].upper()}", f"sm:ic:{k}", style=HUES[i % 3])]
                    for i, (k, v) in enumerate(CFG["services"].items())]
            btns.append(nav_back("sm"))
            await revise(ev, "কোন service-এর icon বদলাবেন?" if CFG["services"] else "কোনো service নেই।", btns)
            return
        elif sub == "ic" and len(p) == 3:
            ask_input(ev, "svcicon", k=p[2])
            await revise(ev, ("🖼 ওই service-এর জন্য একটা <b>Premium emoji</b> সহ মেসেজ পাঠান (শুধু ১টা emoji)।\n"
                             "Icon মুছতে <code>0</code> লিখুন।"), [nav_back("sm")])
            return
        t, btns = view_svc_admin()
        await revise(ev, t, btns)

    # ---------------- FORCE JOIN
    elif cmd == "jm":
        if sub == "t":
            CFG["fj_on"] = not CFG["fj_on"]
            persist_cfg()
        elif sub == "a":
            ask_input(ev, "addch")
            await revise(ev, ("➕ Channel/Group দিন:\n• Public: <code>@username</code>\n"
                             "• Private: <code>-100123456789 https://t.me/+invite</code>\n\n"
                             "⚠️ বটকে ওই channel/group-এ <b>admin</b> বানাতে হবে।"), [nav_back("jm")])
            return
        elif sub == "r":
            btns = [[Btn(f"🗑 {c['chat']}", f"jm:d:{i}", style=HUE_C)] for i, c in enumerate(CFG["fj_channels"])]
            btns.append(nav_back("jm"))
            await revise(ev, "কোনটা মুছবেন?" if CFG["fj_channels"] else "কিছু নেই।", btns)
            return
        elif sub == "d" and len(p) == 3:
            try:
                CFG["fj_channels"].pop(int(p[2]))
                persist_cfg()
            except (ValueError, IndexError):
                pass
        t, btns = view_fj()
        await revise(ev, t, btns)

    # ---------------- BROADCAST
    elif data == "bc":
        ask_input(ev, "bcast")
        await revise(ev, "📢 যে মেসেজটা সবাইকে পাঠাতে চান সেটা এখন পাঠান (text/photo যেকোনো)।", [nav_back("op")])

    # ---------------- OTP GROUP
    elif cmd == "gm":
        if sub == "f":
            CFG["fw_on"] = not CFG["fw_on"]
            persist_cfg()
        elif sub == "l":
            CFG["live_on"] = not CFG["live_on"]
            persist_cfg()
        elif sub == "a":
            ask_input(ev, "addgrp")
            await revise(ev, ("➕ OTP group-এর <code>chat id</code> (যেমন -1001234567890) বা @username দিন।\n"
                             "Private group হলে id দিন। ⚠️ বটকে group-এ admin বানান।"), [nav_back("gm")])
            return
        elif sub == "r":
            btns = [[Btn(f"🗑 {g}", f"gm:d:{i}", style=HUE_C)] for i, g in enumerate(CFG["fw_groups"])]
            btns.append(nav_back("gm"))
            await revise(ev, "কোনটা মুছবেন?" if CFG["fw_groups"] else "কোনো group নেই।", btns)
            return
        elif sub == "d" and len(p) == 3:
            try:
                CFG["fw_groups"].pop(int(p[2]))
                persist_cfg()
            except (ValueError, IndexError):
                pass
        elif sub in ("m", "g"):
            ask_input(ev, "gm_" + sub)
            what = "Main Channel" if sub == "m" else "OTP Group"
            await revise(ev, f"🔗 {what}-এর link দিন (https://t.me/... বা @username)।\nমুছতে <code>0</code> লিখুন।", [nav_back("gm")])
            return
        elif sub == "w":
            ask_input(ev, "gm_w")
            await revise(ev, ("💸 Withdraw request যাবে এমন channel/group-এর <code>chat id</code> বা @username দিন।\n"
                             "Private হলে id (যেমন -1001234567890) দিন। ⚠️ বটকে admin বানান।\n"
                             "মুছতে <code>0</code> লিখুন।"), [nav_back("gm")])
            return
        t, btns = view_groups()
        await revise(ev, t, btns)

    # ---------------- VX CONTROL
    elif cmd == "rc":
        prompts = {
            "o": ("rc_o", "💵 Default OTP rate (TK) লিখুন, যেমন <code>0.1</code>"),
            "m": ("rc_m", "💸 Minimum withdraw (TK) লিখুন"),
            "r": ("rc_r", "🎁 Refer reward (TK) লিখুন। (রেফার করা user-এর প্রথম OTP আসলে)"),
            "c": ("rc_c", "🤝 Refer commission (%) লিখুন। রেফার করা user প্রতি OTP-তে যা পায় তার এই % referrer পাবে। বন্ধ করতে <code>0</code>।"),
            "d": ("rc_d", "⏱ Cooldown (সেকেন্ড) লিখুন। একজন user কত সেকেন্ড পরপর number নিতে পারবে। যেমন <code>5</code>"),
        }
        if sub in prompts:
            ask_input(ev, prompts[sub][0])
            await revise(ev, prompts[sub][1], [nav_back("rc")])
            return
        t, btns = view_rates()
        await revise(ev, t, btns)
    elif cmd == "nc":
        if sub.isdigit() and 1 <= int(sub) <= 5:
            CFG["num_count"] = int(sub)
            persist_cfg()
        t, btns = view_numcount()
        await revise(ev, t, btns)

    # ---------------- FIREBASE
    elif cmd == "fb":
        if sub in ("u", "t") and datastore.fixed:
            await ev.ans("🔒 Firebase কোডে বসানো আছে, কোড থেকেই বদলান।", True)
            return
        if sub == "u":
            ask_input(ev, "fbup")
            await revise(ev, ("🔥 Firebase থেকে নামানো <b>service-account .json ফাইলটা</b> এখন এখানে পাঠান।\n\n"
                             "পাঠানোর পর বট মেসেজটা মুছে ফেলবে (কারণ ওতে private key আছে)।"), [nav_back("fb")])
            return
        elif sub == "t":
            await ev.ans("⏳ Please wait...")
            try:
                if datastore.fs is not None:
                    await datastore.disconnect_firebase()
                else:
                    cred, _on = datastore.saved_cred()
                    if not cred:
                        await revise(ev, "❌ আগে JSON ফাইল upload করুন।", [nav_back("fb")])
                        return
                    await datastore.connect_firebase(cred)
            except Exception as e:
                log.warning("firebase toggle failed: %s", e)
                await revise(ev, f"❌ কাজ হয়নি।\n<code>{htm(str(e)[:200])}</code>", [nav_back("fb")])
                return
        elif sub == "x":
            await ev.ans("⏳ Testing...")
            try:
                ms = await datastore.test_firebase()
                await revise(ev, f"✅ Firebase OK ({ms} ms)", [nav_back("fb")])
            except Exception as e:
                await revise(ev, f"❌ Firebase error\n<code>{htm(str(e)[:200])}</code>", [nav_back("fb")])
            return
        t, btns = view_fb()
        await revise(ev, t, btns)

    # ---------------- SETTINGS
    elif cmd == "se":
        prompts = {
            "t": ("se_t", "⏱ Number-এর উপরে কত মিনিট দেখাবে? (১–১২০) যেমন <code>15</code>"),
            "w": ("se_w", "🏦 Withdraw method গুলো কমা দিয়ে লিখুন, যেমন: <code>bKash, Nagad, Rocket</code>"),
        }
        if sub == "m":
            CFG["maint"] = not CFG["maint"]
            persist_cfg()
        elif sub in prompts:
            ask_input(ev, prompts[sub][0])
            await revise(ev, prompts[sub][1], [nav_back("se")])
            return
        t, btns = view_settings()
        await revise(ev, t, btns)
    elif cmd in ("uc", "bl"):
        ask_input(ev, "uc_find", mode="u" if cmd == "uc" else "b")
        await revise(ev, "👤 User-এর <b>ID</b> বা @username পাঠান:", [nav_back("se")])
    elif cmd == "ub" and sub.isdigit():
        tid = int(sub)
        if tid in USERS:
            USERS[tid]["banned"] = not USERS[tid].get("banned")
            persist_user(tid)
        t, btns = view_member(tid, "u")
        await revise(ev, t, btns)
    elif cmd in ("ba", "br") and sub.isdigit():
        ask_input(ev, "bal_add" if cmd == "ba" else "bal_sub", tid=int(sub))
        await revise(ev, ("➕ কত TK যোগ করবেন? সংখ্যা লিখুন:" if cmd == "ba" else "➖ কত TK কাটবেন? সংখ্যা লিখুন:"),
                   [nav_back("se")])
    elif data == "wl":
        t, btns = view_welcome()
        await revise(ev, t, btns)
    elif data == "wl:s":
        ask_input(ev, "setwel")
        await revise(ev, "✏️ নতুন Welcome message পাঠান।\n<code>{name}</code> লিখলে user-এর নাম বসবে।", [nav_back("wl")])
    elif data == "wq":
        t, btns = view_wd_queue()
        await revise(ev, t, btns)
    elif cmd in ("qa", "qr") and sub:
        rec = await settle_withdraw(sub, cmd == "qa")
        if not rec:
            await ev.ans("এই request আগেই প্রসেস হয়েছে।", True)
        t, btns = view_wd_queue()
        await revise(ev, t, btns)
    elif data == "st":
        t, btns = view_stats()
        await revise(ev, t, btns)
    else:
        await ev.ans("❓ Unknown", False)


# ----------------------------------------------------------------------------
# UPDATE LOOP / STARTUP
# ----------------------------------------------------------------------------
async def on_update(u):
    try:
        if "message" in u:
            await on_msg(u["message"])
        elif "callback_query" in u:
            await on_btn(u["callback_query"])
    except Exception as e:
        log.exception("update error: %s", e)


async def run_updates():
    offset = 0
    while True:
        try:
            ups = await tg_call("getUpdates", offset=offset, timeout=30,
                            allowed_updates=["message", "callback_query"])
        except TgFail as e:
            if e.code == 409:
                log.error("409 Conflict: another instance of this bot is running! Stop the other one.")
                await asyncio.sleep(10)
            else:
                await asyncio.sleep(3)
            continue
        for u in ups or []:
            offset = max(offset, u["update_id"] + 1)
            spawn(on_update(u))


async def load_data():
    d = await datastore.get("vx_settings", "main") or {}
    CFG.clear()
    CFG.update(copy.deepcopy(CFG_DEFAULTS))
    CFG.update(d)
    if CFG.get("welcome") == PREV_WELCOME:
        CFG["welcome"] = WELCOME_TEXT
    code_keys = [str(k).strip() for k in VOLTX_API_KEYS if str(k).strip()]
    env_key = os.environ.get("VOLTX_API_KEY", "").strip()
    if env_key:
        code_keys.append(env_key)
    added = False
    for k in code_keys:
        if k not in CFG["vx_keys"]:
            CFG["vx_keys"].append(k)
            added = True
    if added:
        await datastore.set("vx_settings", "main", copy.deepcopy(CFG))
    for who, ud in await datastore.all("vx_users"):
        try:
            base = make_user({"id": int(who)})
            base.update(ud)
            USERS[int(who)] = base
        except ValueError:
            pass
    t = clock()
    for n, rec in await datastore.all("vx_assigned"):
        if t - rec.get("ts", 0) <= HOLD_SECS:
            HELD[n] = rec
    for wid, rec in await datastore.all("vx_withdraws"):
        if rec.get("status") == "pending":
            WDQ[wid] = rec
    log.info("loaded: %d users, %d assigned numbers, %d pending withdraws", len(USERS), len(HELD), len(WDQ))


async def boot():
    if not BOT_KEY or BOT_KEY == "PUT_YOUR_BOT_TOKEN_HERE":
        raise SystemExit("BOT_TOKEN বসানো হয়নি। ফাইলের EDIT HERE অংশে BotFather-এর token বসান।")
    if not ADMINS:
        raise SystemExit("OWNER_ID_LIST বসানো হয়নি। ফাইলের EDIT HERE অংশে আপনার Telegram numeric id বসান।")
    asyncio.get_running_loop().set_default_executor(ThreadPoolExecutor(max_workers=48))
    datastore.init()
    await load_data()
    me = await tg_call("getMe")
    ME["username"] = me.get("username", "")
    log.info("Bot started as @%s", ME["username"])
    await tg_try("deleteWebhook", drop_pending_updates=False)
    cmds = [{"command": "start", "description": "Start the bot"},
            {"command": "menu", "description": "Open main menu"},
            {"command": "cancel", "description": "Cancel current action"}]
    await tg_try("setMyCommands", commands=cmds)
    await tg_try("setChatMenuButton", menu_button={"type": "commands"})
    for o in ADMINS:
        await tg_try("setMyCommands", commands=cmds + [{"command": "owner", "description": "Owner panel"}],
                   scope={"type": "chat", "chat_id": o})
    await asyncio.gather(run_updates(), console_poll(), access_poll(), sms_poll(), feed_worker(), gc_loop(), ping_server())


if __name__ == "__main__":
    try:
        asyncio.run(boot())
    except KeyboardInterrupt:
        pass
