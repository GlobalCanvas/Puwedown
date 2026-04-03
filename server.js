'use strict';
// ════════════════════════════════════════════════════════════════════════════
//  PuweDownloader — server.js
//  Express web server + all API endpoints
//  Drop-in Node.js replacement for webapp.py
// ════════════════════════════════════════════════════════════════════════════
//
//  Endpoints:
//    POST   /api/auth/telegram    — verify Telegram initData, issue token
//    GET    /api/auth/me          — validate session token
//    POST   /api/info             — fetch video metadata (no download)
//    POST   /api/download         — download video / audio → temp link
//    POST   /api/search           — search YouTube / TikTok
//    POST   /api/search-download  — download a search result (rate-limited)
//    GET    /api/limits           — daily usage for current user
//    DELETE /api/delete/:id       — manually delete a downloaded file
//    GET    /api/file/:name       — stream a downloaded file (Range-aware)
//    GET    /login/:token         — one-time bot login link
//    GET    /                     — serves miniapp.html
//
//  Usage:
//    BOT_TOKEN=xxx node server.js
// ════════════════════════════════════════════════════════════════════════════

require('dotenv').config();

const express  = require('express');
const crypto   = require('crypto');
const fs       = require('fs');
const path     = require('path');
const { spawn } = require('child_process');
const { v4: uuidv4 } = require('uuid');
const Database = require('better-sqlite3');

// ─── Config ──────────────────────────────────────────────────────────────────
const PORT          = parseInt(process.env.WEBAPP_PORT, 10)  || 3000;
const BOT_TOKEN     = process.env.BOT_TOKEN                  || '';
const SECRET_SALT   = process.env.SECRET_SALT                || 'puwe_webapp_v1';
const DB_FILE       = path.resolve(process.env.BOT_DB        || './bot.db');
const DL_DIR        = path.resolve(process.env.DOWNLOADS_DIR || './downloads');
const FILE_TTL_MS   = (parseInt(process.env.FILE_TTL_SEC, 10) || 120) * 1000;
const FREE_DL_DAY   = parseInt(process.env.FREE_DL_DAY,    10) || 3;
const PREM_DL_DAY   = parseInt(process.env.PREMIUM_DL_DAY, 10) || 12;
const LT_FILE       = path.resolve(process.env.LOGIN_TOKENS_FILE || './login_tokens.json');
const MINIAPP_HTML  = path.resolve('./miniapp.html');

// Ensure downloads directory exists.
fs.mkdirSync(DL_DIR, { recursive: true });

// ─── Logging ─────────────────────────────────────────────────────────────────
function log(level, ...args) {
  const ts = new Date().toISOString();
  (level === 'error' ? console.error : console.log)(`[${ts}] [${level.toUpperCase()}]`, ...args);
}

// ════════════════════════════════════════════════════════════════════════════
//  DATABASE  (better-sqlite3 — synchronous, WAL mode, shared with bot.js)
// ════════════════════════════════════════════════════════════════════════════
const db = new Database(DB_FILE);
db.pragma('journal_mode = WAL');   // safe concurrent access with bot.js
db.pragma('synchronous  = NORMAL');
db.pragma('foreign_keys = ON');

// Create tables if bot.js hasn't run yet (standalone mode).
db.exec(`
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
  CREATE TABLE IF NOT EXISTS search_downloads (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id    INTEGER,
    date_str   TEXT,
    count      INTEGER DEFAULT 0,
    UNIQUE(user_id, date_str)
  );
  CREATE INDEX IF NOT EXISTS idx_premium ON users(premium_until);
`);

// Prepared statements — compiled once, reused on every request.
const stmts = {
  getUser:       db.prepare('SELECT * FROM users WHERE user_id = ?'),
  userExists:    db.prepare('SELECT 1 FROM users WHERE user_id = ?'),
  insertUser:    db.prepare('INSERT INTO users (user_id, username, first_name, joined_at, last_seen) VALUES (?, ?, ?, ?, ?)'),
  updateUser:    db.prepare('UPDATE users SET username = ?, first_name = ?, last_seen = ? WHERE user_id = ?'),
  allUserIds:    db.prepare('SELECT user_id FROM users'),
  getSearchDl:   db.prepare('SELECT count FROM search_downloads WHERE user_id = ? AND date_str = ?'),
  incSearchDl:   db.prepare(`
    INSERT INTO search_downloads (user_id, date_str, count) VALUES (?, ?, 1)
    ON CONFLICT(user_id, date_str) DO UPDATE SET count = count + 1
  `),
};

// ─── DB helpers ──────────────────────────────────────────────────────────────

function dbGetUser(uid) {
  return stmts.getUser.get(uid);
}

function dbUpsertUser(uid, username, firstName) {
  const now = Math.floor(Date.now() / 1000);
  if (stmts.userExists.get(uid)) {
    stmts.updateUser.run(username, firstName, now, uid);
  } else {
    stmts.insertUser.run(uid, username, firstName, now, now);
  }
}

function dbIsPremium(uid) {
  const u = dbGetUser(uid);
  if (!u) return false;
  return u.premium_until === -1 || u.premium_until > Math.floor(Date.now() / 1000);
}

function dbGetSearchDlCount(uid) {
  const today = new Date().toISOString().slice(0, 10);
  const row = stmts.getSearchDl.get(uid, today);
  return row ? row.count : 0;
}

function dbIncSearchDl(uid) {
  const today = new Date().toISOString().slice(0, 10);
  stmts.incSearchDl.run(uid, today);
}

// ════════════════════════════════════════════════════════════════════════════
//  AUTHENTICATION
// ════════════════════════════════════════════════════════════════════════════

// ─── Session tokens ───────────────────────────────────────────────────────────
// Format: SHA-256( "{uid}:{salt}:{dayIndex}" )
// Valid for a 3-day rolling window so users aren't logged out at midnight.

function makeToken(uid) {
  const day = Math.floor(Date.now() / 86400000);
  return crypto.createHash('sha256').update(`${uid}:${SECRET_SALT}:${day}`).digest('hex');
}

function verifyToken(token) {
  if (!token || token.length !== 64) return null;
  const now  = Math.floor(Date.now() / 86400000);
  const rows = stmts.allUserIds.all();

  for (const { user_id: uid } of rows) {
    for (let offset = 0; offset < 3; offset++) {
      const expected = crypto
        .createHash('sha256')
        .update(`${uid}:${SECRET_SALT}:${now - offset}`)
        .digest('hex');
      try {
        if (crypto.timingSafeEqual(Buffer.from(expected, 'hex'), Buffer.from(token, 'hex'))) {
          return uid;
        }
      } catch { /* length mismatch — skip */ }
    }
  }
  return null;
}

// ─── Telegram initData verification ──────────────────────────────────────────
// https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app

function verifyTelegramInitData(initData) {
  try {
    const params    = new URLSearchParams(initData);
    const checkHash = params.get('hash');
    if (!checkHash) return null;

    // Auth timestamp must be ≤ 5 minutes old.
    const authDate = parseInt(params.get('auth_date') || '0', 10);
    if (Math.floor(Date.now() / 1000) - authDate > 300) return null;

    // Build sorted key=value data-check string (without hash).
    params.delete('hash');
    const dataCheckStr = [...params.entries()]
      .sort(([a], [b]) => a.localeCompare(b))
      .map(([k, v]) => `${k}=${v}`)
      .join('\n');

    // secret_key = HMAC-SHA256("WebAppData", bot_token)
    const secretKey = crypto.createHmac('sha256', 'WebAppData').update(BOT_TOKEN).digest();
    const computed  = crypto.createHmac('sha256', secretKey).update(dataCheckStr).digest('hex');

    if (!crypto.timingSafeEqual(Buffer.from(computed, 'hex'), Buffer.from(checkHash, 'hex'))) {
      return null;
    }

    return JSON.parse(new URLSearchParams(initData).get('user') || '{}');
  } catch {
    return null;
  }
}

// ─── One-time login tokens (shared file with bot.js) ─────────────────────────

function ltConsume(token) {
  let data = {};
  try { data = JSON.parse(fs.readFileSync(LT_FILE, 'utf8')); } catch { return null; }

  const entry = data[token];
  if (!entry) return null;

  // Always delete the token (one-time use), even if expired.
  delete data[token];
  try { fs.writeFileSync(LT_FILE, JSON.stringify(data), 'utf8'); } catch { /* ignore */ }

  if (entry.expires < Math.floor(Date.now() / 1000)) return null;
  return entry.uid;
}

// ─── Auth middleware ──────────────────────────────────────────────────────────

function requireAuth(req, res, next) {
  const uid = verifyToken(req.headers['x-token'] || '');
  if (!uid) return res.status(401).json({ ok: false, error: 'Unauthorized' });
  req.uid = uid;
  next();
}

// ════════════════════════════════════════════════════════════════════════════
//  YT-DLP  (child_process.spawn — non-blocking, no shell injection)
// ════════════════════════════════════════════════════════════════════════════

// Spawn yt-dlp and collect stdout.  Resolves { stdout, stderr, code }.
function ytdlp(args, timeoutMs = 300_000) {
  return new Promise((resolve, reject) => {
    const out  = [];
    const err  = [];
    const proc = spawn('yt-dlp', args, { stdio: ['ignore', 'pipe', 'pipe'] });

    let timedOut = false;
    const timer  = setTimeout(() => {
      timedOut = true;
      proc.kill('SIGTERM');
    }, timeoutMs);

    proc.stdout.on('data', c => out.push(c));
    proc.stderr.on('data', c => err.push(c));
    proc.on('error', e => {
      clearTimeout(timer);
      reject(e.code === 'ENOENT' ? new Error('yt-dlp not found in PATH') : e);
    });
    proc.on('close', code => {
      clearTimeout(timer);
      if (timedOut) return reject(new Error('yt-dlp timed out'));
      resolve({
        stdout: Buffer.concat(out).toString('utf8'),
        stderr: Buffer.concat(err).toString('utf8'),
        code,
      });
    });
  });
}

// Format seconds → "H:MM:SS" or "M:SS"
function fmtDuration(sec) {
  if (!sec) return '';
  sec = Math.floor(sec);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return h
    ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`;
}

// Find files matching a pattern "PREFIX.*" in DL_DIR.
function findFiles(prefix) {
  try {
    return fs.readdirSync(DL_DIR)
      .filter(f => f.startsWith(prefix + '.') && !f.endsWith('.part'))
      .map(f => path.join(DL_DIR, f))
      .filter(fp => { try { return fs.statSync(fp).size > 1024; } catch { return false; } });
  } catch {
    return [];
  }
}

// ─── getInfo ─────────────────────────────────────────────────────────────────

async function ytGetInfo(url) {
  const { stdout, stderr, code } = await ytdlp([
    '--dump-json', '--no-playlist', '--quiet', '--no-warnings', url,
  ], 60_000);

  if (code !== 0 || !stdout.trim()) {
    const msg = stderr.trim().split('\n').pop() || 'Could not fetch video info';
    throw new Error(msg.slice(0, 200));
  }

  const info = JSON.parse(stdout.trim());

  // Build deduplicated format list, sorted best→worst, max 6 entries.
  const seen = new Set();
  const formats = [];
  const sorted = (info.formats || [])
    .filter(f => f.height && f.height >= 144)
    .sort((a, b) => (b.height || 0) - (a.height || 0));

  for (const f of sorted) {
    if (seen.has(f.height)) continue;
    seen.add(f.height);
    formats.push({
      format_id:   f.format_id,
      height:      f.height,
      format_note: f.format_note || '',
      filesize:    f.filesize    || null,
    });
    if (formats.length >= 6) break;
  }

  return {
    ok:           true,
    url,
    title:        (info.title || '').slice(0, 200),
    thumbnail:    info.thumbnail    || null,
    duration:     info.duration     || 0,
    duration_str: fmtDuration(info.duration),
    view_count:   info.view_count   || null,
    extractor:    info.extractor_key || info.extractor || '',
    formats,
  };
}

// ─── downloadVideo ───────────────────────────────────────────────────────────
// No size cap — requirement says remove the 50 MB limit.

async function ytDownload(url, mode = 'video') {
  const fileId = uuidv4().slice(0, 8);
  const outTpl = path.join(DL_DIR, `${fileId}.%(ext)s`);

  const fmt = mode === 'audio'
    ? 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio'
    // Prefer single-file formats (video+audio muxed) — avoids requiring ffmpeg.
    // Falls back progressively until something downloads.
    : 'best[vcodec!=none][acodec!=none][ext=mp4]/best[vcodec!=none][acodec!=none]/best[ext=mp4]/best';

  const { stderr, code } = await ytdlp([
    '--output',            outTpl,
    '--format',            fmt,
    '--no-playlist',
    '--quiet',
    '--no-warnings',
    '--fragment-retries',  '3',
    '--retries',           '3',
    '--extractor-args',    'youtube:player_client=android_embedded,web',
    url,
  ], 600_000); // 10-minute timeout for large files

  if (code !== 0) {
    const msg = stderr.trim().split('\n').pop() || 'Download failed';
    throw new Error(msg.slice(0, 200));
  }

  const files = findFiles(fileId);
  if (!files.length) throw new Error('File not found after download');

  // Pick most recently modified file.
  files.sort((a, b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs);
  const filepath = files[0];
  const filename = path.basename(filepath);
  const size     = fs.statSync(filepath).size;

  return { fileId, filename, filepath, size };
}

// ─── search ──────────────────────────────────────────────────────────────────

async function ytSearch(query, platform = 'yt') {
  const searchUrl = platform === 'yt'
    ? `ytsearch100:${query}`
    : `tiktok:search:${query}`;

  const { stdout, code } = await ytdlp([
    '--flat-playlist',
    '--dump-single-json',
    '--quiet',
    '--no-warnings',
    searchUrl,
  ], 60_000);

  if (code !== 0 || !stdout.trim()) return [];

  let info;
  try { info = JSON.parse(stdout.trim()); } catch { return []; }

  const results = [];
  for (const e of (info.entries || [])) {
    if (!e) continue;

    let url = e.url || e.webpage_url;
    if (!url) {
      const id = e.id;
      if (platform === 'yt' && id)  url = `https://www.youtube.com/watch?v=${id}`;
      else if (id)                  url = `https://www.tiktok.com/@${e.uploader || 'user'}/video/${id}`;
    }
    if (!url) continue;

    const vc = e.view_count || 0;
    const views = vc >= 1e6  ? `${(vc / 1e6).toFixed(1)}M 👁`
                : vc >= 1e3  ? `${Math.floor(vc / 1000)}K 👁`
                : vc > 0     ? `${vc} 👁`
                : '';

    results.push({
      title:     (e.title || 'Unknown').slice(0, 80),
      url,
      thumbnail: e.thumbnail || null,
      duration:  fmtDuration(e.duration),
      views,
    });
  }

  return results.slice(0, 100);
}

// ════════════════════════════════════════════════════════════════════════════
//  FILE CLEANUP
// ════════════════════════════════════════════════════════════════════════════

// Schedule deletion of a single file after delayMs.
function scheduleDelete(filepath, delayMs) {
  setTimeout(() => {
    fs.unlink(filepath, err => {
      if (!err) log('info', `[cleanup] Deleted: ${path.basename(filepath)}`);
    });
  }, delayMs);
}

// Remove all files in DL_DIR older than 1 hour (safety net for crashes).
function cleanupOldFiles() {
  const cutoff = Date.now() - 3_600_000;
  let removed  = 0;
  try {
    for (const name of fs.readdirSync(DL_DIR)) {
      const fp = path.join(DL_DIR, name);
      try {
        if (fs.statSync(fp).mtimeMs < cutoff) { fs.unlinkSync(fp); removed++; }
      } catch { /* skip */ }
    }
  } catch { /* DL_DIR not yet created */ }
  if (removed) log('info', `[cleanup] Removed ${removed} stale file(s)`);
}

// Run once at startup, then every 5 minutes.
cleanupOldFiles();
setInterval(cleanupOldFiles, 5 * 60_000).unref();

// ════════════════════════════════════════════════════════════════════════════
//  EXPRESS APP
// ════════════════════════════════════════════════════════════════════════════
const app = express();

// CORS — required for Telegram Mini App iframe
app.use((req, res, next) => {
  res.setHeader('Access-Control-Allow-Origin',  '*');
  res.setHeader('Access-Control-Allow-Headers', 'Content-Type, X-Token');
  res.setHeader('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS');
  if (req.method === 'OPTIONS') return res.sendStatus(204);
  next();
});

// Security
app.disable('x-powered-by');
app.use((req, res, next) => {
  res.setHeader('X-Content-Type-Options', 'nosniff');
  next();
});

// JSON body parsing — bodies are small (URLs + params only).
app.use(express.json({ limit: '1mb' }));

// Request logging
app.use((req, res, next) => {
  const t = Date.now();
  res.on('finish', () => {
    log('info', `${req.method} ${req.path} ${res.statusCode} ${Date.now() - t}ms`);
  });
  next();
});

// ─────────────────────────────────────────────────────────────────────────────
//  POST /api/auth/telegram
//  Verifies Telegram WebApp initData and issues a session token.
// ─────────────────────────────────────────────────────────────────────────────
app.post('/api/auth/telegram', (req, res) => {
  const initData = (req.body.init_data || '').trim();
  if (!initData) return res.status(400).json({ ok: false, error: 'init_data required' });

  const tgUser = verifyTelegramInitData(initData);
  if (!tgUser) return res.status(401).json({ ok: false, error: 'Invalid Telegram data' });

  const uid = tgUser.id;
  dbUpsertUser(uid, tgUser.username || '', tgUser.first_name || '');

  return res.json({
    ok:    true,
    token: makeToken(uid),
    user: {
      id:         uid,
      first_name: tgUser.first_name || '',
      username:   tgUser.username   || '',
      is_premium: dbIsPremium(uid),
    },
  });
});

// ─────────────────────────────────────────────────────────────────────────────
//  GET /api/auth/me
//  Returns profile of the authenticated user.
// ─────────────────────────────────────────────────────────────────────────────
app.get('/api/auth/me', requireAuth, (req, res) => {
  const u = dbGetUser(req.uid);
  if (!u) return res.status(404).json({ ok: false, error: 'User not found' });

  return res.json({
    ok:   true,
    user: {
      id:         req.uid,
      first_name: u.first_name,
      username:   u.username,
      is_premium: dbIsPremium(req.uid),
    },
  });
});

// ─────────────────────────────────────────────────────────────────────────────
//  POST /api/info
//  Fetches video metadata without downloading.
// ─────────────────────────────────────────────────────────────────────────────
app.post('/api/info', requireAuth, async (req, res) => {
  const url = (req.body.url || '').trim();
  if (!url) return res.status(400).json({ ok: false, error: 'No URL' });

  try {
    return res.json(await ytGetInfo(url));
  } catch (err) {
    log('warn', '[info] Error:', err.message);
    return res.json({ ok: false, error: err.message.slice(0, 200) });
  }
});

// ─────────────────────────────────────────────────────────────────────────────
//  POST /api/download
//  Downloads a video or audio file, returns a temporary download URL.
//  No size cap — streams files of any size.
// ─────────────────────────────────────────────────────────────────────────────
app.post('/api/download', requireAuth, async (req, res) => {
  const url  = (req.body.url  || '').trim();
  const mode = (req.body.mode || 'video').trim();  // 'video' | 'audio'
  if (!url) return res.status(400).json({ ok: false, error: 'No URL' });

  try {
    const { fileId, filename, filepath, size } = await ytDownload(url, mode);
    scheduleDelete(filepath, FILE_TTL_MS);

    log('info', `[download] ${filename} (${Math.round(size / 1024 / 1024)}MB)`);

    return res.json({
      ok:           true,
      file_id:      fileId,
      filename,
      size,
      download_url: `/api/file/${filename}`,
    });
  } catch (err) {
    log('warn', '[download] Error:', err.message);
    return res.json({ ok: false, error: err.message.slice(0, 200) });
  }
});

// ─────────────────────────────────────────────────────────────────────────────
//  POST /api/search
//  Searches YouTube or TikTok, returns up to 100 results.
// ─────────────────────────────────────────────────────────────────────────────
app.post('/api/search', requireAuth, async (req, res) => {
  const query    = (req.body.query    || '').trim();
  const platform = (req.body.platform || 'yt').trim();
  if (!query) return res.status(400).json({ ok: false, error: 'No query' });

  let results = [];
  try {
    results = await ytSearch(query, platform);
  } catch (err) {
    log('warn', '[search] Error:', err.message);
    // Return empty results — match Python behaviour.
  }

  const prem  = dbIsPremium(req.uid);
  const limit = prem ? PREM_DL_DAY : FREE_DL_DAY;
  const used  = dbGetSearchDlCount(req.uid);

  return res.json({ ok: true, results, used, limit });
});

// ─────────────────────────────────────────────────────────────────────────────
//  POST /api/search-download
//  Downloads a video from search results, enforcing a daily per-user cap.
// ─────────────────────────────────────────────────────────────────────────────
app.post('/api/search-download', requireAuth, async (req, res) => {
  const prem  = dbIsPremium(req.uid);
  const limit = prem ? PREM_DL_DAY : FREE_DL_DAY;
  const used  = dbGetSearchDlCount(req.uid);

  if (used >= limit) {
    return res.status(429).json({ ok: false, error: `Daily limit of ${limit} reached` });
  }

  const url = (req.body.url || '').trim();
  if (!url) return res.status(400).json({ ok: false, error: 'No URL' });

  try {
    const { fileId, filename, filepath } = await ytDownload(url, 'video');

    dbIncSearchDl(req.uid);
    scheduleDelete(filepath, FILE_TTL_MS);

    log('info', `[search-dl] uid=${req.uid} ${filename} used=${used + 1}/${limit}`);

    return res.json({
      ok:           true,
      file_id:      fileId,
      filename,
      download_url: `/api/file/${filename}`,
      used:         used + 1,
      limit,
    });
  } catch (err) {
    log('warn', '[search-dl] Error:', err.message);
    return res.json({ ok: false, error: err.message.slice(0, 200) });
  }
});

// ─────────────────────────────────────────────────────────────────────────────
//  GET /api/limits
//  Returns daily search-download usage for the authenticated user.
// ─────────────────────────────────────────────────────────────────────────────
app.get('/api/limits', requireAuth, (req, res) => {
  const prem  = dbIsPremium(req.uid);
  const limit = prem ? PREM_DL_DAY : FREE_DL_DAY;
  const used  = dbGetSearchDlCount(req.uid);
  return res.json({ ok: true, used, limit, is_premium: prem });
});

// ─────────────────────────────────────────────────────────────────────────────
//  DELETE /api/delete/:id
//  Manually deletes a previously downloaded file before its TTL expires.
// ─────────────────────────────────────────────────────────────────────────────
app.delete('/api/delete/:id', requireAuth, (req, res) => {
  // Sanitise: only alphanumeric and hyphens, max 16 chars.
  const safeId = (req.params.id || '').replace(/[^a-zA-Z0-9-]/g, '').slice(0, 16);
  if (!safeId) return res.status(400).json({ ok: false, error: 'Invalid id' });

  const files = findFiles(safeId);
  let removed = 0;
  for (const fp of files) {
    try { fs.unlinkSync(fp); removed++; log('info', `[delete] Removed ${path.basename(fp)}`); }
    catch (err) { log('warn', `[delete] Could not remove ${fp}: ${err.message}`); }
  }
  return res.json({ ok: true, removed });
});

// ─────────────────────────────────────────────────────────────────────────────
//  GET /api/file/:name
//  Streams a downloaded file to the client.
//  Supports HTTP Range requests — required for large files and video seeking.
//  Never buffers the file in memory — uses fs.createReadStream().pipe(res).
// ─────────────────────────────────────────────────────────────────────────────
app.get('/api/file/:name', (req, res) => {
  // Sanitise filename to prevent path traversal.
  const name = path.basename(req.params.name);
  const fp   = path.join(DL_DIR, name);

  let stat;
  try {
    stat = fs.statSync(fp);
    if (!stat.isFile()) throw new Error();
  } catch {
    return res.status(404).json({ ok: false, error: 'Not found' });
  }

  const total       = stat.size;
  const rangeHeader = req.headers.range;

  if (rangeHeader) {
    // ── Partial content (Range request) ──────────────────────
    // Enables: resumable downloads, video player seeking, mobile streaming
    const match = rangeHeader.match(/bytes=(\d*)-(\d*)/);
    if (!match) {
      res.setHeader('Content-Range', `bytes */${total}`);
      return res.sendStatus(416);
    }
    const start = match[1] ? parseInt(match[1], 10) : 0;
    const end   = match[2] ? parseInt(match[2], 10) : total - 1;

    if (start > end || end >= total) {
      res.setHeader('Content-Range', `bytes */${total}`);
      return res.sendStatus(416);
    }

    res.writeHead(206, {
      'Content-Range':       `bytes ${start}-${end}/${total}`,
      'Accept-Ranges':       'bytes',
      'Content-Length':      end - start + 1,
      'Content-Type':        'application/octet-stream',
      'Content-Disposition': `attachment; filename="${name}"`,
    });

    const stream = fs.createReadStream(fp, { start, end });
    stream.on('error', err => { log('error', '[file] Stream error:', err.message); res.destroy(); });
    stream.pipe(res);
    return;
  }

  // ── Full file stream ──────────────────────────────────────
  res.writeHead(200, {
    'Content-Type':        'application/octet-stream',
    'Content-Disposition': `attachment; filename="${name}"`,
    'Content-Length':      total,
    'Accept-Ranges':       'bytes',
    'Cache-Control':       'no-store',
  });

  const stream = fs.createReadStream(fp);
  stream.on('error', err => { log('error', '[file] Stream error:', err.message); res.destroy(); });
  stream.pipe(res);

  log('info', `[file] Streaming ${name} (${Math.round(total / 1024 / 1024)}MB)`);
});

// ─────────────────────────────────────────────────────────────────────────────
//  GET /login/:token
//  One-time login link generated by bot.js.
//  Validates the token, then emits HTML that auto-stores the session token in
//  localStorage and redirects to the Mini App root.
// ─────────────────────────────────────────────────────────────────────────────
app.get('/login/:token', (req, res) => {
  const uid = ltConsume(req.params.token);

  if (!uid) {
    return res.status(200).send(`<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Invalid link</title></head>
<body>
  <h2>🚫 Link is invalid or expired.</h2>
  <p>Request a new one in the bot: /start</p>
</body></html>`);
  }

  // Ensure the user exists in our DB.
  if (!dbGetUser(uid)) dbUpsertUser(uid, '', '');
  const u = dbGetUser(uid);

  const sessionToken = makeToken(uid);
  const userJson = JSON.stringify({
    id:         uid,
    first_name: u ? u.first_name : '',
    username:   u ? u.username   : '',
    is_premium: dbIsPremium(uid),
  });

  // Inline script stores credentials then redirects — same as Python original.
  return res.status(200).send(`<!DOCTYPE html>
<html>
<head><meta charset="utf-8"><title>Logging in…</title>
<script>
  localStorage.setItem('pw_token', ${JSON.stringify(sessionToken)});
  localStorage.setItem('pw_user',  ${JSON.stringify(userJson)});
  window.location.replace('/');
</script>
</head>
<body><p>🔓 Logging in… <a href="/">Click here if not redirected</a></p></body>
</html>`);
});

// ─────────────────────────────────────────────────────────────────────────────
//  Static images from project root (avatar.png, favicon.ico, etc.)
// ─────────────────────────────────────────────────────────────────────────────
const STATIC_EXTS = new Set(['.png', '.jpg', '.jpeg', '.gif', '.webp', '.ico', '.svg']);
app.get(/^\/[^/]+$/, (req, res, next) => {
  const ext = path.extname(req.path).toLowerCase();
  if (!STATIC_EXTS.has(ext)) return next();
  const fp = path.resolve('.' + req.path);
  if (fs.existsSync(fp)) return res.sendFile(fp);
  next();
});

// ─────────────────────────────────────────────────────────────────────────────
//  GET /  — serve miniapp.html
// ─────────────────────────────────────────────────────────────────────────────
app.get('/', (req, res) => {
  if (fs.existsSync(MINIAPP_HTML)) return res.sendFile(MINIAPP_HTML);
  res.send('<h1>PuweDownloader</h1><p>miniapp.html not found in project root.</p>');
});

// ─────────────────────────────────────────────────────────────────────────────
//  404 catch-all
// ─────────────────────────────────────────────────────────────────────────────
app.use((req, res) => res.status(404).json({ error: 'not found' }));

// Global error handler
// eslint-disable-next-line no-unused-vars
app.use((err, req, res, _next) => {
  log('error', 'Unhandled error:', err);
  res.status(500).json({ ok: false, error: 'Internal server error' });
});

// ─────────────────────────────────────────────────────────────────────────────
//  Start
// ─────────────────────────────────────────────────────────────────────────────
app.listen(PORT, '0.0.0.0', () => {
  log('info', `🌐 PuweDownloader running on http://0.0.0.0:${PORT}`);
  log('info', `   DB:         ${DB_FILE}`);
  log('info', `   Downloads:  ${DL_DIR}`);
  log('info', `   miniapp:    ${fs.existsSync(MINIAPP_HTML) ? '✅ found' : '⚠️  not found'}`);
});

// Graceful shutdown
process.on('SIGTERM', () => { log('info', 'SIGTERM — shutting down'); process.exit(0); });
process.on('SIGINT',  () => { log('info', 'SIGINT — shutting down');  process.exit(0); });

module.exports = app; // for testing
