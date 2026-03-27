#!/usr/bin/env python3
"""
PuweDownloader — Mini App Backend (Flask)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Запуск: python3 webapp.py
Порт:  5000 (измени PORT ниже)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Endpoints:
  POST /api/auth/telegram   — авторизация через Telegram initData
  GET  /api/auth/me         — проверка токена
  POST /api/info            — получить инфо о видео
  POST /api/download        — скачать и отдать ссылку
  POST /api/search          — поиск YouTube/TikTok
  POST /api/search-download — скачать из поиска
  GET  /api/limits          — лимиты пользователя
  DELETE /api/delete/<id>   — удалить файл с сервера
"""

import os, hashlib, hmac, time, json, uuid, glob, asyncio, logging, threading
from dotenv import load_dotenv
load_dotenv()
from datetime import date, datetime
from functools import wraps
from urllib.parse import parse_qsl

from flask import Flask, request, jsonify, send_from_directory, abort
import yt_dlp
import sqlite3

# ══════════════════════════════════
#  CONFIG
# ══════════════════════════════════
BOT_TOKEN         = os.getenv("BOT_TOKEN", "")
BOT_DB            = "bot.db"          # путь к основной БД бота
DOWNLOADS_DIR     = "webapp_dl"       # папка для временных файлов
SECRET_SALT       = "puwe_webapp_v1"  # для генерации токенов
PORT              = 80
FREE_DL_DAY       = 3
PREMIUM_DL_DAY    = 12
FILE_TTL_SEC      = 120               # через сколько секунд авто-удалять файл

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

app = Flask(__name__, static_folder="static")
os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# ══════════════════════════════════
#  DATABASE (shared with bot)
# ══════════════════════════════════
def _conn():
    c = sqlite3.connect(BOT_DB, check_same_thread=False)
    c.row_factory = sqlite3.Row
    return c

def db_get_user(uid: int):
    with _conn() as c:
        return c.execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def db_upsert_user(uid: int, username: str, first_name: str):
    now = int(time.time())
    with _conn() as c:
        if c.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone():
            c.execute("UPDATE users SET username=?,first_name=?,last_seen=? WHERE user_id=?",
                      (username, first_name, now, uid))
        else:
            c.execute("INSERT INTO users (user_id,username,first_name,joined_at,last_seen) VALUES (?,?,?,?,?)",
                      (uid, username, first_name, now, now))
        c.commit()

def is_premium(uid: int) -> bool:
    u = db_get_user(uid)
    if not u: return False
    pu = u["premium_until"]
    return pu == -1 or pu > int(time.time())

def db_get_search_dl(uid: int) -> int:
    today = date.today().isoformat()
    with _conn() as c:
        row = c.execute(
            "SELECT count FROM search_downloads WHERE user_id=? AND date_str=?",
            (uid, today)
        ).fetchone()
        return row[0] if row else 0

def db_inc_search_dl(uid: int):
    today = date.today().isoformat()
    with _conn() as c:
        c.execute(
            "INSERT INTO search_downloads (user_id,date_str,count) VALUES (?,?,1) "
            "ON CONFLICT(user_id,date_str) DO UPDATE SET count=count+1",
            (uid, today)
        )
        c.commit()

# ══════════════════════════════════
#  TOKENS
# ══════════════════════════════════
def make_token(uid: int) -> str:
    raw = f"{uid}:{SECRET_SALT}:{int(time.time()//86400)}"
    return hashlib.sha256(raw.encode()).hexdigest()

def verify_token(token: str) -> int | None:
    """Returns user_id if valid, else None"""
    # We scan users — in production use a tokens table
    with _conn() as c:
        rows = c.execute("SELECT user_id FROM users").fetchall()
    for row in rows:
        uid = row[0]
        for day_offset in range(3):  # valid for 3 days
            day = int(time.time()//86400) - day_offset
            raw = f"{uid}:{SECRET_SALT}:{day}"
            expected = hashlib.sha256(raw.encode()).hexdigest()
            if hmac.compare_digest(expected, token):
                return uid
    return None

# ══════════════════════════════════
#  TELEGRAM INIT DATA VERIFICATION
# ══════════════════════════════════
def verify_telegram_init_data(init_data: str) -> dict | None:
    """Verifies Telegram Mini App initData and returns user dict"""
    try:
        params = dict(parse_qsl(init_data, keep_blank_values=True))
        check_hash = params.pop("hash", "")
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed, check_hash):
            return None
        # Check freshness (5 min)
        auth_date = int(params.get("auth_date", 0))
        if time.time() - auth_date > 300:
            return None
        return json.loads(params.get("user", "{}"))
    except Exception as e:
        log.warning(f"initData verify error: {e}")
        return None

# ══════════════════════════════════
#  AUTH DECORATOR
# ══════════════════════════════════
def require_auth(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        token = request.headers.get("X-Token", "")
        uid = verify_token(token)
        if not uid:
            return jsonify(ok=False, error="Unauthorized"), 401
        request.uid = uid
        return f(*args, **kwargs)
    return wrapper

# ══════════════════════════════════
#  CORS (для dev)
# ══════════════════════════════════
@app.after_request
def add_cors(response):
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type, X-Token"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,DELETE,OPTIONS"
    return response

@app.route("/api/<path:path>", methods=["OPTIONS"])
def options_handler(path):
    return "", 204

# ══════════════════════════════════
#  AUTH ENDPOINTS
# ══════════════════════════════════
@app.route("/api/auth/telegram", methods=["POST"])
def auth_telegram():
    data = request.json or {}
    init_data = data.get("init_data", "")
    tg_user = verify_telegram_init_data(init_data)
    if not tg_user:
        return jsonify(ok=False, error="Invalid Telegram data"), 401

    uid = tg_user["id"]
    db_upsert_user(uid, tg_user.get("username", ""), tg_user.get("first_name", ""))
    token = make_token(uid)
    user_row = db_get_user(uid)
    prem = is_premium(uid)

    return jsonify(ok=True, token=token, user={
        "id": uid,
        "first_name": tg_user.get("first_name", ""),
        "username": tg_user.get("username", ""),
        "is_premium": prem,
    })

@app.route("/api/auth/me", methods=["GET"])
@require_auth
def auth_me():
    uid = request.uid
    u = db_get_user(uid)
    if not u:
        return jsonify(ok=False, error="User not found"), 404
    return jsonify(ok=True, user={
        "id": uid,
        "first_name": u["first_name"],
        "username": u["username"],
        "is_premium": is_premium(uid),
    })

# ══════════════════════════════════
#  VIDEO INFO
# ══════════════════════════════════
@app.route("/api/info", methods=["POST"])
@require_auth
def get_info():
    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify(ok=False, error="No URL"), 400

    opts = {"quiet": True, "no_warnings": True, "check_formats": False}
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200])

    if not info:
        return jsonify(ok=False, error="Не удалось получить информацию")

    # Duration string
    dur = info.get("duration") or 0
    if dur:
        m, s = divmod(int(dur), 60)
        h, m = divmod(m, 60)
        dur_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
    else:
        dur_str = ""

    # Formats
    formats_out = []
    seen = set()
    for f in sorted(info.get("formats", []), key=lambda x: x.get("height", 0) or 0, reverse=True):
        h = f.get("height")
        if not h or h in seen or h < 144:
            continue
        seen.add(h)
        formats_out.append({
            "format_id": f["format_id"],
            "height": h,
            "format_note": f.get("format_note", ""),
            "filesize": f.get("filesize"),
            "url": f.get("url"),  # прямой URL для быстрого скачивания
        })
        if len(formats_out) >= 6:
            break

    # Preview URL — берём самый низкий формат с видео
    preview_url = None
    for f in reversed(formats_out):
        if f.get("url"):
            preview_url = f["url"]
            break

    return jsonify(
        ok=True,
        url=url,
        title=info.get("title", "")[:200],
        thumbnail=info.get("thumbnail"),
        duration=dur,
        duration_str=dur_str,
        view_count=info.get("view_count"),
        extractor=info.get("extractor_key", info.get("extractor", "")),
        formats=formats_out,
        preview_url=preview_url,
    )

# ══════════════════════════════════
#  DOWNLOAD
# ══════════════════════════════════
@app.route("/api/download", methods=["POST"])
@require_auth
def do_download():
    body = request.json or {}
    url = body.get("url", "").strip()
    fmt_id = body.get("format_id", "best")
    mode = body.get("mode", "video")

    if not url:
        return jsonify(ok=False, error="No URL"), 400

    file_id = str(uuid.uuid4())[:8]
    out_tpl = os.path.join(DOWNLOADS_DIR, f"{file_id}.%(ext)s")

    opts = {
        "outtmpl": out_tpl,
        "quiet": True,
        "no_warnings": True,
        "ignoreerrors": False,
        "fragment_retries": 3,
        "retries": 3,
    }

    if mode == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3"}]
        ext = "mp3"
    else:
        opts["format"] = f"{fmt_id}+bestaudio/best" if fmt_id != "best" else "bestvideo[ext=mp4]+bestaudio/best[ext=mp4]/best"
        opts["merge_output_format"] = "mp4"
        ext = "mp4"

    try:
        yt_dlp.YoutubeDL(opts).download([url])
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200])

    # Find the file
    files = glob.glob(os.path.join(DOWNLOADS_DIR, f"{file_id}.*"))
    files = [f for f in files if not f.endswith(".part") and os.path.getsize(f) > 1024]
    if not files:
        return jsonify(ok=False, error="Файл не найден после загрузки")

    filepath = sorted(files)[-1]
    filename = os.path.basename(filepath)
    size = os.path.getsize(filepath)

    if size > 50 * 1024 * 1024:
        os.remove(filepath)
        return jsonify(ok=False, error=f"Файл слишком большой ({size//1024//1024} МБ). Выбери качество пониже.")

    # Schedule auto-delete
    schedule_delete(filepath, FILE_TTL_SEC)

    return jsonify(
        ok=True,
        file_id=file_id,
        filename=filename,
        size=size,
        download_url=f"/api/file/{filename}",
    )

@app.route("/api/file/<filename>")
def serve_file(filename):
    """Serve downloaded file"""
    # Security: only files from DOWNLOADS_DIR
    safe = os.path.basename(filename)
    path = os.path.join(DOWNLOADS_DIR, safe)
    if not os.path.exists(path):
        abort(404)
    return send_from_directory(DOWNLOADS_DIR, safe, as_attachment=True)

@app.route("/api/delete/<file_id>", methods=["DELETE"])
@require_auth
def delete_file(file_id):
    safe_id = file_id.replace("/", "").replace("..", "")[:16]
    files = glob.glob(os.path.join(DOWNLOADS_DIR, f"{safe_id}.*"))
    for f in files:
        try:
            os.remove(f)
            log.info(f"Deleted {f}")
        except Exception:
            pass
    return jsonify(ok=True)

# ══════════════════════════════════
#  SEARCH
# ══════════════════════════════════
@app.route("/api/search", methods=["POST"])
@require_auth
def do_search():
    body = request.json or {}
    query = body.get("query", "").strip()
    platform = body.get("platform", "yt")
    if not query:
        return jsonify(ok=False, error="No query"), 400

    if platform == "yt":
        search_url = f"ytsearch5:{query}"
    else:
        search_url = f"tiktoksearch5:{query}"

    opts = {"quiet": True, "no_warnings": True, "extract_flat": True, "skip_download": True}
    results = []
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(search_url, download=False)
            for e in (info.get("entries") or []):
                if not e: continue
                url = e.get("url") or e.get("webpage_url")
                if not url:
                    eid = e.get("id")
                    if platform == "yt" and eid:
                        url = f"https://www.youtube.com/watch?v={eid}"
                    elif eid:
                        url = f"https://www.tiktok.com/@{e.get('uploader','user')}/video/{eid}"
                if not url: continue

                dur = e.get("duration") or 0
                dur_str = ""
                if dur:
                    m, s = divmod(int(dur), 60)
                    h, m = divmod(m, 60)
                    dur_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"

                vc = e.get("view_count") or 0
                vs = ""
                if vc >= 1e6: vs = f"{vc/1e6:.1f}M 👁"
                elif vc >= 1e3: vs = f"{vc//1000}K 👁"
                elif vc: vs = f"{vc} 👁"

                results.append({
                    "title": (e.get("title") or "Unknown")[:80],
                    "url": url,
                    "thumbnail": e.get("thumbnail"),
                    "duration": dur_str,
                    "views": vs,
                })
    except Exception as ex:
        log.warning(f"Search error: {ex}")

    uid = request.uid
    used = db_get_search_dl(uid)
    limit = PREMIUM_DL_DAY if is_premium(uid) else FREE_DL_DAY

    return jsonify(ok=True, results=results[:5], used=used, limit=limit)

@app.route("/api/search-download", methods=["POST"])
@require_auth
def search_download():
    uid = request.uid
    prem = is_premium(uid)
    limit = PREMIUM_DL_DAY if prem else FREE_DL_DAY
    used = db_get_search_dl(uid)

    if used >= limit:
        return jsonify(ok=False, error=f"Лимит {limit}/день исчерпан"), 429

    url = (request.json or {}).get("url", "").strip()
    if not url:
        return jsonify(ok=False, error="No URL"), 400

    file_id = str(uuid.uuid4())[:8]
    out_tpl = os.path.join(DOWNLOADS_DIR, f"{file_id}.%(ext)s")
    opts = {
        "outtmpl": out_tpl,
        "quiet": True,
        "no_warnings": True,
        "format": "bestvideo[ext=mp4][filesize<45M]+bestaudio[ext=m4a]/best[ext=mp4][filesize<45M]/best[filesize<45M]/best",
        "merge_output_format": "mp4",
        "fragment_retries": 3,
        "retries": 3,
    }

    try:
        yt_dlp.YoutubeDL(opts).download([url])
    except Exception as e:
        return jsonify(ok=False, error=str(e)[:200])

    files = glob.glob(os.path.join(DOWNLOADS_DIR, f"{file_id}.*"))
    files = [f for f in files if not f.endswith(".part") and os.path.getsize(f) > 1024]
    if not files:
        return jsonify(ok=False, error="Файл не найден")

    filepath = sorted(files)[-1]
    filename = os.path.basename(filepath)

    db_inc_search_dl(uid)
    used += 1
    schedule_delete(filepath, FILE_TTL_SEC)

    return jsonify(ok=True, file_id=file_id, filename=filename,
                   download_url=f"/api/file/{filename}",
                   used=used, limit=limit)

# ══════════════════════════════════
#  LIMITS
# ══════════════════════════════════
@app.route("/api/limits", methods=["GET"])
@require_auth
def get_limits():
    uid = request.uid
    prem = is_premium(uid)
    limit = PREMIUM_DL_DAY if prem else FREE_DL_DAY
    used = db_get_search_dl(uid)
    return jsonify(ok=True, used=used, limit=limit, is_premium=prem)

# ══════════════════════════════════
#  SERVE MINIAPP HTML
# ══════════════════════════════════
@app.route("/")
def index():
    return send_from_directory(".", "miniapp.html")

# ══════════════════════════════════
#  AUTO-DELETE HELPER
# ══════════════════════════════════
def schedule_delete(path: str, delay: int):
    def _del():
        time.sleep(delay)
        try:
            if os.path.exists(path):
                os.remove(path)
                log.info(f"Auto-deleted: {path}")
        except Exception:
            pass
    t = threading.Thread(target=_del, daemon=True)
    t.start()

# ══════════════════════════════════
#  STARTUP: cleanup old files
# ══════════════════════════════════
def cleanup_old_files():
    now = time.time()
    for f in glob.glob(os.path.join(DOWNLOADS_DIR, "*")):
        if now - os.path.getmtime(f) > 3600:  # старше 1 часа
            try:
                os.remove(f)
            except Exception:
                pass

if __name__ == "__main__":
    cleanup_old_files()
    log.info(f"🌐 Mini App backend running on http://0.0.0.0:{PORT}")
    log.info(f"📁 Files served from /{DOWNLOADS_DIR}/")
    app.run(host="0.0.0.0", port=PORT, debug=False, threaded=True)
