#!/usr/bin/env python3
"""
PuweDownloader — Mini App Backend (оптимизированная версия)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Endpoints:
  POST /api/auth/telegram   — авторизация через Telegram initData
  GET  /api/auth/me         — проверка токена
  POST /api/info            — получить инфо о видео
  POST /api/download        — скачать и отдать ссылку
  POST /api/search          — поиск YouTube/TikTok
  POST /api/search-download — скачать из поиска
  GET  /api/limits          — лимиты пользователя
  DELETE /api/delete/<id>   — удалить файл с сервера
  GET  /api/file/<n>        — скачать файл (стриминг)
  GET  /                    — miniapp.html
"""

import os, hashlib, hmac, time, json, uuid, glob, logging, threading, sqlite3, mimetypes
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qsl
from pathlib import Path
from datetime import date
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════
#  CONFIG
# ══════════════════════════════════
BASE_DIR       = Path(__file__).parent
BOT_TOKEN      = os.getenv("BOT_TOKEN", "")
BOT_DB         = str(BASE_DIR / "bot.db")
DOWNLOADS_DIR  = str(BASE_DIR / "webapp_dl")
SECRET_SALT    = "puwe_webapp_v1"
PORT           = int(os.getenv("WEBAPP_PORT", "80"))
FREE_DL_DAY    = 3
PREMIUM_DL_DAY = 12
FILE_TTL_SEC   = 120
# Размер чанка при стриминге файла клиенту (2 МБ)
STREAM_CHUNK   = 2 * 1024 * 1024

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger("webapp")

os.makedirs(DOWNLOADS_DIR, exist_ok=True)

# ── Login tokens ──
import secrets as _secrets

LOGIN_TOKENS_FILE = BASE_DIR / "login_tokens.json"
_LT_LOCK = threading.Lock()

def lt_load():
    if not LOGIN_TOKENS_FILE.exists(): return {}
    try: return json.loads(LOGIN_TOKENS_FILE.read_text("utf-8"))
    except: return {}

def lt_save(data):
    LOGIN_TOKENS_FILE.write_text(json.dumps(data, ensure_ascii=False), "utf-8")

def lt_consume(token: str):
    with _LT_LOCK:
        data = lt_load()
        entry = data.pop(token, None)
        if entry:
            lt_save(data)
    if not entry: return None
    if entry["expires"] < time.time(): return None
    return entry["uid"]

import yt_dlp

# ══════════════════════════════════
#  DATABASE
# ══════════════════════════════════

# Один глобальный коннект в WAL-режиме — быстрее чем открывать каждый раз
_DB_CONN = None
_DB_LOCK = threading.Lock()

def _conn():
    global _DB_CONN
    if _DB_CONN is None:
        c = sqlite3.connect(BOT_DB, check_same_thread=False)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        c.execute("PRAGMA synchronous=NORMAL")
        c.execute("PRAGMA cache_size=10000")
        _DB_CONN = c
    return _DB_CONN

def db_get_user(uid):
    with _DB_LOCK:
        return _conn().execute("SELECT * FROM users WHERE user_id=?", (uid,)).fetchone()

def db_upsert_user(uid, username, first_name):
    now = int(time.time())
    with _DB_LOCK:
        c = _conn()
        if c.execute("SELECT 1 FROM users WHERE user_id=?", (uid,)).fetchone():
            c.execute("UPDATE users SET username=?,first_name=?,last_seen=? WHERE user_id=?",
                      (username, first_name, now, uid))
        else:
            c.execute("INSERT INTO users (user_id,username,first_name,joined_at,last_seen) VALUES (?,?,?,?,?)",
                      (uid, username, first_name, now, now))
        c.commit()

def is_premium(uid):
    u = db_get_user(uid)
    if not u: return False
    pu = u["premium_until"]
    return pu == -1 or pu > int(time.time())

def db_get_search_dl(uid):
    today = date.today().isoformat()
    with _DB_LOCK:
        row = _conn().execute(
            "SELECT count FROM search_downloads WHERE user_id=? AND date_str=?",
            (uid, today)).fetchone()
        return row[0] if row else 0

def db_inc_search_dl(uid):
    today = date.today().isoformat()
    with _DB_LOCK:
        c = _conn()
        c.execute(
            "INSERT INTO search_downloads (user_id,date_str,count) VALUES (?,?,1) "
            "ON CONFLICT(user_id,date_str) DO UPDATE SET count=count+1",
            (uid, today))
        c.commit()

# ══════════════════════════════════
#  TOKENS
# ══════════════════════════════════
def make_token(uid):
    raw = f"{uid}:{SECRET_SALT}:{int(time.time()//86400)}"
    return hashlib.sha256(raw.encode()).hexdigest()

def verify_token(token):
    if not token: return None
    with _DB_LOCK:
        rows = _conn().execute("SELECT user_id FROM users").fetchall()
    for row in rows:
        uid = row[0]
        for offset in range(3):
            day = int(time.time()//86400) - offset
            raw = f"{uid}:{SECRET_SALT}:{day}"
            expected = hashlib.sha256(raw.encode()).hexdigest()
            if hmac.compare_digest(expected, token):
                return uid
    return None

# ══════════════════════════════════
#  TELEGRAM INIT DATA
# ══════════════════════════════════
def verify_telegram_init_data(init_data):
    try:
        params = dict(parse_qsl(init_data, keep_blank_values=True))
        check_hash = params.pop("hash", "")
        data_check = "\n".join(f"{k}={v}" for k, v in sorted(params.items()))
        secret = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        computed = hmac.new(secret, data_check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(computed, check_hash): return None
        if time.time() - int(params.get("auth_date", 0)) > 300: return None
        return json.loads(params.get("user", "{}"))
    except Exception as e:
        log.warning("initData verify error: %s", e)
        return None

# ══════════════════════════════════
#  AUTO-DELETE
# ══════════════════════════════════
def schedule_delete(path, delay):
    def _del():
        time.sleep(delay)
        try:
            if os.path.exists(path):
                os.remove(path)
                log.info("Auto-deleted: %s", path)
        except Exception: pass
    threading.Thread(target=_del, daemon=True).start()

def cleanup_old_files():
    now = time.time()
    for f in glob.glob(os.path.join(DOWNLOADS_DIR, "*")):
        if now - os.path.getmtime(f) > 3600:
            try: os.remove(f)
            except Exception: pass

# ══════════════════════════════════
#  YT-DLP ОПЦИИ (оптимизированные)
# ══════════════════════════════════

# Формат без filesize фильтра — убирает 2-3 секунды на проверку размера
VIDEO_FORMAT = (
    "best[vcodec!=none][acodec!=none][ext=mp4]"
    "/best[vcodec!=none][acodec!=none]"
    "/best[ext=mp4]"
    "/best"
)
AUDIO_FORMAT = "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio"

def _base_opts(out_tpl):
    return {
        "outtmpl":                      out_tpl,
        "quiet":                        True,
        "no_warnings":                  True,
        "ignoreerrors":                 False,
        "fragment_retries":             3,
        "retries":                      3,
        "check_formats":                False,   # не проверять форматы заранее
        "concurrent_fragment_downloads": 4,       # параллельная загрузка фрагментов
        "http_chunk_size":              10485760, # 10 МБ чанки
        "no_part":                      True,     # писать сразу без .part файла
        "noprogress":                   True,
    }

# ══════════════════════════════════
#  HTTP HANDLER
# ══════════════════════════════════
class Handler(BaseHTTPRequestHandler):

    def log_message(self, fmt, *args):
        log.info("%s - %s", self.address_string(), fmt % args)

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Token")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,DELETE,OPTIONS")

    def _json(self, code, data):
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self._cors()
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if not length: return {}
        try: return json.loads(self.rfile.read(length))
        except Exception: return {}

    def _require_auth(self):
        uid = verify_token(self.headers.get("X-Token", ""))
        if not uid:
            self._json(401, {"ok": False, "error": "Unauthorized"})
        return uid

    def _stream_file(self, fpath, fname):
        """Стриминг файла чанками — не грузит весь файл в память."""
        size = os.path.getsize(fpath)
        mime, _ = mimetypes.guess_type(fname)
        content_type = mime or "application/octet-stream"

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Disposition", f'attachment; filename="{fname}"')
        self.send_header("Content-Length", size)
        self.send_header("Cache-Control", "public, max-age=120")
        self._cors()
        self.end_headers()

        with open(fpath, "rb") as f:
            while True:
                chunk = f.read(STREAM_CHUNK)
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    break

    # ── OPTIONS ──
    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        self.end_headers()

    # ── GET ──
    def do_GET(self):
        parsed = urlparse(self.path)
        path   = parsed.path.rstrip("/") or "/"

        if path in ("/", "/miniapp.html", ""):
            fpath = BASE_DIR / "miniapp.html"
            if not fpath.exists():
                self._json(404, {"error": "miniapp.html not found"}); return
            self._stream_file(str(fpath), "miniapp.html")
            return

        # Статические файлы из корня
        if path.count("/") == 1:
            ext = Path(path).suffix.lower()
            if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg"):
                fpath = BASE_DIR / path.lstrip("/")
                if fpath.exists():
                    self._stream_file(str(fpath), fpath.name)
                return

        # /api/file/<name>
        if path.startswith("/api/file/"):
            uid = self._require_auth()
            if not uid: return
            fname = os.path.basename(path[10:])
            fpath = Path(DOWNLOADS_DIR) / fname
            if not fpath.exists():
                self._json(404, {"ok": False, "error": "Not found"}); return
            self._stream_file(str(fpath), fname)
            return

        # /login/<token>
        if path.startswith("/login/"):
            token = path[7:]
            uid = lt_consume(token)
            if not uid:
                body = "<html><body><h2>&#128683; Link is invalid or expired.</h2><p>Request a new one in the bot: /start</p></body></html>".encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", len(body))
                self.end_headers()
                self.wfile.write(body)
                return
            u = db_get_user(uid)
            if not u:
                db_upsert_user(uid, "", "")
                u = db_get_user(uid)
            session_token = make_token(uid)
            prem = is_premium(uid)
            user_json = json.dumps({
                "id": uid,
                "first_name": u["first_name"] if u else "",
                "username": u["username"] if u else "",
                "is_premium": prem,
            }, ensure_ascii=False)
            html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>Вход...</title>
<script>
  localStorage.setItem('pw_token', {json.dumps(session_token)});
  localStorage.setItem('pw_user', {json.dumps(user_json)});
  window.location.replace('/');
</script>
</head><body>
<p>&#128275; Выполняем вход... <a href="/">Нажмите сюда если не перенаправило</a></p>
</body></html>""".encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", len(html))
            self._cors()
            self.end_headers()
            self.wfile.write(html)
            return

        if path == "/api/auth/me":
            uid = self._require_auth()
            if not uid: return
            u = db_get_user(uid)
            if not u:
                self._json(404, {"ok": False, "error": "User not found"}); return
            self._json(200, {"ok": True, "user": {
                "id": uid, "first_name": u["first_name"],
                "username": u["username"], "is_premium": is_premium(uid),
            }})
            return

        if path == "/api/limits":
            uid = self._require_auth()
            if not uid: return
            prem  = is_premium(uid)
            limit = PREMIUM_DL_DAY if prem else FREE_DL_DAY
            used  = db_get_search_dl(uid)
            self._json(200, {"ok": True, "used": used, "limit": limit, "is_premium": prem})
            return

        self._json(404, {"error": "not found"})

    # ── DELETE ──
    def do_DELETE(self):
        path = urlparse(self.path).path.rstrip("/")
        if path.startswith("/api/delete/"):
            uid = self._require_auth()
            if not uid: return
            safe_id = os.path.basename(path[12:])[:16]
            for f in glob.glob(os.path.join(DOWNLOADS_DIR, f"{safe_id}.*")):
                try: os.remove(f); log.info("Deleted %s", f)
                except Exception: pass
            self._json(200, {"ok": True})
            return
        self._json(404, {"error": "not found"})

    # ── POST ──
    def do_POST(self):
        path = urlparse(self.path).path.rstrip("/")
        body = self._read_body()

        # /api/auth/telegram
        if path == "/api/auth/telegram":
            tg_user = verify_telegram_init_data(body.get("init_data", ""))
            if not tg_user:
                self._json(401, {"ok": False, "error": "Invalid Telegram data"}); return
            uid = tg_user["id"]
            db_upsert_user(uid, tg_user.get("username", ""), tg_user.get("first_name", ""))
            token = make_token(uid)
            self._json(200, {"ok": True, "token": token, "user": {
                "id": uid, "first_name": tg_user.get("first_name", ""),
                "username": tg_user.get("username", ""), "is_premium": is_premium(uid),
            }})
            return

        # /api/info
        if path == "/api/info":
            uid = self._require_auth()
            if not uid: return
            url = body.get("url", "").strip()
            if not url:
                self._json(400, {"ok": False, "error": "No URL"}); return
            try:
                with yt_dlp.YoutubeDL({
                    "quiet": True, "no_warnings": True,
                    "check_formats": False,  # быстрее без проверки
                }) as ydl:
                    info = ydl.extract_info(url, download=False)
            except Exception as e:
                self._json(200, {"ok": False, "error": str(e)[:200]}); return
            if not info:
                self._json(200, {"ok": False, "error": "Не удалось получить информацию"}); return

            dur = info.get("duration") or 0
            if dur:
                m, s = divmod(int(dur), 60); h, m = divmod(m, 60)
                dur_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
            else:
                dur_str = ""

            formats_out, seen = [], set()
            for f in sorted(info.get("formats", []), key=lambda x: x.get("height", 0) or 0, reverse=True):
                h = f.get("height")
                if not h or h in seen or h < 144: continue
                seen.add(h)
                formats_out.append({"format_id": f["format_id"], "height": h,
                    "format_note": f.get("format_note", ""),
                    "filesize": f.get("filesize")})
                if len(formats_out) >= 6: break

            self._json(200, {"ok": True, "url": url,
                "title": info.get("title", "")[:200],
                "thumbnail": info.get("thumbnail"),
                "duration": dur, "duration_str": dur_str,
                "view_count": info.get("view_count"),
                "extractor": info.get("extractor_key", info.get("extractor", "")),
                "formats": formats_out})
            return

        # /api/download
        if path == "/api/download":
            uid = self._require_auth()
            if not uid: return
            url    = body.get("url", "").strip()
            fmt_id = body.get("format_id", "best")
            mode   = body.get("mode", "video")
            if not url:
                self._json(400, {"ok": False, "error": "No URL"}); return

            file_id = str(uuid.uuid4())[:8]
            out_tpl = os.path.join(DOWNLOADS_DIR, f"{file_id}.%(ext)s")
            opts = _base_opts(out_tpl)

            if mode == "audio":
                opts["format"] = AUDIO_FORMAT
            elif fmt_id and fmt_id != "best":
                # Пользователь выбрал конкретный формат
                opts["format"] = fmt_id
            else:
                opts["format"] = VIDEO_FORMAT

            try:
                yt_dlp.YoutubeDL(opts).download([url])
            except Exception as e:
                self._json(200, {"ok": False, "error": str(e)[:200]}); return

            files = [f for f in glob.glob(os.path.join(DOWNLOADS_DIR, f"{file_id}.*"))
                     if not f.endswith(".part") and os.path.getsize(f) > 1024]
            if not files:
                self._json(200, {"ok": False, "error": "Файл не найден после загрузки"}); return

            filepath = sorted(files)[-1]
            filename = os.path.basename(filepath)
            size     = os.path.getsize(filepath)

            schedule_delete(filepath, FILE_TTL_SEC)
            self._json(200, {"ok": True, "file_id": file_id, "filename": filename,
                             "size": size, "download_url": f"/api/file/{filename}"})
            return

        # /api/search
        if path == "/api/search":
            uid = self._require_auth()
            if not uid: return
            query    = body.get("query", "").strip()
            platform = body.get("platform", "yt")
            if not query:
                self._json(400, {"ok": False, "error": "No query"}); return

            search_url = f"ytsearch100:{query}" if platform == "yt" else f"tiktok:search:{query}"
            opts = {
                "quiet": True, "no_warnings": True,
                "extract_flat": True, "skip_download": True,
                "check_formats": False,
            }
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
                            m, s = divmod(int(dur), 60); h, m = divmod(m, 60)
                            dur_str = f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"
                        vc = e.get("view_count") or 0
                        vs = (f"{vc/1e6:.1f}M 👁" if vc >= 1e6
                              else f"{vc//1000}K 👁" if vc >= 1e3
                              else f"{vc} 👁" if vc else "")
                        results.append({"title": (e.get("title") or "Unknown")[:80],
                            "url": url, "thumbnail": e.get("thumbnail"),
                            "duration": dur_str, "views": vs})
            except Exception as ex:
                log.warning("Search error: %s", ex)

            used  = db_get_search_dl(uid)
            limit = PREMIUM_DL_DAY if is_premium(uid) else FREE_DL_DAY
            self._json(200, {"ok": True, "results": results[:100], "used": used, "limit": limit})
            return

        # /api/search-download
        if path == "/api/search-download":
            uid = self._require_auth()
            if not uid: return
            prem  = is_premium(uid)
            limit = PREMIUM_DL_DAY if prem else FREE_DL_DAY
            used  = db_get_search_dl(uid)
            if used >= limit:
                self._json(429, {"ok": False, "error": f"Лимит {limit}/день исчерпан"}); return
            url = body.get("url", "").strip()
            if not url:
                self._json(400, {"ok": False, "error": "No URL"}); return

            file_id = str(uuid.uuid4())[:8]
            out_tpl = os.path.join(DOWNLOADS_DIR, f"{file_id}.%(ext)s")
            opts = _base_opts(out_tpl)
            opts["format"] = VIDEO_FORMAT

            try:
                yt_dlp.YoutubeDL(opts).download([url])
            except Exception as e:
                self._json(200, {"ok": False, "error": str(e)[:200]}); return

            files = [f for f in glob.glob(os.path.join(DOWNLOADS_DIR, f"{file_id}.*"))
                     if not f.endswith(".part") and os.path.getsize(f) > 1024]
            if not files:
                self._json(200, {"ok": False, "error": "Файл не найден"}); return

            filepath = sorted(files)[-1]
            filename = os.path.basename(filepath)
            db_inc_search_dl(uid)
            used += 1
            schedule_delete(filepath, FILE_TTL_SEC)
            self._json(200, {"ok": True, "file_id": file_id, "filename": filename,
                             "download_url": f"/api/file/{filename}",
                             "used": used, "limit": limit})
            return

        self._json(404, {"error": "not found"})


# ══════════════════════════════════
#  THREADED SERVER
# ══════════════════════════════════
class ThreadedHTTPServer(HTTPServer):
    def process_request(self, request, client_address):
        t = threading.Thread(target=self._handle, args=(request, client_address), daemon=True)
        t.start()

    def _handle(self, request, client_address):
        try: self.finish_request(request, client_address)
        except Exception: pass
        finally: self.shutdown_request(request)


def run(port=None):
    cleanup_old_files()
    p = port or PORT
    server = ThreadedHTTPServer(("0.0.0.0", p), Handler)
    log.info("🌐 Mini App running on http://0.0.0.0:%d", p)
    server.serve_forever()


if __name__ == "__main__":
    run()
