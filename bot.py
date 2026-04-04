#!/usr/bin/env python3
"""
🎬 PuweDownloaderBot — Premium Video Downloader
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
 • Stars-based Premium: trial / monthly / lifetime / gift
 • Per-user settings: language, auto-dl
 • Support tickets (Premium only → forwarded to admin)
 • Admin panel: /stats  /broadcast  /admin
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
"""

import os, re, asyncio, time, sqlite3, json, logging, urllib.parse
from dotenv import load_dotenv
load_dotenv()
from datetime import datetime, date
from telegram import (
    Update, InlineKeyboardButton, InlineKeyboardMarkup,
    LabeledPrice, BotCommand
)
from telegram.ext import (
    Application, MessageHandler, CallbackQueryHandler,
    CommandHandler, ContextTypes, filters, PreCheckoutQueryHandler
)
import yt_dlp

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(message)s",
    level=logging.INFO,
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ══════════════════════════════════════════════
#  CONFIG
# ══════════════════════════════════════════════
BOT_TOKEN     = os.getenv("BOT_TOKEN", "")
BOT_USERNAME  = "PuweDownloaderBot"
SITE_URL      = os.getenv("SITE_URL", "https://puwedown.bothost.tech")
ADMIN_IDS     = {5268649092}
DB_FILE        = "bot.db"
DOWNLOADS_DIR  = "downloads"

# ── Одноразові токени для логіну через бота ──
import secrets as _secrets, threading as _threading
_LT_LOCK = _threading.Lock()
LOGIN_TTL = 900  # 15 хвилин
_LT_FILE  = os.path.join(os.path.dirname(__file__), "login_tokens.json")

def _lt_load():
    try:
        if os.path.exists(_LT_FILE):
            return json.loads(open(_LT_FILE, encoding="utf-8").read())
    except: pass
    return {}

def _lt_save(data):
    try:
        with open(_LT_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except: pass

def create_login_token(uid: int) -> str:
    token = _secrets.token_urlsafe(32)
    with _LT_LOCK:
        data = _lt_load()
        data = {t: v for t, v in data.items()
                if v["uid"] != uid and v["expires"] > time.time()}
        data[token] = {"uid": uid, "expires": time.time() + LOGIN_TTL}
        _lt_save(data)
    return token

# ── Stars pricing ──
TRIAL_STARS    = 5
TRIAL_DAYS     = 7
MONTHLY_BASE   = 50
MONTHLY_EXTRA  = 25
LIFETIME_STARS = 500
MAX_MONTHS     = 12

VIDEO_URL_PATTERN = re.compile(r"https?://(?:www\.)?\S+")

# ── Search limits ──
FREE_SEARCH_DL_DAY   = 3
PREMIUM_SEARCH_DL_DAY = 12


def calc_price(months: int) -> int:
    return MONTHLY_BASE + MONTHLY_EXTRA * (months - 1)


# ══════════════════════════════════════════════
#  DATABASE  (WAL режим — быстрее при параллельных запросах)
# ══════════════════════════════════════════════

# Один глобальный коннект вместо открытия нового на каждый запрос
_DB_CONN = None
_DB_LOCK = _threading.Lock()

def _conn() -> sqlite3.Connection:
    global _DB_CONN
    if _DB_CONN is None:
        c = sqlite3.connect(DB_FILE, check_same_thread=False)
        c.row_factory = sqlite3.Row
        # WAL = читатели не блокируют писателя и наоборот
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA cache_size=10000")
        _DB_CONN = c
    return _DB_CONN


def db_init():
    with _DB_LOCK:
        c = _conn()
        c.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            user_id       INTEGER PRIMARY KEY,
            username      TEXT    DEFAULT '',
            first_name    TEXT    DEFAULT '',
            language      TEXT    DEFAULT 'ru',
            auto_dl       INTEGER DEFAULT 0,
            watermark_on  INTEGER DEFAULT 1,
            trial_used    INTEGER DEFAULT 0,
            premium_until INTEGER DEFAULT 0,
            downloads     INTEGER DEFAULT 0,
            stars_spent   INTEGER DEFAULT 0,
            joined_at     INTEGER DEFAULT 0,
            last_seen     INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS transactions (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            stars      INTEGER,
            tx_type    TEXT,
            months     INTEGER DEFAULT 0,
            payload    TEXT    DEFAULT '',
            created_at INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS support_tickets (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            message    TEXT,
            status     TEXT    DEFAULT 'open',
            created_at INTEGER DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS search_downloads (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id    INTEGER,
            date_str   TEXT,
            count      INTEGER DEFAULT 0,
            UNIQUE(user_id, date_str)
        );
        CREATE INDEX IF NOT EXISTS idx_premium ON users(premium_until);
        """)
    logger.info("DB ready")


def db_get(uid: int):
    with _DB_LOCK:
        return _conn().execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()


def db_upsert(uid: int, username: str, first_name: str):
    now = int(time.time())
    with _DB_LOCK:
        c = _conn()
        if c.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone():
            c.execute(
                "UPDATE users SET username=?, first_name=?, last_seen=? WHERE user_id=?",
                (username, first_name, now, uid),
            )
        else:
            c.execute(
                "INSERT INTO users (user_id,username,first_name,joined_at,last_seen) VALUES (?,?,?,?,?)",
                (uid, username, first_name, now, now),
            )
        c.commit()


def db_set(uid: int, field: str, value):
    if field not in {"language", "auto_dl"}:
        return
    with _DB_LOCK:
        c = _conn()
        c.execute(f"UPDATE users SET {field}=? WHERE user_id=?", (value, uid))
        c.commit()


def db_add_premium(uid: int, days: int):
    now = int(time.time())
    with _DB_LOCK:
        c = _conn()
        row = c.execute("SELECT premium_until FROM users WHERE user_id=?", (uid,)).fetchone()
        if not row:
            return
        pu = row["premium_until"]
        if days == -1:
            new = -1
        elif pu == -1:
            new = -1
        elif pu > now:
            new = pu + days * 86400
        else:
            new = now + days * 86400
        c.execute("UPDATE users SET premium_until=? WHERE user_id=?", (new, uid))
        c.commit()


def db_mark_trial(uid: int):
    with _DB_LOCK:
        c = _conn()
        c.execute("UPDATE users SET trial_used=1 WHERE user_id=?", (uid,))
        c.commit()


def db_inc_dl(uid: int):
    with _DB_LOCK:
        c = _conn()
        c.execute("UPDATE users SET downloads=downloads+1 WHERE user_id=?", (uid,))
        c.commit()


def db_add_stars(uid: int, stars: int):
    with _DB_LOCK:
        c = _conn()
        c.execute("UPDATE users SET stars_spent=stars_spent+? WHERE user_id=?", (stars, uid))
        c.commit()


def db_log_tx(uid: int, stars: int, tx_type: str, months: int = 0, payload: str = ""):
    with _DB_LOCK:
        c = _conn()
        c.execute(
            "INSERT INTO transactions (user_id,stars,tx_type,months,payload,created_at) VALUES (?,?,?,?,?,?)",
            (uid, stars, tx_type, months, payload, int(time.time())),
        )
        c.commit()


def db_add_ticket(uid: int, msg: str) -> int:
    with _DB_LOCK:
        c = _conn()
        cur = c.execute(
            "INSERT INTO support_tickets (user_id,message,created_at) VALUES (?,?,?)",
            (uid, msg, int(time.time())),
        )
        c.commit()
        return cur.lastrowid


def db_get_search_dl_count(uid: int) -> int:
    today = date.today().isoformat()
    with _DB_LOCK:
        row = _conn().execute(
            "SELECT count FROM search_downloads WHERE user_id=? AND date_str=?",
            (uid, today)
        ).fetchone()
        return row[0] if row else 0


def db_inc_search_dl(uid: int):
    today = date.today().isoformat()
    with _DB_LOCK:
        c = _conn()
        c.execute(
            "INSERT INTO search_downloads (user_id, date_str, count) VALUES (?,?,1) "
            "ON CONFLICT(user_id, date_str) DO UPDATE SET count=count+1",
            (uid, today)
        )
        c.commit()


def db_stats() -> dict:
    now = int(time.time())
    today = int(datetime.now().replace(hour=0, minute=0, second=0).timestamp())
    with _DB_LOCK:
        c = _conn()
        total   = c.execute("SELECT COUNT(*) FROM users").fetchone()[0]
        premium = c.execute(
            "SELECT COUNT(*) FROM users WHERE premium_until=-1 OR premium_until>?", (now,)
        ).fetchone()[0]
        new_today = c.execute(
            "SELECT COUNT(*) FROM users WHERE joined_at>=?", (today,)
        ).fetchone()[0]
        dls   = c.execute("SELECT COALESCE(SUM(downloads),0) FROM users").fetchone()[0]
        stars = c.execute("SELECT COALESCE(SUM(stars),0) FROM transactions").fetchone()[0]
    return dict(total=total, premium=premium, today=new_today, downloads=dls, stars=stars)


def db_all_uids() -> list:
    with _DB_LOCK:
        return [r[0] for r in _conn().execute("SELECT user_id FROM users").fetchall()]


def db_by_username(username: str):
    u = username.lstrip("@").lower()
    with _DB_LOCK:
        return _conn().execute(
            "SELECT * FROM users WHERE LOWER(username)=?", (u,)
        ).fetchone()


# ══════════════════════════════════════════════
#  TRANSLATIONS
# ══════════════════════════════════════════════
T = {
    "ru": {
        "start": (
            "🎬 <b>Добро пожаловать, {name}!</b>\n\n"
            "Скачиваю видео с <b>YouTube, TikTok, Instagram, Twitter</b>\n"
            "и ещё сотен платформ.\n\n"
            "✨ <b>Бесплатно</b> — с вотермарком\n"
            "👑 <b>Premium</b> — без вотермарка, приоритет, поддержка\n\n"
            "📎 <i>Просто кинь ссылку — сделаю всё сам!</i>"
        ),
        "help": (
            "📋 <b>Команды:</b>\n\n"
            "🎬  Отправь ссылку на видео\n"
            "🔍  /search yt запрос — поиск YouTube\n"
            "🔍  /search tt запрос — поиск TikTok\n"
            "/sub       — 👑 Premium подписка\n"
            "/profile   — 👤 Мой профиль\n"
            "/settings  — ⚙️  Настройки\n"
            "/ticket    — 🎫 Поддержка (Premium)\n"
            "/help      — 📋 Справка"
        ),
        "analyzing":   "⏳ Анализирую...",
        "err_url":     "❌ Не удалось получить данные о видео.\n<i>Попробуй другую ссылку.</i>",
        "choose_q":    "🎥 <b>{title}</b>\n\n<i>Выбери качество:</i>",
        "downloading": "⬇️ Загружаю… Жди немного",
        "sending":     "📤 Отправляю в Telegram…",
        "done_cap":    "✅ @{bot}",
        "err_dl":      "❌ Ошибка: <code>{err}</code>",
        "audio_only":  "🎵 Только аудио (MP3)",
        "sub_menu": (
            "👑 <b>Premium подписка</b>\n\n"
            "Что входит:\n"
            "┣ ⚡ Приоритетная загрузка\n"
            "┣ 🎛️ Расширенные настройки\n"
            "┗ 🎫 Прямая линия поддержки\n\n"
            "{status}"
        ),
        "sub_none":     "📊 Статус: Бесплатный аккаунт",
        "sub_active":   "📊 Статус: 👑 <b>Premium</b> до <b>{date}</b>",
        "sub_lifetime": "📊 Статус: 👑 <b>Premium навсегда</b> ✨",
        "trial_used":   "✅ использован",
        "profile": (
            "👤 <b>Профиль</b>\n\n"
            "🆔 <code>{user_id}</code>  •  {name}\n"
            "📊 {status}\n\n"
            "📥 Скачано: <b>{dl}</b>\n"
            "⭐ Потрачено звёзд: <b>{stars}</b>\n"
            "📅 С нами с: <b>{joined}</b>"
        ),
        "p_active":   "👑 Premium до {date}",
        "p_lifetime": "👑 Premium навсегда ✨",
        "p_none":     "🆓 Бесплатный",
        "settings":    "⚙️ <b>Настройки</b>\n\nНастрой бота под себя:",
        "ticket_prem": "👑 Поддержка доступна только Premium пользователям\n\nПолучить: /sub",
        "ticket_ask":  "✉️ <b>Напиши сообщение</b> — я передам его в поддержку:\n\n<i>Следующее сообщение станет тикетом</i>",
        "ticket_sent": "✅ <b>Тикет отправлен!</b> Ответим как можно скорее.",
        "ticket_adm":  "🎫 <b>Тикет #{tid}</b>\nОт: {name}  |  <code>{ticket_uid}</code>\n\n{msg}",
        "inv_trial_t":    "🎁 Пробный Premium — 7 дней",
        "inv_trial_d":    "Попробуй все возможности Premium на 7 дней!",
        "inv_month_t":    "👑 Premium на {n} {mw}",
        "inv_month_d":    "Premium на {n} {mw}. Скачивай без ограничений!",
        "inv_life_t":     "♾️ Premium навсегда",
        "inv_life_d":     "Вечный доступ ко всем функциям бота!",
        "inv_gtrial_t":   "🎁 Подарок: Premium 7 дней → @{to}",
        "inv_gmonth_t":   "🎁 Подарок: Premium {n} {mw} → @{to}",
        "inv_glife_t":    "🎁 Подарок: Premium навсегда → @{to}",
        "inv_gift_d":     "Тебе дарят Premium в боте @PuweDownloaderBot!",
        "paid_trial":   "🎉 <b>Пробный Premium активирован!</b>\n\n👑 7 дней без ограничений — пользуйся!",
        "paid_month":   "🎉 <b>Premium активирован на {n} {mw}!</b>\n\n👑 Enjoy!",
        "paid_life":    "🎉 <b>Вечный Premium активирован! ✨</b>\n\n👑 Ты навсегда с нами!",
        "gift_recv":    "🎁 <b>Тебе подарили Premium!</b>\n\nОт: {from_name}\nПериод: {period}\n\n👑 Наслаждайся!",
        "gift_ok":      "🎉 Подарок отправлен <b>@{to}</b>!",
        "stats": (
            "📊 <b>Статистика бота</b>\n\n"
            "👥 Пользователей: <b>{total}</b>\n"
            "👑 Premium: <b>{premium}</b>\n"
            "🆕 Сегодня: <b>{today}</b>\n"
            "📥 Скачиваний: <b>{downloads}</b>\n"
            "⭐ Звёзд собрано: <b>{stars}</b>"
        ),
        "bc_ask":  "📢 <b>Рассылка</b>\n\nОтправь текст (HTML разрешён):\n<i>Следующее сообщение уйдёт всем</i>",
        "bc_done": "✅ Разослано <b>{n}</b> пользователям",
        "no_admin": "❌ Недостаточно прав",
        "gift_ask": (
            "🎁 <b>Кому подарить Premium?</b>\n\n"
            "Отправь <code>@username</code> или числовой ID пользователя:"
        ),
        "gift_404":  "❌ Пользователь не найден. Убедись, что он уже запускал бота.",
        "gift_self": "😅 Нельзя подарить самому себе!",
        "gift_sel":  "🎁 Подарить <b>@{to}</b>\n\nВыбери период:",
        "mw1": "месяц", "mw2": "месяца", "mw5": "месяцев",
        "on": "✅ Вкл", "off": "❌ Выкл",
        "search_usage":    "🔍 <b>Поиск видео</b>\n\nИспользование:\n<code>/search yt запрос</code> — поиск на YouTube\n<code>/search tt запрос</code> — поиск на TikTok\n\nПример: <code>/search yt смешные коты</code>",
        "search_searching": "🔍 Ищу <b>{query}</b> на {platform}…",
        "search_no_results":"😕 Ничего не нашёл по запросу <b>{query}</b>.",
        "search_results":   "🔍 <b>Результаты: {query}</b>\n\nВыбери видео для скачивания:",
        "search_limit_free":"⚠️ Лимит исчерпан!\n\nБесплатно: <b>{limit} скачиваний/день</b> через поиск.\nОсталось сегодня: <b>0</b>\n\n👑 Premium даёт <b>12/день</b> → /sub",
        "search_limit_prem":"⚠️ Лимит исчерпан!\n\nPremium: <b>{limit} скачиваний/день</b> через поиск.\nПриходи завтра 😊",
        "search_dl_count":  "📊 Скачиваний через поиск сегодня: <b>{used}/{limit}</b>",
    },
    "en": {
        "start": (
            "🎬 <b>Welcome, {name}!</b>\n\n"
            "I download videos from <b>YouTube, TikTok, Instagram, Twitter</b>\n"
            "and hundreds of other platforms.\n\n"
            "✨ <b>Free</b> — скачивай без ограничений\n"
            "👑 <b>Premium</b> — приоритет и поддержка\n\n"
            "📎 <i>Just send a link — I'll handle the rest!</i>"
        ),
        "help": (
            "📋 <b>Commands:</b>\n\n"
            "🎬  Send a video link\n"
            "🔍  /search yt query — search YouTube\n"
            "🔍  /search tt query — search TikTok\n"
            "/sub       — 👑 Premium subscription\n"
            "/profile   — 👤 My profile\n"
            "/settings  — ⚙️  Settings\n"
            "/ticket    — 🎫 Support (Premium)\n"
            "/help      — 📋 Help"
        ),
        "analyzing":   "⏳ Analyzing...",
        "err_url":     "❌ Couldn't get video info.\n<i>Try a different link.</i>",
        "choose_q":    "🎥 <b>{title}</b>\n\n<i>Choose quality:</i>",
        "downloading": "⬇️ Downloading… Please wait",
        "sending":     "📤 Sending to Telegram…",
        "done_cap":    "✅ @{bot}",
        "err_dl":      "❌ Error: <code>{err}</code>",
        "audio_only":  "🎵 Audio only (MP3)",
        "sub_menu": (
            "👑 <b>Premium Subscription</b>\n\n"
            "What's included:\n"
            "┣ ⚡ Priority downloads\n"
            "┣ 🎛️ Advanced settings\n"
            "┗ 🎫 Direct support line\n\n"
            "{status}"
        ),
        "sub_none":     "📊 Status: Free account",
        "sub_active":   "📊 Status: 👑 <b>Premium</b> until <b>{date}</b>",
        "sub_lifetime": "📊 Status: 👑 <b>Premium forever</b> ✨",
        "trial_used":   "✅ used",
        "profile": (
            "👤 <b>Profile</b>\n\n"
            "🆔 <code>{user_id}</code>  •  {name}\n"
            "📊 {status}\n\n"
            "📥 Downloads: <b>{dl}</b>\n"
            "⭐ Stars spent: <b>{stars}</b>\n"
            "📅 Member since: <b>{joined}</b>"
        ),
        "p_active":   "👑 Premium until {date}",
        "p_lifetime": "👑 Premium forever ✨",
        "p_none":     "🆓 Free",
        "settings":    "⚙️ <b>Settings</b>\n\nCustomize your experience:",
        "ticket_prem": "👑 Support is for Premium users only\n\nGet: /sub",
        "ticket_ask":  "✉️ <b>Write your message</b> — I'll forward it to support:\n\n<i>Your next message will become a ticket</i>",
        "ticket_sent": "✅ <b>Ticket sent!</b> We'll reply as soon as possible.",
        "ticket_adm":  "🎫 <b>Тикет #{tid}</b>\nОт: {name}  |  <code>{ticket_uid}</code>\n\n{msg}",
        "inv_trial_t":    "🎁 Trial Premium — 7 days",
        "inv_trial_d":    "Try all Premium features for 7 days!",
        "inv_month_t":    "👑 Premium for {n} {mw}",
        "inv_month_d":    "Premium for {n} {mw}. Download without limits!",
        "inv_life_t":     "♾️ Premium forever",
        "inv_life_d":     "Unlimited access to all bot features forever!",
        "inv_gtrial_t":   "🎁 Gift: Premium 7 days → @{to}",
        "inv_gmonth_t":   "🎁 Gift: Premium {n} {mw} → @{to}",
        "inv_glife_t":    "🎁 Gift: Premium forever → @{to}",
        "inv_gift_d":     "You're receiving a Premium gift in @PuweDownloaderBot!",
        "paid_trial":   "🎉 <b>Trial Premium activated!</b>\n\n👑 7 days without limits — enjoy!",
        "paid_month":   "🎉 <b>Premium activated for {n} {mw}!</b>\n\n👑 Enjoy!",
        "paid_life":    "🎉 <b>Lifetime Premium activated! ✨</b>\n\n👑 You're with us forever!",
        "gift_recv":    "🎁 <b>You received a Premium gift!</b>\n\nFrom: {from_name}\nPeriod: {period}\n\n👑 Enjoy!",
        "gift_ok":      "🎉 Gift sent to <b>@{to}</b>!",
        "stats": (
            "📊 <b>Bot Statistics</b>\n\n"
            "👥 Users: <b>{total}</b>\n"
            "👑 Premium: <b>{premium}</b>\n"
            "🆕 Today: <b>{today}</b>\n"
            "📥 Downloads: <b>{downloads}</b>\n"
            "⭐ Stars collected: <b>{stars}</b>"
        ),
        "bc_ask":  "📢 <b>Broadcast</b>\n\nSend message (HTML allowed):\n<i>Your next message goes to everyone</i>",
        "bc_done": "✅ Sent to <b>{n}</b> users",
        "no_admin": "❌ Insufficient permissions",
        "gift_ask": (
            "🎁 <b>Who to gift Premium?</b>\n\n"
            "Send <code>@username</code> or numeric user ID:"
        ),
        "gift_404":  "❌ User not found. Make sure they've started the bot.",
        "gift_self": "😅 Can't gift to yourself!",
        "gift_sel":  "🎁 Gift to <b>@{to}</b>\n\nChoose period:",
        "mw1": "month", "mw2": "months", "mw5": "months",
        "on": "✅ On", "off": "❌ Off",
        "search_usage":    "🔍 <b>Video Search</b>\n\nUsage:\n<code>/search yt query</code> — search YouTube\n<code>/search tt query</code> — search TikTok\n\nExample: <code>/search yt funny cats</code>",
        "search_searching": "🔍 Searching <b>{query}</b> on {platform}…",
        "search_no_results":"😕 Nothing found for <b>{query}</b>.",
        "search_results":   "🔍 <b>Results: {query}</b>\n\nChoose a video to download:",
        "search_limit_free":"⚠️ Daily limit reached!\n\nFree: <b>{limit} downloads/day</b> via search.\nLeft today: <b>0</b>\n\n👑 Premium gives <b>12/day</b> → /sub",
        "search_limit_prem":"⚠️ Daily limit reached!\n\nPremium: <b>{limit} downloads/day</b> via search.\nCome back tomorrow 😊",
        "search_dl_count":  "📊 Search downloads today: <b>{used}/{limit}</b>",
    },
}


def get_lang(uid: int) -> str:
    u = db_get(uid)
    return u["language"] if u else "ru"


def tx(uid: int, key: str, **kw) -> str:
    lang = get_lang(uid)
    d = T.get(lang, T["ru"])
    text = d.get(key, T["ru"].get(key, f"[{key}]"))
    return text.format(**kw) if kw else text


def mword(n: int, lang: str) -> str:
    if lang == "en":
        return "month" if n == 1 else "months"
    if n % 10 == 1 and n % 100 != 11:
        return T["ru"]["mw1"]
    if n % 10 in (2, 3, 4) and n % 100 not in (12, 13, 14):
        return T["ru"]["mw2"]
    return T["ru"]["mw5"]


def is_premium(uid: int) -> bool:
    if uid in ADMIN_IDS:
        return True
    u = db_get(uid)
    if not u:
        return False
    pu = u["premium_until"]
    return pu == -1 or pu > int(time.time())


def premium_label(uid: int) -> str:
    if uid in ADMIN_IDS:
        lang = get_lang(uid)
        return "👑 <b>Premium навсегда</b> ✨  👨‍💻" if lang == "ru" else "👑 <b>Premium forever</b> ✨  👨‍💻"
    u = db_get(uid)
    if not u:
        return tx(uid, "p_none")
    pu = u["premium_until"]
    if pu == -1:
        return tx(uid, "p_lifetime")
    if pu > int(time.time()):
        return tx(uid, "p_active", date=datetime.fromtimestamp(pu).strftime("%d.%m.%Y"))
    return tx(uid, "p_none")


# ══════════════════════════════════════════════
#  DOWNLOADER  (оптимизированный)
# ══════════════════════════════════════════════

# Формат без filesize фильтра — убирает 2-3 секунды на проверку размера
SINGLE = (
    "best[vcodec!=none][acodec!=none][ext=mp4]"
    "/best[vcodec!=none][acodec!=none]"
    "/best[ext=mp4]"
    "/best"
)

def _yt_opts(out_tpl: str, fmt: str) -> dict:
    """Базовые опции yt-dlp с максимальной скоростью."""
    return {
        "outtmpl":                       out_tpl,
        "format":                        fmt,
        "quiet":                         True,
        "no_warnings":                   True,
        "ignoreerrors":                  False,
        "fragment_retries":              3,
        "retries":                       3,
        "check_formats":                 False,   # не проверять форматы — быстрее
        "concurrent_fragment_downloads": 4,        # 4 потока скачки фрагментов
        "http_chunk_size":               10485760, # 10 МБ чанки
        "no_part":                       True,     # без .part файлов
        "noprogress":                    True,
        "skip_unavailable_fragments":    True,
    }


class Downloader:
    def get_info(self, url: str):
        opts = {
            "quiet": True,
            "no_warnings": True,
            "check_formats": False,  # быстрее без проверки форматов
        }
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                return ydl.extract_info(url, download=False)
        except Exception:
            return None

    async def download(self, url: str, fmt_id: str):
        import glob
        ts = int(time.time())
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        out_tpl = f"{DOWNLOADS_DIR}/v_{ts}.%(ext)s"

        def _find_downloaded() -> str | None:
            for ext in ("mp4", "mkv", "webm", "mp3", "m4a"):
                p = f"{DOWNLOADS_DIR}/v_{ts}.{ext}"
                if os.path.exists(p) and os.path.getsize(p) > 1024:
                    return p
            matches = glob.glob(f"{DOWNLOADS_DIR}/v_{ts}.*")
            for p in sorted(matches):
                if os.path.getsize(p) > 1024 and not p.endswith(".part"):
                    return p
            return None

        def _try_download(fmt: str) -> str | None:
            opts = _yt_opts(out_tpl, fmt)
            try:
                yt_dlp.YoutubeDL(opts).download([url])
            except Exception:
                pass
            found = _find_downloaded()
            if found:
                return found
            # Fallback: android_embedded для YouTube 403
            opts2 = dict(opts)
            opts2["extractor_args"] = {"youtube": {"player_client": ["android_embedded"]}}
            try:
                yt_dlp.YoutubeDL(opts2).download([url])
            except Exception:
                pass
            return _find_downloaded()

        # ── Аудио ──
        if fmt_id == "bestaudio":
            return await asyncio.to_thread(
                _try_download,
                "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio"
            )

        # ── Видео ──
        file = await asyncio.to_thread(_try_download, SINGLE)

        # ── Fallback ──
        if not file:
            file = await asyncio.to_thread(_try_download, "best[ext=mp4]/best")

        return file

    async def download_photos(self, url: str) -> list:
        ts = int(time.time())
        os.makedirs(DOWNLOADS_DIR, exist_ok=True)
        out = f"{DOWNLOADS_DIR}/p_{ts}_%(autonumber)s.%(ext)s"
        opts = {
            "outtmpl": out,
            "quiet": True,
            "no_warnings": True,
            "format": "best",
            "extract_flat": False,
        }
        await asyncio.to_thread(lambda: yt_dlp.YoutubeDL(opts).download([url]))
        files = sorted([
            os.path.join(DOWNLOADS_DIR, f)
            for f in os.listdir(DOWNLOADS_DIR)
            if f.startswith(f"p_{ts}_") and f.lower().endswith((".jpg", ".jpeg", ".png", ".webp"))
        ])
        return files

    def search_videos(self, query: str, platform: str, max_results: int = 5) -> list:
        if platform == "yt":
            search_url = f"ytsearch{max_results}:{query}"
        else:
            search_url = f"tiktoksearch{max_results}:{query}"

        opts = {
            "quiet": True,
            "no_warnings": True,
            "extract_flat": True,
            "skip_download": True,
            "check_formats": False,
        }
        results = []
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(search_url, download=False)
                entries = info.get("entries", []) if info else []
                for e in entries:
                    if not e:
                        continue
                    url = e.get("url") or e.get("webpage_url")
                    if not url:
                        eid = e.get("id")
                        if platform == "yt" and eid:
                            url = f"https://www.youtube.com/watch?v={eid}"
                        elif eid:
                            url = f"https://www.tiktok.com/@{e.get('uploader','user')}/video/{eid}"
                    if not url:
                        continue
                    view_count = e.get("view_count") or 0
                    duration = e.get("duration") or 0
                    dur_str = ""
                    if duration:
                        mins, secs = divmod(int(duration), 60)
                        hrs, mins = divmod(mins, 60)
                        dur_str = f"{hrs}:{mins:02d}:{secs:02d}" if hrs else f"{mins}:{secs:02d}"
                    views_str = ""
                    if view_count >= 1_000_000:
                        views_str = f"{view_count/1_000_000:.1f}M 👁"
                    elif view_count >= 1_000:
                        views_str = f"{view_count//1_000}K 👁"
                    elif view_count:
                        views_str = f"{view_count} 👁"
                    results.append({
                        "title": (e.get("title") or "Unknown")[:60],
                        "url": url,
                        "duration": dur_str,
                        "views": views_str,
                    })
        except Exception as ex:
            logger.warning(f"Search error ({platform}): {ex}")
        return results[:max_results]

    def is_photo_post(self, info: dict) -> bool:
        if not info:
            return False
        fmts = info.get("formats", [])
        image_exts = {"jpg", "jpeg", "png", "webp"}
        has_video = any(
            f.get("vcodec", "none") not in ("none", None, "")
            and f.get("ext") not in image_exts
            for f in fmts
        )
        has_images = any(f.get("ext") in image_exts for f in fmts)
        heights = [f.get("height") for f in fmts if f.get("height")]
        return (has_images and not has_video) or (not heights and has_images)


dl = Downloader()


# ══════════════════════════════════════════════
#  KEYBOARDS
# ══════════════════════════════════════════════
def _btn(label: str, cb: str) -> InlineKeyboardButton:
    return InlineKeyboardButton(label, callback_data=cb)


def kb_main(uid: int) -> InlineKeyboardMarkup:
    lang = get_lang(uid)
    return InlineKeyboardMarkup([
        [_btn("👑 Premium", "sub"),
         _btn("👤 Профиль" if lang == "ru" else "👤 Profile", "profile")],
        [_btn("⚙️ Настройки" if lang == "ru" else "⚙️ Settings", "settings")],
    ])


def kb_sub(uid: int, months: int = 1) -> InlineKeyboardMarkup:
    u = db_get(uid)
    lang = u["language"] if u else "ru"
    trial_used = bool(u["trial_used"]) if u else False
    mw = mword(months, lang)
    stars = calc_price(months)

    rows = []
    if not trial_used:
        label = (f"🎁 Пробный — {TRIAL_STARS}⭐ (7 дней)"
                 if lang == "ru" else
                 f"🎁 Trial — {TRIAL_STARS}⭐ (7 days)")
        rows.append([_btn(label, "pay_trial")])
    else:
        used_txt = "🎁 Пробный — ✅ использован" if lang == "ru" else "🎁 Trial — ✅ used"
        rows.append([_btn(used_txt, "noop")])

    rows.append([
        _btn("➖", f"subm_dec_{months}"),
        _btn(f"📅 {months} {mw}  =  {stars}⭐", "noop"),
        _btn("➕", f"subm_inc_{months}"),
    ])
    buy_label = (f"💳 Купить {months} {mw} — {stars}⭐"
                 if lang == "ru" else
                 f"💳 Buy {months} {mw} — {stars}⭐")
    rows.append([_btn(buy_label, f"pay_months_{months}")])

    life_label = (f"♾️ Навсегда — {LIFETIME_STARS}⭐"
                  if lang == "ru" else
                  f"♾️ Forever — {LIFETIME_STARS}⭐")
    rows.append([_btn(life_label, "pay_lifetime")])

    gift_label = "🎁 Подарить Premium" if lang == "ru" else "🎁 Gift Premium"
    rows.append([_btn(gift_label, "gift_start")])
    rows.append([_btn("◀️ Назад" if lang == "ru" else "◀️ Back", "back_main")])

    return InlineKeyboardMarkup(rows)


def kb_gift_select(uid: int, to_uid: int, months: int = 1) -> InlineKeyboardMarkup:
    u = db_get(uid)
    lang = u["language"] if u else "ru"
    mw = mword(months, lang)
    stars = calc_price(months)

    rows = [
        [_btn(f"🎁 7 {'дней' if lang=='ru' else 'days'} — {TRIAL_STARS}⭐", f"gpay_trial_{to_uid}")],
        [
            _btn("➖", f"gsubm_dec_{months}_{to_uid}"),
            _btn(f"📅 {months} {mw}  =  {stars}⭐", "noop"),
            _btn("➕", f"gsubm_inc_{months}_{to_uid}"),
        ],
        [_btn(f"💳 {'Подарить' if lang=='ru' else 'Gift'} {months} {mw} — {stars}⭐", f"gpay_months_{months}_{to_uid}")],
        [_btn(f"♾️ {'Навсегда' if lang=='ru' else 'Forever'} — {LIFETIME_STARS}⭐", f"gpay_life_{to_uid}")],
        [_btn("◀️", "sub")],
    ]
    return InlineKeyboardMarkup(rows)


def kb_settings(uid: int) -> InlineKeyboardMarkup:
    u = db_get(uid)
    lang = u["language"] if u else "ru"
    auto = bool(u["auto_dl"]) if u else False

    def st(val: bool) -> str:
        return (T[lang]["on"] if val else T[lang]["off"])

    flag = "🇷🇺 Русский" if lang == "ru" else "🇺🇸 English"
    lang_lbl = f"🌍 Язык: {flag}" if lang == "ru" else f"🌍 Language: {flag}"
    auto_lbl = ("⬇️ Авто-загрузка: " if lang == "ru" else "⬇️ Auto-download: ") + st(auto)
    back_lbl = "◀️ Назад" if lang == "ru" else "◀️ Back"

    return InlineKeyboardMarkup([
        [_btn(lang_lbl, "set_lang")],
        [_btn(auto_lbl, "set_auto")],
        [_btn(back_lbl, "back_main")],
    ])


def kb_admin(lang: str = "ru") -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[
        _btn("📊 Статистика" if lang == "ru" else "📊 Stats", "admin_stats"),
        _btn("📢 Рассылка" if lang == "ru" else "📢 Broadcast", "admin_bc"),
    ]])


# ══════════════════════════════════════════════
#  COMMAND HANDLERS
# ══════════════════════════════════════════════
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    db_upsert(u.id, u.username or "", u.first_name or "")

    args = ctx.args or []
    if args and args[0] == "webapp":
        token = create_login_token(u.id)
        login_url = f"{SITE_URL}/login/{token}"
        text = (
            "🔑 <b>Ссылка для входа на сайт</b>\n\n"
            "⏱ Срок действия: <b>15 мин.</b>\n"
            "Откройте ссылку — вход произойдет автоматически.\n\n"
            "⚠️ Не передавайте ссылку другим!"
        )
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("🔓 Открыть сайт", url=login_url)]])
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)
        return

    if args and args[0] == "sub":
        user = db_get(u.id)
        pu = user["premium_until"] if user else 0
        now = int(time.time())
        if pu == -1:
            status = tx(u.id, "sub_lifetime")
        elif pu > now:
            status = tx(u.id, "sub_active", date=datetime.fromtimestamp(pu).strftime("%d.%m.%Y"))
        else:
            status = tx(u.id, "sub_none")
        await update.message.reply_text(
            tx(u.id, "sub_menu", status=status),
            parse_mode="HTML",
            reply_markup=kb_sub(u.id),
        )
        return

    await update.message.reply_text(
        tx(u.id, "start", name=u.first_name or "друг"),
        parse_mode="HTML",
        reply_markup=kb_main(u.id),
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    db_upsert(u.id, u.username or "", u.first_name or "")
    await update.message.reply_text(tx(u.id, "help"), parse_mode="HTML")


async def cmd_sub(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    db_upsert(u.id, u.username or "", u.first_name or "")
    user = db_get(u.id)
    pu = user["premium_until"] if user else 0
    now = int(time.time())
    if pu == -1:
        status = tx(u.id, "sub_lifetime")
    elif pu > now:
        status = tx(u.id, "sub_active", date=datetime.fromtimestamp(pu).strftime("%d.%m.%Y"))
    else:
        status = tx(u.id, "sub_none")
    await update.message.reply_text(
        tx(u.id, "sub_menu", status=status),
        parse_mode="HTML",
        reply_markup=kb_sub(u.id),
    )


async def cmd_profile(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    db_upsert(u.id, u.username or "", u.first_name or "")
    user = db_get(u.id)
    joined = (
        datetime.fromtimestamp(user["joined_at"]).strftime("%d.%m.%Y")
        if user and user["joined_at"] else "?"
    )
    lang = get_lang(u.id)
    await update.message.reply_text(
        tx(u.id, "profile",
           user_id=u.id,
           name=(u.first_name or "—") + ("  👨‍💻" if u.id in ADMIN_IDS else ""),
           status=premium_label(u.id),
           dl=user["downloads"] if user else 0,
           stars=user["stars_spent"] if user else 0,
           joined=joined),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup([[
            _btn("👑 Premium", "sub"),
            _btn("⚙️ Настройки" if lang == "ru" else "⚙️ Settings", "settings"),
        ]]),
    )


async def cmd_settings(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    db_upsert(u.id, u.username or "", u.first_name or "")
    await update.message.reply_text(
        tx(u.id, "settings"), parse_mode="HTML", reply_markup=kb_settings(u.id)
    )


async def cmd_ticket(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    db_upsert(u.id, u.username or "", u.first_name or "")
    if not is_premium(u.id):
        await update.message.reply_text(tx(u.id, "ticket_prem"), parse_mode="HTML")
        return
    ctx.user_data["awaiting_ticket"] = True
    await update.message.reply_text(tx(u.id, "ticket_ask"), parse_mode="HTML")


async def cmd_search(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    db_upsert(u.id, u.username or "", u.first_name or "")
    args = ctx.args

    if not args or len(args) < 2:
        await update.message.reply_text(tx(u.id, "search_usage"), parse_mode="HTML")
        return

    platform_raw = args[0].lower()
    if platform_raw in ("yt", "youtube"):
        platform = "yt"
        platform_name = "YouTube"
    elif platform_raw in ("tt", "tiktok"):
        platform = "tt"
        platform_name = "TikTok"
    else:
        await update.message.reply_text(tx(u.id, "search_usage"), parse_mode="HTML")
        return

    query = " ".join(args[1:])
    thread_id = update.message.message_thread_id

    m = await update.message.reply_text(
        tx(u.id, "search_searching", query=query, platform=platform_name),
        parse_mode="HTML",
    )

    results = await asyncio.to_thread(dl.search_videos, query, platform, 5)

    if not results:
        await m.edit_text(tx(u.id, "search_no_results", query=query), parse_mode="HTML")
        return

    prem = is_premium(u.id)
    limit = PREMIUM_SEARCH_DL_DAY if prem else FREE_SEARCH_DL_DAY
    used = db_get_search_dl_count(u.id)
    lang = get_lang(u.id)

    lines = []
    for i, r in enumerate(results, 1):
        meta = []
        if r["duration"]: meta.append(f"⏱ {r['duration']}")
        if r["views"]:    meta.append(r["views"])
        meta_str = "  ".join(meta)
        lines.append(f"{i}. <b>{r['title']}</b>\n   {meta_str}" if meta_str else f"{i}. <b>{r['title']}</b>")

    text = tx(u.id, "search_results", query=query) + "\n\n" + "\n\n".join(lines)
    if used < limit:
        remaining = limit - used
        quota_line = (
            f"\n\n📊 Осталось скачиваний через поиск сегодня: <b>{remaining}/{limit}</b>"
            if lang == "ru" else
            f"\n\n📊 Search downloads left today: <b>{remaining}/{limit}</b>"
        )
    else:
        quota_line = ""
    text += quota_line

    kb_rows = []
    for i, r in enumerate(results, 1):
        label = f"⬇️ {i}. {r['title'][:30]}…" if len(r['title']) > 30 else f"⬇️ {i}. {r['title']}"
        kb_rows.append([_btn(label, f"sdl_{u.id}_{i-1}")])

    search_key = f"search_{u.id}"
    ctx.bot_data[search_key] = {
        "results": results,
        "platform": platform,
        "thread_id": thread_id,
        "chat_id": update.message.chat_id,
    }

    await m.edit_text(text, parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb_rows))


async def cmd_stats(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS:
        await update.message.reply_text(tx(uid, "no_admin"))
        return
    s = db_stats()
    await update.message.reply_text(
        tx(uid, "stats", **s), parse_mode="HTML", reply_markup=kb_admin(get_lang(uid))
    )


async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS:
        await update.message.reply_text(tx(uid, "no_admin"))
        return
    ctx.user_data["awaiting_broadcast"] = True
    await update.message.reply_text(tx(uid, "bc_ask"), parse_mode="HTML")


async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if uid not in ADMIN_IDS:
        await update.message.reply_text(tx(uid, "no_admin"))
        return
    s = db_stats()
    await update.message.reply_text(
        tx(uid, "stats", **s), parse_mode="HTML", reply_markup=kb_admin(get_lang(uid))
    )


# ══════════════════════════════════════════════
#  DOWNLOAD HELPER
# ══════════════════════════════════════════════
async def do_download(ctx, msg, url: str, fmt_id: str, width: int, uid: int, chat_id: int, thread_id: int = None):
    try:
        file = await dl.download(url, fmt_id)
        if not file:
            await msg.edit_text(tx(uid, "err_dl", err="file not found"), parse_mode="HTML")
            return

        final = file
        await msg.edit_text(tx(uid, "sending"))

        size_mb = os.path.getsize(final) / (1024 * 1024)
        if size_mb > 49 and not final.endswith(".mp3"):
            lang = get_lang(uid)
            too_big = (
                f"⚠️ Файл слишком большой ({size_mb:.0f} МБ) — Telegram не принимает файлы >50 МБ.\n"
                "Попробуй выбрать качество пониже."
                if lang == "ru" else
                f"⚠️ File too large ({size_mb:.0f} MB) — Telegram doesn't accept files >50 MB.\n"
                "Try selecting a lower quality."
            )
            await msg.edit_text(too_big, parse_mode="HTML")
            for p in {file, final}:
                try:
                    if os.path.exists(p): os.remove(p)
                except Exception: pass
            return

        cap = tx(uid, "done_cap", bot=BOT_USERNAME)
        send_kwargs = {"caption": cap}
        if thread_id:
            send_kwargs["message_thread_id"] = thread_id
        with open(final, "rb") as fh:
            if final.endswith(".mp3"):
                await ctx.bot.send_audio(chat_id, fh, **send_kwargs)
            else:
                await ctx.bot.send_video(chat_id, fh, supports_streaming=True, **send_kwargs)

        try:
            await msg.delete()
        except Exception:
            pass

        if not is_premium(uid):
            u_row = db_get(uid)
            dl_count = (u_row["downloads"] + 1) if u_row else 1
            if dl_count % 3 == 0:
                lang = get_lang(uid)
                promo_text = (
                    "💡 <b>Устал от вотермарка?</b>\n\n"
                    "С <b>Premium</b> ты получаешь:\n"
                    "┣ 🎫 Прямой чат с поддержкой\n"
                    "┣ ⚡ Приоритетная загрузка\n"
                    "┗ ⚙️ Расширенные настройки\n\n"
                    "🎁 Попробуй <b>7 дней за 5⭐</b> — это дешевле чашки кофе!"
                    if lang == "ru" else
                    "💡 <b>Скачивай быстрее с Premium!</b>\n\n"
                    "With <b>Premium</b> you get:\n"
                    "┣ 🎫 Direct chat with support\n"
                    "┣ ⚡ Priority downloads\n"
                    "┗ ⚙️ Advanced settings\n\n"
                    "🎁 Try <b>7 days for 5⭐</b> — cheaper than a coffee!"
                )
                promo_kwargs = {"parse_mode": "HTML", "reply_markup": InlineKeyboardMarkup([[
                    _btn("👑 Попробовать Premium" if lang == "ru" else "👑 Try Premium", "sub")
                ]])}
                if thread_id:
                    promo_kwargs["message_thread_id"] = thread_id
                await ctx.bot.send_message(chat_id, promo_text, **promo_kwargs)

        db_inc_dl(uid)
        for p in {file, final}:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except Exception:
                pass

    except Exception as e:
        logger.exception("Download error")
        try:
            await msg.edit_text(tx(uid, "err_dl", err=str(e)[:120]), parse_mode="HTML")
        except Exception:
            pass


# ══════════════════════════════════════════════
#  MESSAGE HANDLER
# ══════════════════════════════════════════════
async def handle_message(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    text = update.message.text or ""
    db_upsert(u.id, u.username or "", u.first_name or "")
    thread_id = update.message.message_thread_id

    if ctx.user_data.get("awaiting_broadcast") and u.id in ADMIN_IDS:
        ctx.user_data.pop("awaiting_broadcast")
        uids = db_all_uids()
        sent = 0
        for uid in uids:
            try:
                await ctx.bot.send_message(uid, text, parse_mode="HTML")
                sent += 1
                await asyncio.sleep(0.04)
            except Exception:
                pass
        await update.message.reply_text(tx(u.id, "bc_done", n=sent), parse_mode="HTML")
        return

    if ctx.user_data.get("awaiting_ticket"):
        ctx.user_data.pop("awaiting_ticket")
        if is_premium(u.id):
            tid = db_add_ticket(u.id, text)
            await update.message.reply_text(tx(u.id, "ticket_sent"), parse_mode="HTML")
            for aid in ADMIN_IDS:
                try:
                    await ctx.bot.send_message(
                        aid,
                        tx(aid, "ticket_adm", tid=tid, name=u.first_name, ticket_uid=u.id, msg=text),
                        parse_mode="HTML",
                    )
                except Exception:
                    pass
        return

    if ctx.user_data.get("awaiting_gift_target"):
        ctx.user_data.pop("awaiting_gift_target")
        target = text.strip()
        to_user = None
        try:
            if target.lstrip("@").isdigit():
                to_user = db_get(int(target.lstrip("@")))
            else:
                to_user = db_by_username(target)
        except Exception:
            pass

        if not to_user:
            await update.message.reply_text(tx(u.id, "gift_404"), parse_mode="HTML")
            return
        if to_user["user_id"] == u.id:
            await update.message.reply_text(tx(u.id, "gift_self"), parse_mode="HTML")
            return

        to_uid = to_user["user_id"]
        to_name = to_user["username"] or to_user["first_name"] or str(to_uid)
        await update.message.reply_text(
            tx(u.id, "gift_sel", to=to_name),
            parse_mode="HTML",
            reply_markup=kb_gift_select(u.id, to_uid),
        )
        return

    url_match = VIDEO_URL_PATTERN.search(text)
    if not url_match:
        return

    url = url_match.group()

    chat_type = update.effective_chat.type
    is_group = chat_type in ("group", "supergroup", "channel")

    if is_group:
        send_kw = {"message_thread_id": thread_id} if thread_id else {}
        m = await update.message.reply_text("⏳ Скачиваю...", **send_kw)
        info = await asyncio.to_thread(dl.get_info, url)
        if not info:
            await m.edit_text(tx(u.id, "err_url"), parse_mode="HTML")
            return

        if dl.is_photo_post(info):
            await m.edit_text(tx(u.id, "downloading"))
            photos = await dl.download_photos(url)
            if not photos:
                await m.edit_text(tx(u.id, "err_url"), parse_mode="HTML")
                return
            cap = tx(u.id, "done_cap", bot=BOT_USERNAME)
            try:
                await m.delete()
            except Exception:
                pass
            if len(photos) == 1:
                with open(photos[0], "rb") as fh:
                    await ctx.bot.send_photo(update.message.chat_id, fh, caption=cap, **send_kw)
            else:
                from telegram import InputMediaPhoto
                media = []
                for i, p in enumerate(photos):
                    with open(p, "rb") as fh:
                        media.append(InputMediaPhoto(fh, caption=cap if i == 0 else None))
                await ctx.bot.send_media_group(update.message.chat_id, media, **send_kw)
            for p in photos:
                try:
                    os.remove(p)
                except Exception:
                    pass
            db_inc_dl(u.id)
            return

        fmts = [f for f in info.get("formats", []) if f.get("height")]
        best_video = max(fmts, key=lambda x: x.get("height", 0), default=None)
        fmt_id = best_video["format_id"] if best_video else "best"
        width = info.get("width", 1280)
        cap = tx(u.id, "done_cap", bot=BOT_USERNAME)
        chat_id = update.message.chat_id

        await m.edit_text("⬇️ Загружаю видео...")
        video_file = await dl.download(url, fmt_id)

        await m.edit_text("⬇️ Загружаю аудио...")
        audio_file = await dl.download(url, "bestaudio")

        await m.edit_text("📤 Отправляю...")

        if video_file:
            final_video = video_file
            size_mb = os.path.getsize(final_video) / (1024 * 1024)
            if size_mb <= 49:
                try:
                    with open(final_video, "rb") as fh:
                        await ctx.bot.send_video(chat_id, fh, caption=cap, supports_streaming=True, **send_kw)
                except Exception as e:
                    logger.warning(f"Group video send error: {e}")
            for p in {video_file, final_video}:
                try:
                    if os.path.exists(p): os.remove(p)
                except Exception:
                    pass

        if audio_file:
            try:
                with open(audio_file, "rb") as fh:
                    await ctx.bot.send_audio(chat_id, fh, caption=cap, **send_kw)
            except Exception as e:
                logger.warning(f"Group audio send error: {e}")
            try:
                if os.path.exists(audio_file): os.remove(audio_file)
            except Exception:
                pass

        try:
            await m.delete()
        except Exception:
            pass

        db_inc_dl(u.id)
        return

    m = await update.message.reply_text(tx(u.id, "analyzing"))
    info = await asyncio.to_thread(dl.get_info, url)

    if not info:
        await m.edit_text(tx(u.id, "err_url"), parse_mode="HTML")
        return

    if dl.is_photo_post(info):
        await m.edit_text(tx(u.id, "downloading"))
        photos = await dl.download_photos(url)
        if not photos:
            await m.edit_text(tx(u.id, "err_url"), parse_mode="HTML")
            return
        cap = tx(u.id, "done_cap", bot=BOT_USERNAME)
        chat_id = update.message.chat_id
        send_kw = {"message_thread_id": thread_id} if thread_id else {}
        try:
            await m.delete()
        except Exception:
            pass
        if len(photos) == 1:
            with open(photos[0], "rb") as fh:
                await ctx.bot.send_photo(chat_id, fh, caption=cap, **send_kw)
        else:
            from telegram import InputMediaPhoto
            media = []
            for i, p in enumerate(photos):
                with open(p, "rb") as fh:
                    media.append(InputMediaPhoto(fh, caption=cap if i == 0 else None))
            await ctx.bot.send_media_group(chat_id, media, **send_kw)
        for p in photos:
            try:
                os.remove(p)
            except Exception:
                pass
        db_inc_dl(u.id)
        return

    user_row = db_get(u.id)

    if user_row and user_row["auto_dl"]:
        fmts = [f for f in info.get("formats", []) if f.get("height")]
        best = max(fmts, key=lambda x: x.get("height", 0), default=None)
        fmt_id = best["format_id"] if best else "best"
        await m.edit_text(tx(u.id, "downloading"))
        await do_download(ctx, m, url, fmt_id, info.get("width", 1280), u.id, update.message.chat_id, thread_id)
        return

    formats = []
    seen = set()
    for f in sorted(info.get("formats", []), key=lambda x: x.get("height", 0) or 0, reverse=True):
        h = f.get("height")
        if h and h not in seen and h >= 360:
            formats.append(_btn(f"🎬 {h}p", f"v_{m.message_id}_{f['format_id']}"))
            seen.add(h)
        if len(formats) >= 4:
            break

    title = (info.get("title") or "")[:50]
    kb_rows = [[f] for f in formats]
    kb_rows.append([_btn(tx(u.id, "audio_only"), f"v_{m.message_id}_bestaudio")])

    ctx.bot_data[f"dl_{m.message_id}"] = {
        "url": url,
        "width": info.get("width", 1280),
        "uid": u.id,
        "chat_id": update.message.chat_id,
        "thread_id": thread_id,
    }

    await m.edit_text(
        tx(u.id, "choose_q", title=title),
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb_rows),
    )


# ══════════════════════════════════════════════
#  CALLBACK HANDLER
# ══════════════════════════════════════════════
async def callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data
    uid = q.from_user.id
    chat_id = q.message.chat_id
    thread_id = q.message.message_thread_id

    if data == "noop":
        return

    # ── Search download ──
    if data.startswith("sdl_"):
        parts = data.split("_")
        target_uid = int(parts[1])
        idx = int(parts[2])

        if uid != target_uid:
            await q.answer("❌", show_alert=True)
            return

        search_key = f"search_{uid}"
        search_data = ctx.bot_data.get(search_key)
        if not search_data:
            await q.answer("Сессия истекла. Запусти поиск заново." if get_lang(uid) == "ru" else "Session expired. Run search again.", show_alert=True)
            return

        results = search_data["results"]
        if idx >= len(results):
            return

        prem = is_premium(uid)
        limit = PREMIUM_SEARCH_DL_DAY if prem else FREE_SEARCH_DL_DAY
        used = db_get_search_dl_count(uid)

        if used >= limit:
            key = "search_limit_prem" if prem else "search_limit_free"
            await q.answer(tx(uid, key, limit=limit), show_alert=True)
            return

        video = results[idx]
        msg = await ctx.bot.send_message(
            chat_id, tx(uid, "downloading"),
            message_thread_id=search_data.get("thread_id"),
        )

        file = await dl.download(video["url"], "best")
        if not file:
            await msg.edit_text(tx(uid, "err_dl", err="not found"), parse_mode="HTML")
            return

        size_mb = os.path.getsize(file) / (1024 * 1024)
        if size_mb > 49:
            await msg.edit_text(f"⚠️ Файл {size_mb:.0f} МБ — слишком большой для Telegram.")
            os.remove(file)
            return

        db_inc_search_dl(uid)
        db_inc_dl(uid)
        await msg.edit_text(tx(uid, "sending"))

        cap = tx(uid, "done_cap", bot=BOT_USERNAME)
        send_kw = {}
        if search_data.get("thread_id"):
            send_kw["message_thread_id"] = search_data["thread_id"]
        try:
            with open(file, "rb") as fh:
                await ctx.bot.send_video(chat_id, fh, caption=cap, supports_streaming=True, **send_kw)
        except Exception:
            with open(file, "rb") as fh:
                await ctx.bot.send_document(chat_id, fh, caption=cap, **send_kw)

        try:
            await msg.delete()
        except Exception:
            pass
        try:
            os.remove(file)
        except Exception:
            pass
        return

    # ── Video quality pick ──
    if data.startswith("v_"):
        parts = data.split("_", 2)
        msg_id = int(parts[1])
        fmt_id = parts[2]

        dl_data = ctx.bot_data.get(f"dl_{msg_id}")
        if not dl_data:
            await q.edit_message_text("⏰ Сессия истекла. Отправь ссылку заново.")
            return

        await q.edit_message_text(tx(uid, "downloading"))
        await do_download(ctx, q.message, dl_data["url"], fmt_id,
                          dl_data["width"], uid, chat_id, thread_id)
        return

    # ── Sub menu ──
    if data == "sub":
        user = db_get(uid)
        pu = user["premium_until"] if user else 0
        now = int(time.time())
        if pu == -1:
            status = tx(uid, "sub_lifetime")
        elif pu > now:
            status = tx(uid, "sub_active", date=datetime.fromtimestamp(pu).strftime("%d.%m.%Y"))
        else:
            status = tx(uid, "sub_none")
        await q.edit_message_text(
            tx(uid, "sub_menu", status=status),
            parse_mode="HTML",
            reply_markup=kb_sub(uid),
        )
        return

    if data.startswith("subm_"):
        _, action, cur_s = data.split("_")
        cur = int(cur_s)
        cur = max(1, cur - 1) if action == "dec" else min(MAX_MONTHS, cur + 1)
        await q.edit_message_reply_markup(reply_markup=kb_sub(uid, cur))
        return

    if data == "profile":
        user = db_get(uid)
        joined = (
            datetime.fromtimestamp(user["joined_at"]).strftime("%d.%m.%Y")
            if user and user["joined_at"] else "?"
        )
        lang = get_lang(uid)
        await q.edit_message_text(
            tx(uid, "profile",
               user_id=uid,
               name=(q.from_user.first_name or "—") + ("  👨‍💻" if uid in ADMIN_IDS else ""),
               status=premium_label(uid),
               dl=user["downloads"] if user else 0,
               stars=user["stars_spent"] if user else 0,
               joined=joined),
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([[
                _btn("👑 Premium", "sub"),
                _btn("⚙️ Настройки" if lang == "ru" else "⚙️ Settings", "settings"),
            ]]),
        )
        return

    if data == "settings":
        await q.edit_message_text(
            tx(uid, "settings"), parse_mode="HTML", reply_markup=kb_settings(uid)
        )
        return

    if data == "set_lang":
        new_lang = "en" if get_lang(uid) == "ru" else "ru"
        db_set(uid, "language", new_lang)
        await q.edit_message_text(
            tx(uid, "settings"), parse_mode="HTML", reply_markup=kb_settings(uid)
        )
        return

    if data == "set_auto":
        u = db_get(uid)
        db_set(uid, "auto_dl", 0 if (u and u["auto_dl"]) else 1)
        await q.edit_message_reply_markup(reply_markup=kb_settings(uid))
        return

    if data == "back_main":
        u_obj = q.from_user
        db_upsert(u_obj.id, u_obj.username or "", u_obj.first_name or "")
        await q.edit_message_text(
            tx(uid, "start", name=u_obj.first_name or "друг"),
            parse_mode="HTML",
            reply_markup=kb_main(uid),
        )
        return

    if data == "pay_trial":
        user = db_get(uid)
        if user and user["trial_used"]:
            await q.answer(tx(uid, "trial_used"), show_alert=True)
            return
        await ctx.bot.send_invoice(
            chat_id=uid,
            title=tx(uid, "inv_trial_t"),
            description=tx(uid, "inv_trial_d"),
            payload=json.dumps({"type": "trial", "uid": uid}),
            currency="XTR",
            prices=[LabeledPrice("Trial 7 days", TRIAL_STARS)],
        )
        return

    if data.startswith("pay_months_"):
        months = int(data.split("_")[2])
        stars = calc_price(months)
        lang = get_lang(uid)
        mw = mword(months, lang)
        await ctx.bot.send_invoice(
            chat_id=uid,
            title=tx(uid, "inv_month_t", n=months, mw=mw),
            description=tx(uid, "inv_month_d", n=months, mw=mw),
            payload=json.dumps({"type": "monthly", "uid": uid, "months": months}),
            currency="XTR",
            prices=[LabeledPrice(f"Premium {months}mo", stars)],
        )
        return

    if data == "pay_lifetime":
        await ctx.bot.send_invoice(
            chat_id=uid,
            title=tx(uid, "inv_life_t"),
            description=tx(uid, "inv_life_d"),
            payload=json.dumps({"type": "lifetime", "uid": uid}),
            currency="XTR",
            prices=[LabeledPrice("Premium Lifetime", LIFETIME_STARS)],
        )
        return

    if data == "gift_start":
        ctx.user_data["awaiting_gift_target"] = True
        await q.edit_message_text(tx(uid, "gift_ask"), parse_mode="HTML")
        return

    if data.startswith("gsubm_"):
        parts = data.split("_")
        action, cur, to_uid = parts[1], int(parts[2]), int(parts[3])
        cur = max(1, cur - 1) if action == "dec" else min(MAX_MONTHS, cur + 1)
        await q.edit_message_reply_markup(reply_markup=kb_gift_select(uid, to_uid, cur))
        return

    if data.startswith("gpay_trial_"):
        to_uid = int(data.split("_")[2])
        to_u = db_get(to_uid)
        to_name = (to_u["username"] or to_u["first_name"] or str(to_uid)) if to_u else str(to_uid)
        await ctx.bot.send_invoice(
            chat_id=uid,
            title=tx(uid, "inv_gtrial_t", to=to_name),
            description=tx(uid, "inv_gift_d"),
            payload=json.dumps({"type": "gift_trial", "uid": uid, "to_uid": to_uid}),
            currency="XTR",
            prices=[LabeledPrice(f"Gift Trial→{to_name}", TRIAL_STARS)],
        )
        return

    if data.startswith("gpay_months_"):
        parts = data.split("_")
        months, to_uid = int(parts[2]), int(parts[3])
        stars = calc_price(months)
        lang = get_lang(uid)
        mw = mword(months, lang)
        to_u = db_get(to_uid)
        to_name = (to_u["username"] or to_u["first_name"] or str(to_uid)) if to_u else str(to_uid)
        await ctx.bot.send_invoice(
            chat_id=uid,
            title=tx(uid, "inv_gmonth_t", n=months, mw=mw, to=to_name),
            description=tx(uid, "inv_gift_d"),
            payload=json.dumps({"type": "gift_monthly", "uid": uid, "to_uid": to_uid, "months": months}),
            currency="XTR",
            prices=[LabeledPrice(f"Gift {months}mo→{to_name}", stars)],
        )
        return

    if data.startswith("gpay_life_"):
        to_uid = int(data.split("_")[2])
        to_u = db_get(to_uid)
        to_name = (to_u["username"] or to_u["first_name"] or str(to_uid)) if to_u else str(to_uid)
        await ctx.bot.send_invoice(
            chat_id=uid,
            title=tx(uid, "inv_glife_t", to=to_name),
            description=tx(uid, "inv_gift_d"),
            payload=json.dumps({"type": "gift_lifetime", "uid": uid, "to_uid": to_uid}),
            currency="XTR",
            prices=[LabeledPrice(f"Gift Lifetime→{to_name}", LIFETIME_STARS)],
        )
        return

    if data == "admin_stats" and uid in ADMIN_IDS:
        s = db_stats()
        await q.edit_message_text(
            tx(uid, "stats", **s), parse_mode="HTML", reply_markup=kb_admin(get_lang(uid))
        )
        return

    if data == "admin_bc" and uid in ADMIN_IDS:
        ctx.user_data["awaiting_broadcast"] = True
        await q.edit_message_text(tx(uid, "bc_ask"), parse_mode="HTML")
        return


# ══════════════════════════════════════════════
#  PAYMENT HANDLERS
# ══════════════════════════════════════════════
async def pre_checkout(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    payment = update.message.successful_payment
    uid = update.effective_user.id
    stars = payment.total_amount
    payload = json.loads(payment.invoice_payload)
    p_type = payload["type"]
    lang = get_lang(uid)

    db_add_stars(uid, stars)
    db_log_tx(uid, stars, p_type, payload.get("months", 0), payment.invoice_payload)

    from_name = update.effective_user.first_name or "Аноним"

    if p_type == "trial":
        db_add_premium(uid, TRIAL_DAYS)
        db_mark_trial(uid)
        await update.message.reply_text(tx(uid, "paid_trial"), parse_mode="HTML")

    elif p_type == "monthly":
        months = payload["months"]
        mw = mword(months, lang)
        db_add_premium(uid, months * 30)
        await update.message.reply_text(tx(uid, "paid_month", n=months, mw=mw), parse_mode="HTML")

    elif p_type == "lifetime":
        db_add_premium(uid, -1)
        await update.message.reply_text(tx(uid, "paid_life"), parse_mode="HTML")

    elif p_type == "gift_trial":
        to_uid = payload["to_uid"]
        db_add_premium(to_uid, TRIAL_DAYS)
        to_u = db_get(to_uid)
        to_name = (to_u["username"] or to_u["first_name"] or str(to_uid)) if to_u else str(to_uid)
        period = f"7 {'дней' if lang == 'ru' else 'days'}"
        await update.message.reply_text(tx(uid, "gift_ok", to=to_name), parse_mode="HTML")
        try:
            await ctx.bot.send_message(to_uid, tx(to_uid, "gift_recv", from_name=from_name, period=period), parse_mode="HTML")
        except Exception:
            pass

    elif p_type == "gift_monthly":
        to_uid = payload["to_uid"]
        months = payload["months"]
        mw = mword(months, lang)
        db_add_premium(to_uid, months * 30)
        to_u = db_get(to_uid)
        to_name = (to_u["username"] or to_u["first_name"] or str(to_uid)) if to_u else str(to_uid)
        await update.message.reply_text(tx(uid, "gift_ok", to=to_name), parse_mode="HTML")
        try:
            await ctx.bot.send_message(to_uid, tx(to_uid, "gift_recv", from_name=from_name, period=f"{months} {mw}"), parse_mode="HTML")
        except Exception:
            pass

    elif p_type == "gift_lifetime":
        to_uid = payload["to_uid"]
        db_add_premium(to_uid, -1)
        to_u = db_get(to_uid)
        to_name = (to_u["username"] or to_u["first_name"] or str(to_uid)) if to_u else str(to_uid)
        await update.message.reply_text(tx(uid, "gift_ok", to=to_name), parse_mode="HTML")
        try:
            await ctx.bot.send_message(to_uid, tx(to_uid, "gift_recv", from_name=from_name, period="♾️ навсегда / forever"), parse_mode="HTML")
        except Exception:
            pass


# ══════════════════════════════════════════════
#  WEBAPP PROCESS MANAGER
# ══════════════════════════════════════════════
import threading, sys, pathlib

_webapp_thread = None
_webapp_server = None
_webapp_lock   = _threading.RLock()
WEBAPP_FILE    = pathlib.Path(__file__).parent / "webapp.py"

def webapp_start() -> str:
    global _webapp_thread, _webapp_server
    with _webapp_lock:
        if _webapp_thread and _webapp_thread.is_alive():
            return "⚠️ Сайт уже запущен."
        if not WEBAPP_FILE.exists():
            return "❌ webapp.py не найден."
        try:
            import importlib.util as _ilu
            spec = _ilu.spec_from_file_location("webapp", str(WEBAPP_FILE))
            mod  = _ilu.module_from_spec(spec)
            spec.loader.exec_module(mod)

            port = int(os.getenv("WEBAPP_PORT", "80"))
            mod.cleanup_old_files()
            server = mod.ThreadedHTTPServer(("0.0.0.0", port), mod.Handler)
            _webapp_server = server

            def _run():
                logger.info("🌐 webapp started on port %d", port)
                server.serve_forever()

            _webapp_thread = threading.Thread(target=_run, daemon=True)
            _webapp_thread.start()
            return f"✅ Сайт запущен на порту {port}"
        except Exception as e:
            return f"❌ Ошибка запуска: {e}"

def webapp_stop() -> str:
    global _webapp_server, _webapp_thread
    with _webapp_lock:
        srv = _webapp_server
    if not srv:
        return "⚠️ Сайт не запущен."
    try:
        srv.shutdown()
        _webapp_server = None
        _webapp_thread = None
        logger.info("🛑 webapp stopped")
        return "🛑 Сайт остановлен."
    except Exception as e:
        return f"❌ Ошибка остановки: {e}"

def webapp_restart() -> str:
    webapp_stop()
    time.sleep(1)
    return webapp_start()

def webapp_status() -> str:
    with _webapp_lock:
        t = _webapp_thread
        srv = _webapp_server
    if t and t.is_alive() and srv:
        return "🟢 Запущен"
    return "⚫ Не запущен"

def owner_only(uid: int) -> bool:
    return uid in ADMIN_IDS

async def cmd_start_site(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update.effective_user.id):
        await update.message.reply_text("⛔ Только владелец может запускать сайт.")
        return
    await update.message.reply_text(webapp_start())

async def cmd_stop_site(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update.effective_user.id):
        await update.message.reply_text("⛔ Только владелец может останавливать сайт.")
        return
    await update.message.reply_text(webapp_stop())

async def cmd_restart_site(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update.effective_user.id):
        await update.message.reply_text("⛔ Только владелец может перезапускать сайт.")
        return
    await update.message.reply_text("🔄 Перезапускаю...")
    await update.message.reply_text(webapp_restart())

async def cmd_site_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not owner_only(update.effective_user.id):
        await update.message.reply_text("⛔ Нет доступа.")
        return
    await update.message.reply_text(f"🖥 Статус сайта: {webapp_status()}")


# ══════════════════════════════════════════════
#  STARTUP & MAIN
# ══════════════════════════════════════════════
async def post_init(app: Application):
    await app.bot.delete_my_commands()
    await app.bot.set_my_commands([
        BotCommand("start",     "🎬 Главное меню"),
        BotCommand("search",    "🔍 Поиск YouTube/TikTok"),
        BotCommand("sub",       "👑 Premium подписка"),
        BotCommand("profile",   "👤 Мой профиль"),
        BotCommand("settings",  "⚙️ Настройки"),
        BotCommand("ticket",    "🎫 Поддержка (Premium)"),
        BotCommand("help",      "📋 Справка"),
    ])
    logger.info("Commands registered")


def main():
    db_init()
    os.makedirs(DOWNLOADS_DIR, exist_ok=True)

    app = Application.builder().token(BOT_TOKEN).post_init(post_init).build()

    app.add_handler(CommandHandler("start",        cmd_start))
    app.add_handler(CommandHandler("help",         cmd_help))
    app.add_handler(CommandHandler("search",       cmd_search))
    app.add_handler(CommandHandler("sub",          cmd_sub))
    app.add_handler(CommandHandler("profile",      cmd_profile))
    app.add_handler(CommandHandler("settings",     cmd_settings))
    app.add_handler(CommandHandler("ticket",       cmd_ticket))
    app.add_handler(CommandHandler("stats",        cmd_stats))
    app.add_handler(CommandHandler("broadcast",    cmd_broadcast))
    app.add_handler(CommandHandler("admin",        cmd_admin))
    app.add_handler(CommandHandler("start_site",   cmd_start_site))
    app.add_handler(CommandHandler("stop_site",    cmd_stop_site))
    app.add_handler(CommandHandler("restart_site", cmd_restart_site))
    app.add_handler(CommandHandler("site_status",  cmd_site_status))

    app.add_handler(PreCheckoutQueryHandler(pre_checkout))
    app.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment))
    app.add_handler(CallbackQueryHandler(callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("🤖 PuweDownloaderBot started!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
