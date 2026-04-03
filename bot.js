'use strict';
// ════════════════════════════════════════════════════════════════════════════
//  PuweDownloader — bot.js
//  Telegram bot — drop-in Node.js replacement for bot.py
//  Uses node-telegram-bot-api (polling mode)
// ════════════════════════════════════════════════════════════════════════════
//
//  Commands:
//    /start    — welcome / webapp login / subscription deep-link
//    /help     — command list
//    /search   — search YouTube or TikTok
//    /sub      — premium subscription menu
//    /profile  — user profile
//    /settings — language / auto-download toggle
//    /ticket   — support ticket (Premium only)
//    /stats    — admin stats
//    /broadcast— admin broadcast
//    /admin    — admin panel
//
//  Inline callbacks: quality picker, premium purchase, gift flow,
//                    search-download, settings toggles, admin actions
//
//  Payments: Telegram Stars (XTR) — trial / monthly / lifetime / gifts
//
//  Usage:
//    BOT_TOKEN=xxx node bot.js
// ════════════════════════════════════════════════════════════════════════════

require('dotenv').config();

const fs      = require('fs');
const path    = require('path');
const crypto  = require('crypto');
const { spawn } = require('child_process');
const TelegramBot = require('node-telegram-bot-api');
const Database    = require('better-sqlite3');

// ─── Config ──────────────────────────────────────────────────────────────────
const BOT_TOKEN    = process.env.BOT_TOKEN    || '';
const BOT_USERNAME = process.env.BOT_USERNAME || 'PuweDownloaderBot';
const SITE_URL     = process.env.SITE_URL     || 'https://puwedown.bothost.tech';
const ADMIN_IDS    = new Set(
  (process.env.ADMIN_IDS || '5268649092').split(',').map(s => parseInt(s.trim(), 10))
);
const DB_FILE  = path.resolve(process.env.BOT_DB        || './bot.db');
const DL_DIR   = path.resolve(process.env.DOWNLOADS_DIR || './downloads');
const LT_FILE  = path.resolve(process.env.LOGIN_TOKENS_FILE || './login_tokens.json');

const TRIAL_STARS    = 5;
const TRIAL_DAYS     = 7;
const MONTHLY_BASE   = 50;
const MONTHLY_EXTRA  = 25;
const LIFETIME_STARS = 500;
const MAX_MONTHS     = 12;
const FREE_SEARCH_DL   = 3;
const PREM_SEARCH_DL   = 12;
const LOGIN_TTL        = 900; // 15 minutes

fs.mkdirSync(DL_DIR, { recursive: true });

if (!BOT_TOKEN) { console.error('BOT_TOKEN is not set'); process.exit(1); }

// ─── Logging ─────────────────────────────────────────────────────────────────
function log(level, ...args) {
  const ts = new Date().toISOString();
  (level === 'error' ? console.error : console.log)(`[${ts}] [${level.toUpperCase()}]`, ...args);
}

// ════════════════════════════════════════════════════════════════════════════
//  DATABASE  (same bot.db shared with server.js)
// ════════════════════════════════════════════════════════════════════════════
const db = new Database(DB_FILE);
db.pragma('journal_mode = WAL');
db.pragma('synchronous  = NORMAL');
db.pragma('foreign_keys = ON');

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
`);

// Prepared statements
const S = {
  getUser:      db.prepare('SELECT * FROM users WHERE user_id = ?'),
  exists:       db.prepare('SELECT 1 FROM users WHERE user_id = ?'),
  insert:       db.prepare('INSERT INTO users (user_id,username,first_name,joined_at,last_seen) VALUES (?,?,?,?,?)'),
  update:       db.prepare('UPDATE users SET username=?,first_name=?,last_seen=? WHERE user_id=?'),
  setField:     null, // built dynamically (field is validated before use)
  incDl:        db.prepare('UPDATE users SET downloads=downloads+1 WHERE user_id=?'),
  addStars:     db.prepare('UPDATE users SET stars_spent=stars_spent+? WHERE user_id=?'),
  markTrial:    db.prepare('UPDATE users SET trial_used=1 WHERE user_id=?'),
  getPremium:   db.prepare('SELECT premium_until FROM users WHERE user_id=?'),
  setPremium:   db.prepare('UPDATE users SET premium_until=? WHERE user_id=?'),
  logTx:        db.prepare('INSERT INTO transactions (user_id,stars,tx_type,months,payload,created_at) VALUES (?,?,?,?,?,?)'),
  addTicket:    db.prepare('INSERT INTO support_tickets (user_id,message,created_at) VALUES (?,?,?)'),
  getSearchDl:  db.prepare('SELECT count FROM search_downloads WHERE user_id=? AND date_str=?'),
  incSearchDl:  db.prepare(`
    INSERT INTO search_downloads (user_id,date_str,count) VALUES (?,?,1)
    ON CONFLICT(user_id,date_str) DO UPDATE SET count=count+1
  `),
  allUids:      db.prepare('SELECT user_id FROM users'),
  byUsername:   db.prepare('SELECT * FROM users WHERE LOWER(username)=?'),
  stats:        db.prepare(`
    SELECT
      COUNT(*) AS total,
      SUM(CASE WHEN premium_until=-1 OR premium_until>? THEN 1 ELSE 0 END) AS premium,
      SUM(CASE WHEN joined_at>=? THEN 1 ELSE 0 END) AS today,
      COALESCE(SUM(downloads),0) AS downloads
    FROM users
  `),
  txStars:      db.prepare('SELECT COALESCE(SUM(stars),0) AS stars FROM transactions'),
};

// ─── DB helpers ──────────────────────────────────────────────────────────────

function dbGet(uid)                    { return S.getUser.get(uid); }
function dbUpsert(uid, username, firstName) {
  const now = Math.floor(Date.now() / 1000);
  S.exists.get(uid) ? S.update.run(username, firstName, now, uid)
                    : S.insert.run(uid, username, firstName, now, now);
}
function dbSet(uid, field, value) {
  if (!['language', 'auto_dl'].includes(field)) return;
  db.prepare(`UPDATE users SET ${field}=? WHERE user_id=?`).run(value, uid);
}
function dbIncDl(uid)                  { S.incDl.run(uid); }
function dbAddStars(uid, stars)        { S.addStars.run(stars, uid); }
function dbMarkTrial(uid)              { S.markTrial.run(uid); }
function dbLogTx(uid, stars, type, months = 0, payload = '') {
  S.logTx.run(uid, stars, type, months, payload, Math.floor(Date.now() / 1000));
}
function dbAddTicket(uid, msg) {
  return S.addTicket.run(uid, msg, Math.floor(Date.now() / 1000)).lastInsertRowid;
}
function dbGetSearchDl(uid) {
  const today = new Date().toISOString().slice(0, 10);
  const r = S.getSearchDl.get(uid, today);
  return r ? r.count : 0;
}
function dbIncSearchDl(uid) {
  S.incSearchDl.run(uid, new Date().toISOString().slice(0, 10));
}
function dbAllUids()                   { return S.allUids.all().map(r => r.user_id); }
function dbByUsername(un)              { return S.byUsername.get(un.replace('@', '').toLowerCase()); }
function dbStats() {
  const now   = Math.floor(Date.now() / 1000);
  const today = Math.floor(new Date().setHours(0,0,0,0) / 1000);
  const r     = S.stats.get(now, today);
  return { ...r, stars: S.txStars.get().stars };
}

function dbAddPremium(uid, days) {
  // days === -1 → lifetime
  const now = Math.floor(Date.now() / 1000);
  const row = S.getPremium.get(uid);
  if (!row) return;
  const pu = row.premium_until;
  let newPu;
  if (days === -1)       newPu = -1;
  else if (pu === -1)    newPu = -1;
  else if (pu > now)     newPu = pu + days * 86400;
  else                   newPu = now + days * 86400;
  S.setPremium.run(newPu, uid);
}

function isPremium(uid) {
  if (ADMIN_IDS.has(uid)) return true;
  const u = dbGet(uid);
  if (!u) return false;
  return u.premium_until === -1 || u.premium_until > Math.floor(Date.now() / 1000);
}

// ════════════════════════════════════════════════════════════════════════════
//  LOGIN TOKENS  (shared with server.js via login_tokens.json)
// ════════════════════════════════════════════════════════════════════════════

function createLoginToken(uid) {
  const token = crypto.randomBytes(32).toString('base64url');
  let data = {};
  try { data = JSON.parse(fs.readFileSync(LT_FILE, 'utf8')); } catch { /* empty */ }

  // Remove all existing tokens for this user and any expired ones.
  const now = Math.floor(Date.now() / 1000);
  for (const [t, v] of Object.entries(data)) {
    if (v.uid === uid || v.expires < now) delete data[t];
  }

  data[token] = { uid, expires: now + LOGIN_TTL };
  fs.writeFileSync(LT_FILE, JSON.stringify(data), 'utf8');
  return token;
}

// ════════════════════════════════════════════════════════════════════════════
//  TRANSLATIONS
// ════════════════════════════════════════════════════════════════════════════
const T = {
  ru: {
    start: (name) =>
      `🎬 <b>Добро пожаловать, ${name}!</b>\n\nСкачиваю видео с <b>YouTube, TikTok, Instagram, Twitter</b> и ещё сотен платформ.\n\n✨ <b>Бесплатно</b> — без лимитов\n👑 <b>Premium</b> — приоритет и поддержка\n\n📎 <i>Просто кинь ссылку — сделаю всё сам!</i>`,
    help: `📋 <b>Команды:</b>\n\n🎬 Отправь ссылку на видео\n🔍 /search yt запрос — поиск YouTube\n🔍 /search tt запрос — поиск TikTok\n/sub — 👑 Premium подписка\n/profile — 👤 Мой профиль\n/settings — ⚙️ Настройки\n/ticket — 🎫 Поддержка (Premium)\n/help — 📋 Справка`,
    analyzing:    '⏳ Анализирую...',
    err_url:      '❌ Не удалось получить данные о видео.\n<i>Попробуй другую ссылку.</i>',
    choose_q:     (title) => `🎥 <b>${title}</b>\n\n<i>Выбери качество:</i>`,
    downloading:  '⬇️ Загружаю… Жди немного',
    sending:      '📤 Отправляю в Telegram…',
    done_cap:     `✅ @${BOT_USERNAME}`,
    err_dl:       (err) => `❌ Ошибка: <code>${err}</code>`,
    audio_only:   '🎵 Только аудио (MP3)',
    sub_none:     '📊 Статус: Бесплатный аккаунт',
    sub_active:   (date) => `📊 Статус: 👑 <b>Premium</b> до <b>${date}</b>`,
    sub_lifetime: '📊 Статус: 👑 <b>Premium навсегда</b> ✨',
    sub_menu:     (status) => `👑 <b>Premium подписка</b>\n\nЧто входит:\n┣ ⚡ Приоритетная загрузка\n┣ 🎛️ Расширенные настройки\n┗ 🎫 Прямая линия поддержки\n\n${status}`,
    trial_used:   '✅ использован',
    profile:      (u, status, joined) => `👤 <b>Профиль</b>\n\n🆔 <code>${u.user_id}</code>  •  ${u.first_name}\n📊 ${status}\n\n📥 Скачано: <b>${u.downloads}</b>\n⭐ Потрачено звёзд: <b>${u.stars_spent}</b>\n📅 С нами с: <b>${joined}</b>`,
    p_active:     (date) => `👑 Premium до ${date}`,
    p_lifetime:   '👑 Premium навсегда ✨',
    p_none:       '🆓 Бесплатный',
    settings:     '⚙️ <b>Настройки</b>\n\nНастрой бота под себя:',
    on: '✅ Вкл', off: '❌ Выкл',
    ticket_prem:  '👑 Поддержка доступна только Premium пользователям\n\nПолучить: /sub',
    ticket_ask:   '✉️ <b>Напиши сообщение</b> — я передам его в поддержку:\n\n<i>Следующее сообщение станет тикетом</i>',
    ticket_sent:  '✅ <b>Тикет отправлен!</b> Ответим как можно скорее.',
    ticket_adm:   (tid, name, uid, msg) => `🎫 <b>Тикет #${tid}</b>\nОт: ${name}  |  <code>${uid}</code>\n\n${msg}`,
    stats:        (s) => `📊 <b>Статистика бота</b>\n\n👥 Пользователей: <b>${s.total}</b>\n👑 Premium: <b>${s.premium}</b>\n🆕 Сегодня: <b>${s.today}</b>\n📥 Скачиваний: <b>${s.downloads}</b>\n⭐ Звёзд собрано: <b>${s.stars}</b>`,
    bc_ask:       '📢 <b>Рассылка</b>\n\nОтправь текст (HTML разрешён):\n<i>Следующее сообщение уйдёт всем</i>',
    bc_done:      (n) => `✅ Разослано <b>${n}</b> пользователям`,
    no_admin:     '❌ Недостаточно прав',
    gift_ask:     '🎁 <b>Кому подарить Premium?</b>\n\nОтправь <code>@username</code> или числовой ID пользователя:',
    gift_404:     '❌ Пользователь не найден. Убедись, что он уже запускал бота.',
    gift_self:    '😅 Нельзя подарить самому себе!',
    gift_sel:     (to) => `🎁 Подарить <b>@${to}</b>\n\nВыбери период:`,
    gift_ok:      (to) => `🎉 Подарок отправлен <b>@${to}</b>!`,
    gift_recv:    (from, period) => `🎁 <b>Тебе подарили Premium!</b>\n\nОт: ${from}\nПериод: ${period}\n\n👑 Наслаждайся!`,
    paid_trial:   '🎉 <b>Пробный Premium активирован!</b>\n\n👑 7 дней без ограничений — пользуйся!',
    paid_month:   (n, mw) => `🎉 <b>Premium активирован на ${n} ${mw}!</b>\n\n👑 Enjoy!`,
    paid_life:    '🎉 <b>Вечный Premium активирован! ✨</b>\n\n👑 Ты навсегда с нами!',
    search_usage: '🔍 <b>Поиск видео</b>\n\nИспользование:\n<code>/search yt запрос</code> — YouTube\n<code>/search tt запрос</code> — TikTok\n\nПример: <code>/search yt смешные коты</code>',
    search_searching: (q, p) => `🔍 Ищу <b>${q}</b> на ${p}…`,
    search_no_results:(q) => `😕 Ничего не нашёл по запросу <b>${q}</b>.`,
    search_results:   (q) => `🔍 <b>Результаты: ${q}</b>\n\nВыбери видео для скачивания:`,
    search_limit_free:(lim) => `⚠️ Лимит исчерпан!\n\nБесплатно: <b>${lim} скачиваний/день</b> через поиск.\n\n👑 Premium даёт <b>12/день</b> → /sub`,
    search_limit_prem:(lim) => `⚠️ Лимит исчерпан!\n\nPremium: <b>${lim} скачиваний/день</b> через поиск.\nПриходи завтра 😊`,
    too_big:      (mb) => `⚠️ Файл слишком большой (${mb} МБ) — Telegram не принимает файлы >50 МБ.\nПопробуй выбрать качество пониже.`,
    promo: `💡 <b>Устал от ограничений?</b>\n\nС <b>Premium</b> ты получаешь:\n┣ 🎫 Прямой чат с поддержкой\n┣ ⚡ Приоритетная загрузка\n┗ ⚙️ Расширенные настройки\n\n🎁 Попробуй <b>7 дней за 5⭐</b>!`,
  },
  en: {
    start: (name) =>
      `🎬 <b>Welcome, ${name}!</b>\n\nI download videos from <b>YouTube, TikTok, Instagram, Twitter</b> and hundreds of other platforms.\n\n✨ <b>Free</b> — no limits\n👑 <b>Premium</b> — priority & support\n\n📎 <i>Just send a link — I'll handle the rest!</i>`,
    help: `📋 <b>Commands:</b>\n\n🎬 Send a video link\n🔍 /search yt query — YouTube search\n🔍 /search tt query — TikTok search\n/sub — 👑 Premium subscription\n/profile — 👤 My profile\n/settings — ⚙️ Settings\n/ticket — 🎫 Support (Premium)\n/help — 📋 Help`,
    analyzing:    '⏳ Analyzing...',
    err_url:      '❌ Couldn\'t get video info.\n<i>Try a different link.</i>',
    choose_q:     (title) => `🎥 <b>${title}</b>\n\n<i>Choose quality:</i>`,
    downloading:  '⬇️ Downloading… Please wait',
    sending:      '📤 Sending to Telegram…',
    done_cap:     `✅ @${BOT_USERNAME}`,
    err_dl:       (err) => `❌ Error: <code>${err}</code>`,
    audio_only:   '🎵 Audio only (MP3)',
    sub_none:     '📊 Status: Free account',
    sub_active:   (date) => `📊 Status: 👑 <b>Premium</b> until <b>${date}</b>`,
    sub_lifetime: '📊 Status: 👑 <b>Premium forever</b> ✨',
    sub_menu:     (status) => `👑 <b>Premium Subscription</b>\n\nWhat's included:\n┣ ⚡ Priority downloads\n┣ 🎛️ Advanced settings\n┗ 🎫 Direct support\n\n${status}`,
    trial_used:   '✅ used',
    profile:      (u, status, joined) => `👤 <b>Profile</b>\n\n🆔 <code>${u.user_id}</code>  •  ${u.first_name}\n📊 ${status}\n\n📥 Downloads: <b>${u.downloads}</b>\n⭐ Stars spent: <b>${u.stars_spent}</b>\n📅 Member since: <b>${joined}</b>`,
    p_active:     (date) => `👑 Premium until ${date}`,
    p_lifetime:   '👑 Premium forever ✨',
    p_none:       '🆓 Free',
    settings:     '⚙️ <b>Settings</b>\n\nCustomise the bot:',
    on: '✅ On', off: '❌ Off',
    ticket_prem:  '👑 Support is available to Premium users only.\n\nGet it: /sub',
    ticket_ask:   '✉️ <b>Send your message</b> — I\'ll forward it to support:\n\n<i>Your next message will become a ticket</i>',
    ticket_sent:  '✅ <b>Ticket sent!</b> We\'ll get back to you soon.',
    ticket_adm:   (tid, name, uid, msg) => `🎫 <b>Ticket #${tid}</b>\nFrom: ${name}  |  <code>${uid}</code>\n\n${msg}`,
    stats:        (s) => `📊 <b>Bot stats</b>\n\n👥 Users: <b>${s.total}</b>\n👑 Premium: <b>${s.premium}</b>\n🆕 Today: <b>${s.today}</b>\n📥 Downloads: <b>${s.downloads}</b>\n⭐ Stars earned: <b>${s.stars}</b>`,
    bc_ask:       '📢 <b>Broadcast</b>\n\nSend your message (HTML allowed):\n<i>Your next message will be sent to all users</i>',
    bc_done:      (n) => `✅ Sent to <b>${n}</b> users`,
    no_admin:     '❌ Access denied',
    gift_ask:     '🎁 <b>Who to gift Premium to?</b>\n\nSend <code>@username</code> or numeric user ID:',
    gift_404:     '❌ User not found. Make sure they have started the bot.',
    gift_self:    '😅 You can\'t gift yourself!',
    gift_sel:     (to) => `🎁 Gift <b>@${to}</b>\n\nChoose period:`,
    gift_ok:      (to) => `🎉 Gift sent to <b>@${to}</b>!`,
    gift_recv:    (from, period) => `🎁 <b>You received a Premium gift!</b>\n\nFrom: ${from}\nPeriod: ${period}\n\n👑 Enjoy!`,
    paid_trial:   '🎉 <b>Trial Premium activated!</b>\n\n👑 7 days of Premium — enjoy!',
    paid_month:   (n, mw) => `🎉 <b>Premium activated for ${n} ${mw}!</b>\n\n👑 Enjoy!`,
    paid_life:    '🎉 <b>Lifetime Premium activated! ✨</b>\n\n👑 Welcome to the club!',
    search_usage: '🔍 <b>Video search</b>\n\nUsage:\n<code>/search yt query</code> — YouTube\n<code>/search tt query</code> — TikTok\n\nExample: <code>/search yt funny cats</code>',
    search_searching: (q, p) => `🔍 Searching <b>${q}</b> on ${p}…`,
    search_no_results:(q) => `😕 No results found for <b>${q}</b>.`,
    search_results:   (q) => `🔍 <b>Results: ${q}</b>\n\nChoose a video to download:`,
    search_limit_free:(lim) => `⚠️ Limit reached!\n\nFree: <b>${lim} downloads/day</b> via search.\n\n👑 Premium gives <b>12/day</b> → /sub`,
    search_limit_prem:(lim) => `⚠️ Limit reached!\n\nPremium: <b>${lim} downloads/day</b> via search.\nCome back tomorrow 😊`,
    too_big:      (mb) => `⚠️ File too large (${mb} MB) — Telegram doesn't accept files >50 MB.\nTry selecting a lower quality.`,
    promo: `💡 <b>Download faster with Premium!</b>\n\nWith <b>Premium</b> you get:\n┣ 🎫 Direct chat with support\n┣ ⚡ Priority downloads\n┗ ⚙️ Advanced settings\n\n🎁 Try <b>7 days for 5⭐</b>!`,
  },
};

function getLang(uid)  { const u = dbGet(uid); return (u && u.language) || 'ru'; }
function t(uid, key, ...args) {
  const lang = getLang(uid);
  const d    = T[lang] || T.ru;
  const val  = d[key]  ?? T.ru[key] ?? `[${key}]`;
  return typeof val === 'function' ? val(...args) : val;
}

function mword(n, lang) {
  if (lang === 'en') return n === 1 ? 'month' : 'months';
  if (n % 10 === 1 && n % 100 !== 11)                          return 'месяц';
  if ([2,3,4].includes(n % 10) && ![12,13,14].includes(n % 100)) return 'месяца';
  return 'месяцев';
}

function calcPrice(months) { return MONTHLY_BASE + MONTHLY_EXTRA * (months - 1); }

function fmtDate(ts) {
  return new Date(ts * 1000).toLocaleDateString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric' });
}

function premiumLabel(uid) {
  if (ADMIN_IDS.has(uid)) return getLang(uid) === 'ru' ? '👑 <b>Premium навсегда</b> ✨  👨‍💻' : '👑 <b>Premium forever</b> ✨  👨‍💻';
  const u = dbGet(uid);
  if (!u) return t(uid, 'p_none');
  if (u.premium_until === -1)                               return t(uid, 'p_lifetime');
  if (u.premium_until > Math.floor(Date.now() / 1000))     return t(uid, 'p_active', fmtDate(u.premium_until));
  return t(uid, 'p_none');
}

function subStatus(uid) {
  const u   = dbGet(uid);
  const now = Math.floor(Date.now() / 1000);
  if (!u || u.premium_until === 0) return t(uid, 'sub_none');
  if (u.premium_until === -1)      return t(uid, 'sub_lifetime');
  if (u.premium_until > now)       return t(uid, 'sub_active', fmtDate(u.premium_until));
  return t(uid, 'sub_none');
}

// ════════════════════════════════════════════════════════════════════════════
//  KEYBOARDS
// ════════════════════════════════════════════════════════════════════════════

function btn(text, cb) { return [{ text, callback_data: cb }]; }

function kbMain(uid) {
  const lang = getLang(uid);
  return { inline_keyboard: [
    [{ text: '👑 Premium', callback_data: 'sub' },
     { text: lang === 'ru' ? '👤 Профиль' : '👤 Profile', callback_data: 'profile' }],
    [{ text: lang === 'ru' ? '⚙️ Настройки' : '⚙️ Settings', callback_data: 'settings' }],
  ]};
}

function kbSub(uid, months = 1) {
  const u     = dbGet(uid);
  const lang  = (u && u.language) || 'ru';
  const mw    = mword(months, lang);
  const stars = calcPrice(months);
  const rows  = [];

  if (!u || !u.trial_used) {
    rows.push(btn(`🎁 ${lang === 'ru' ? 'Пробный' : 'Trial'} — ${TRIAL_STARS}⭐ (7 ${lang === 'ru' ? 'дней' : 'days'})`, 'pay_trial'));
  } else {
    rows.push(btn(`🎁 ${lang === 'ru' ? 'Пробный' : 'Trial'} — ${t(uid, 'trial_used')}`, 'noop'));
  }

  rows.push([
    { text: '➖', callback_data: `subm_dec_${months}` },
    { text: `📅 ${months} ${mw}  =  ${stars}⭐`, callback_data: 'noop' },
    { text: '➕', callback_data: `subm_inc_${months}` },
  ]);
  rows.push(btn(`💳 ${lang === 'ru' ? 'Купить' : 'Buy'} ${months} ${mw} — ${stars}⭐`, `pay_months_${months}`));
  rows.push(btn(`♾️ ${lang === 'ru' ? 'Навсегда' : 'Forever'} — ${LIFETIME_STARS}⭐`, 'pay_lifetime'));
  rows.push(btn(`🎁 ${lang === 'ru' ? 'Подарить Premium' : 'Gift Premium'}`, 'gift_start'));
  rows.push(btn(`◀️ ${lang === 'ru' ? 'Назад' : 'Back'}`, 'back_main'));

  return { inline_keyboard: rows };
}

function kbGiftSelect(uid, toUid, months = 1) {
  const lang  = getLang(uid);
  const mw    = mword(months, lang);
  const stars = calcPrice(months);
  return { inline_keyboard: [
    btn(`🎁 7 ${lang === 'ru' ? 'дней' : 'days'} — ${TRIAL_STARS}⭐`, `gpay_trial_${toUid}`),
    [
      { text: '➖', callback_data: `gsubm_dec_${months}_${toUid}` },
      { text: `📅 ${months} ${mw}  =  ${stars}⭐`, callback_data: 'noop' },
      { text: '➕', callback_data: `gsubm_inc_${months}_${toUid}` },
    ],
    btn(`💳 ${lang === 'ru' ? 'Подарить' : 'Gift'} ${months} ${mw} — ${stars}⭐`, `gpay_months_${months}_${toUid}`),
    btn(`♾️ ${lang === 'ru' ? 'Навсегда' : 'Forever'} — ${LIFETIME_STARS}⭐`, `gpay_life_${toUid}`),
    btn('◀️', 'sub'),
  ]};
}

function kbSettings(uid) {
  const u    = dbGet(uid);
  const lang = (u && u.language) || 'ru';
  const auto = u && u.auto_dl;
  const flag = lang === 'ru' ? '🇷🇺 Русский' : '🇺🇸 English';
  const on   = t(uid, 'on');
  const off  = t(uid, 'off');
  return { inline_keyboard: [
    btn(`🌍 ${lang === 'ru' ? 'Язык' : 'Language'}: ${flag}`, 'set_lang'),
    btn(`⬇️ ${lang === 'ru' ? 'Авто-загрузка' : 'Auto-download'}: ${auto ? on : off}`, 'set_auto'),
    btn(`◀️ ${lang === 'ru' ? 'Назад' : 'Back'}`, 'back_main'),
  ]};
}

function kbAdmin(uid) {
  const lang = getLang(uid);
  return { inline_keyboard: [[
    { text: lang === 'ru' ? '📊 Статистика' : '📊 Stats',    callback_data: 'admin_stats' },
    { text: lang === 'ru' ? '📢 Рассылка'  : '📢 Broadcast', callback_data: 'admin_bc'    },
  ]]};
}

// ════════════════════════════════════════════════════════════════════════════
//  YT-DLP  (child_process.spawn)
// ════════════════════════════════════════════════════════════════════════════

function ytdlp(args, timeoutMs = 300_000) {
  return new Promise((resolve, reject) => {
    const out = [], err = [];
    const proc = spawn('yt-dlp', args, { stdio: ['ignore', 'pipe', 'pipe'] });
    let timedOut = false;
    const timer = setTimeout(() => { timedOut = true; proc.kill('SIGTERM'); }, timeoutMs);
    proc.stdout.on('data', c => out.push(c));
    proc.stderr.on('data', c => err.push(c));
    proc.on('error', e => { clearTimeout(timer); reject(e); });
    proc.on('close', code => {
      clearTimeout(timer);
      if (timedOut) return reject(new Error('yt-dlp timed out'));
      resolve({ stdout: Buffer.concat(out).toString('utf8'), stderr: Buffer.concat(err).toString('utf8'), code });
    });
  });
}

function fmtDuration(sec) {
  if (!sec) return '';
  sec = Math.floor(sec);
  const h = Math.floor(sec / 3600), m = Math.floor((sec % 3600) / 60), s = sec % 60;
  return h ? `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}` : `${m}:${String(s).padStart(2,'0')}`;
}

function findFiles(prefix) {
  try {
    return fs.readdirSync(DL_DIR)
      .filter(f => f.startsWith(prefix + '.') && !f.endsWith('.part'))
      .map(f => path.join(DL_DIR, f))
      .filter(fp => { try { return fs.statSync(fp).size > 1024; } catch { return false; } });
  } catch { return []; }
}

async function getInfo(url) {
  const { stdout, code } = await ytdlp(['--dump-json','--no-playlist','--quiet','--no-warnings', url], 60_000);
  if (code !== 0 || !stdout.trim()) return null;
  try { return JSON.parse(stdout.trim()); } catch { return null; }
}

// Format selector: prefer single-file (no ffmpeg needed), best quality first.
const VIDEO_FMT = (
  'best[vcodec!=none][acodec!=none][ext=mp4]' +
  '/best[vcodec!=none][acodec!=none]' +
  '/best[ext=mp4]/best'
);
const AUDIO_FMT = 'bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio';

async function downloadFile(url, mode = 'video') {
  const prefix = `v_${Date.now()}`;
  const outTpl = path.join(DL_DIR, `${prefix}.%(ext)s`);
  const fmt    = mode === 'audio' ? AUDIO_FMT : VIDEO_FMT;

  const { code } = await ytdlp([
    '--output', outTpl,
    '--format', fmt,
    '--no-playlist', '--quiet', '--no-warnings',
    '--fragment-retries', '3', '--retries', '3',
    '--extractor-args', 'youtube:player_client=android_embedded,web',
    url,
  ], 600_000);

  if (code !== 0) {
    // Fallback attempt with explicit best
    await ytdlp(['--output', outTpl, '--format', 'best', '--no-playlist', '--quiet', url], 600_000)
      .catch(() => {});
  }

  const files = findFiles(prefix);
  return files.length ? files.sort((a,b) => fs.statSync(b).mtimeMs - fs.statSync(a).mtimeMs)[0] : null;
}

function isPhotoPost(info) {
  if (!info) return false;
  const fmts     = info.formats || [];
  const imgExts  = new Set(['jpg','jpeg','png','webp']);
  const hasVideo = fmts.some(f => f.vcodec && f.vcodec !== 'none' && !imgExts.has(f.ext));
  const hasImgs  = fmts.some(f => imgExts.has(f.ext));
  return hasImgs && !hasVideo;
}

async function downloadPhotos(url) {
  const prefix = `p_${Date.now()}`;
  const outTpl = path.join(DL_DIR, `${prefix}_%(autonumber)s.%(ext)s`);
  await ytdlp(['--output', outTpl, '--format', 'best', '--quiet', '--no-warnings', url], 120_000).catch(()=>{});
  const imgExts = ['.jpg','.jpeg','.png','.webp'];
  return fs.readdirSync(DL_DIR)
    .filter(f => f.startsWith(prefix) && imgExts.some(e => f.endsWith(e)))
    .map(f => path.join(DL_DIR, f))
    .sort();
}

async function searchVideos(query, platform, maxResults = 5) {
  const url = platform === 'yt' ? `ytsearch${maxResults}:${query}` : `tiktoksearch${maxResults}:${query}`;
  const { stdout } = await ytdlp(['--flat-playlist','--dump-single-json','--quiet','--no-warnings', url], 60_000).catch(() => ({ stdout: '' }));
  if (!stdout.trim()) return [];
  let info; try { info = JSON.parse(stdout.trim()); } catch { return []; }
  const results = [];
  for (const e of (info.entries || [])) {
    if (!e) continue;
    let u = e.url || e.webpage_url;
    if (!u) {
      const id = e.id;
      if (platform === 'yt' && id)  u = `https://www.youtube.com/watch?v=${id}`;
      else if (id)                  u = `https://www.tiktok.com/@${e.uploader||'user'}/video/${id}`;
    }
    if (!u) continue;
    const vc = e.view_count || 0;
    const views = vc >= 1e6 ? `${(vc/1e6).toFixed(1)}M 👁` : vc >= 1e3 ? `${Math.floor(vc/1000)}K 👁` : vc ? `${vc} 👁` : '';
    results.push({ title: (e.title || 'Unknown').slice(0, 60), url: u, duration: fmtDuration(e.duration), views });
  }
  return results;
}

// ════════════════════════════════════════════════════════════════════════════
//  FILE CLEANUP
// ════════════════════════════════════════════════════════════════════════════

function cleanup() {
  const cutoff = Date.now() - 3_600_000;
  try {
    for (const f of fs.readdirSync(DL_DIR)) {
      const fp = path.join(DL_DIR, f);
      try { if (fs.statSync(fp).mtimeMs < cutoff) fs.unlinkSync(fp); } catch {}
    }
  } catch {}
}
cleanup();
setInterval(cleanup, 5 * 60_000).unref();

function safeDelete(fp) { try { if (fp && fs.existsSync(fp)) fs.unlinkSync(fp); } catch {} }

// ════════════════════════════════════════════════════════════════════════════
//  BOT INSTANCE
// ════════════════════════════════════════════════════════════════════════════

const bot = new TelegramBot(BOT_TOKEN, { polling: true });

// Per-user state: awaiting_broadcast, awaiting_ticket, awaiting_gift_target,
// pending_downloads (quality picker), search_results, gift_to_uid
const userState = new Map(); // uid → { ... }

function getState(uid) {
  if (!userState.has(uid)) userState.set(uid, {});
  return userState.get(uid);
}
function setState(uid, patch) {
  const s = getState(uid);
  Object.assign(s, patch);
}
function clearState(uid, ...keys) {
  const s = getState(uid);
  for (const k of keys) delete s[k];
}

// ─── Send helpers ─────────────────────────────────────────────────────────────

function send(chatId, text, extra = {}) {
  return bot.sendMessage(chatId, text, { parse_mode: 'HTML', ...extra }).catch(e => log('warn', 'sendMessage:', e.message));
}
function edit(chatId, msgId, text, extra = {}) {
  return bot.editMessageText(text, { chat_id: chatId, message_id: msgId, parse_mode: 'HTML', ...extra }).catch(e => log('warn', 'editMessageText:', e.message));
}
function editKb(chatId, msgId, kb) {
  return bot.editMessageReplyMarkup(kb, { chat_id: chatId, message_id: msgId }).catch(e => log('warn', 'editMarkup:', e.message));
}

// ════════════════════════════════════════════════════════════════════════════
//  COMMAND HANDLERS
// ════════════════════════════════════════════════════════════════════════════

// /start
bot.onText(/\/start(.*)/, async (msg, match) => {
  const uid  = msg.from.id;
  const arg  = (match[1] || '').trim().replace(/^_/, ''); // /start webapp or /start sub
  dbUpsert(uid, msg.from.username || '', msg.from.first_name || '');

  if (arg === 'webapp') {
    const token = createLoginToken(uid);
    const url   = `${SITE_URL}/login/${token}`;
    return send(uid,
      '🔑 <b>Ссылка для входа на сайт</b>\n\n⏱ Срок действия: <b>15 мин.</b>\nОткройте ссылку — вход произойдет автоматически.\n\n⚠️ Не передавайте ссылку другим!',
      { reply_markup: { inline_keyboard: [[{ text: '🔓 Открыть сайт', url }]] } }
    );
  }

  if (arg === 'sub') {
    return send(uid, t(uid, 'sub_menu', subStatus(uid)), { reply_markup: kbSub(uid) });
  }

  return send(uid, t(uid, 'start', msg.from.first_name || 'друг'), { reply_markup: kbMain(uid) });
});

// /help
bot.onText(/\/help/, (msg) => {
  const uid = msg.from.id;
  dbUpsert(uid, msg.from.username || '', msg.from.first_name || '');
  send(uid, t(uid, 'help'));
});

// /sub
bot.onText(/\/sub/, (msg) => {
  const uid = msg.from.id;
  dbUpsert(uid, msg.from.username || '', msg.from.first_name || '');
  send(uid, t(uid, 'sub_menu', subStatus(uid)), { reply_markup: kbSub(uid) });
});

// /profile
bot.onText(/\/profile/, (msg) => {
  const uid = msg.from.id;
  dbUpsert(uid, msg.from.username || '', msg.from.first_name || '');
  const u = dbGet(uid);
  const joined = u && u.joined_at ? fmtDate(u.joined_at) : '?';
  send(uid, t(uid, 'profile', u || { user_id: uid, first_name: '', downloads: 0, stars_spent: 0 }, premiumLabel(uid), joined));
});

// /settings
bot.onText(/\/settings/, (msg) => {
  const uid = msg.from.id;
  dbUpsert(uid, msg.from.username || '', msg.from.first_name || '');
  send(uid, t(uid, 'settings'), { reply_markup: kbSettings(uid) });
});

// /ticket
bot.onText(/\/ticket/, (msg) => {
  const uid = msg.from.id;
  dbUpsert(uid, msg.from.username || '', msg.from.first_name || '');
  if (!isPremium(uid)) return send(uid, t(uid, 'ticket_prem'));
  setState(uid, { awaiting_ticket: true });
  send(uid, t(uid, 'ticket_ask'));
});

// /stats / /admin
bot.onText(/\/(stats|admin)/, (msg) => {
  const uid = msg.from.id;
  if (!ADMIN_IDS.has(uid)) return send(uid, t(uid, 'no_admin'));
  const s = dbStats();
  send(uid, t(uid, 'stats', s), { reply_markup: kbAdmin(uid) });
});

// /broadcast
bot.onText(/\/broadcast/, (msg) => {
  const uid = msg.from.id;
  if (!ADMIN_IDS.has(uid)) return send(uid, t(uid, 'no_admin'));
  setState(uid, { awaiting_broadcast: true });
  send(uid, t(uid, 'bc_ask'));
});

// /search [yt|tt] query
bot.onText(/\/search(.*)/, async (msg, match) => {
  const uid  = msg.from.id;
  dbUpsert(uid, msg.from.username || '', msg.from.first_name || '');
  const args = (match[1] || '').trim().split(/\s+/);

  if (args.length < 2) return send(uid, t(uid, 'search_usage'));

  const rawPlat = args[0].toLowerCase();
  const platform = rawPlat === 'yt' || rawPlat === 'youtube' ? 'yt'
                 : rawPlat === 'tt' || rawPlat === 'tiktok'  ? 'tt' : null;
  if (!platform) return send(uid, t(uid, 'search_usage'));

  const query    = args.slice(1).join(' ');
  const platName = platform === 'yt' ? 'YouTube' : 'TikTok';

  const waitMsg  = await bot.sendMessage(uid, t(uid, 'search_searching', query, platName), { parse_mode: 'HTML' });
  const results  = await searchVideos(query, platform, 5).catch(() => []);

  if (!results.length) {
    return edit(uid, waitMsg.message_id, t(uid, 'search_no_results', query));
  }

  const prem  = isPremium(uid);
  const limit = prem ? PREM_SEARCH_DL : FREE_SEARCH_DL;
  const used  = dbGetSearchDl(uid);

  const lines = results.map((r, i) => {
    const meta = [r.duration && `⏱ ${r.duration}`, r.views].filter(Boolean).join('  ');
    return `${i + 1}. <b>${r.title}</b>${meta ? `\n   ${meta}` : ''}`;
  });

  let text = t(uid, 'search_results', query) + '\n\n' + lines.join('\n\n');
  if (used < limit) text += `\n\n📊 ${getLang(uid) === 'ru' ? 'Осталось скачиваний через поиск сегодня' : 'Search downloads left today'}: <b>${limit - used}/${limit}</b>`;

  // Store results for callback handler.
  setState(uid, { search_results: results, search_platform: platform, search_chat: msg.chat.id });

  const kb = { inline_keyboard: results.map((r, i) => [{
    text: `⬇️ ${i + 1}. ${r.title.slice(0, 32)}${r.title.length > 32 ? '…' : ''}`,
    callback_data: `sdl_${i}`,
  }])};

  edit(uid, waitMsg.message_id, text, { reply_markup: kb });
});

// ════════════════════════════════════════════════════════════════════════════
//  MESSAGE HANDLER  (URLs + awaiting states)
// ════════════════════════════════════════════════════════════════════════════

const URL_RE = /https?:\/\/\S+/;

bot.on('message', async (msg) => {
  if (!msg.text || msg.text.startsWith('/')) return; // commands handled above
  const uid  = msg.from.id;
  const text = msg.text;
  dbUpsert(uid, msg.from.username || '', msg.from.first_name || '');
  const s    = getState(uid);

  // ── Admin broadcast ───────────────────────────────────────────────────────
  if (s.awaiting_broadcast && ADMIN_IDS.has(uid)) {
    clearState(uid, 'awaiting_broadcast');
    const uids = dbAllUids();
    let sent = 0;
    for (const id of uids) {
      try { await bot.sendMessage(id, text, { parse_mode: 'HTML' }); sent++; } catch {}
      await new Promise(r => setTimeout(r, 40)); // 25 msg/s rate limit
    }
    return send(uid, t(uid, 'bc_done', sent));
  }

  // ── Support ticket ────────────────────────────────────────────────────────
  if (s.awaiting_ticket) {
    clearState(uid, 'awaiting_ticket');
    if (isPremium(uid)) {
      const tid = dbAddTicket(uid, text);
      await send(uid, t(uid, 'ticket_sent'));
      for (const aid of ADMIN_IDS) {
        send(aid, t(uid, 'ticket_adm', tid, msg.from.first_name, uid, text)).catch(() => {});
      }
    }
    return;
  }

  // ── Gift target ───────────────────────────────────────────────────────────
  if (s.awaiting_gift_target) {
    clearState(uid, 'awaiting_gift_target');
    const target = text.trim();
    let toUser = null;
    if (/^\d+$/.test(target.replace('@', ''))) {
      toUser = dbGet(parseInt(target.replace('@', ''), 10));
    } else {
      toUser = dbByUsername(target);
    }
    if (!toUser) return send(uid, t(uid, 'gift_404'));
    if (toUser.user_id === uid) return send(uid, t(uid, 'gift_self'));
    setState(uid, { gift_to_uid: toUser.user_id });
    const toName = toUser.username || toUser.first_name || String(toUser.user_id);
    return send(uid, t(uid, 'gift_sel', toName), { reply_markup: kbGiftSelect(uid, toUser.user_id) });
  }

  // ── URL detection ─────────────────────────────────────────────────────────
  const match = URL_RE.exec(text);
  if (!match) return;
  const url = match[0];

  const chatType = msg.chat.type;
  const isGroup  = chatType === 'group' || chatType === 'supergroup' || chatType === 'channel';
  const threadId = msg.message_thread_id;
  const chatId   = msg.chat.id;

  const waitMsg = await bot.sendMessage(chatId, isGroup ? '⏳ Скачиваю...' : t(uid, 'analyzing'), {
    parse_mode: 'HTML',
    ...(threadId ? { message_thread_id: threadId } : {}),
  });

  const info = await getInfo(url);
  if (!info) {
    return edit(chatId, waitMsg.message_id, t(uid, 'err_url'));
  }

  // ── TikTok photo slideshow ────────────────────────────────────────────────
  if (isPhotoPost(info)) {
    await edit(chatId, waitMsg.message_id, t(uid, 'downloading'));
    const photos = await downloadPhotos(url);
    if (!photos.length) return edit(chatId, waitMsg.message_id, t(uid, 'err_url'));

    try { await bot.deleteMessage(chatId, waitMsg.message_id); } catch {}

    const cap  = t(uid, 'done_cap');
    const sendKw = threadId ? { message_thread_id: threadId } : {};
    if (photos.length === 1) {
      await bot.sendPhoto(chatId, photos[0], { caption: cap, ...sendKw }).catch(e => log('warn', 'sendPhoto:', e.message));
    } else {
      const media = photos.map((p, i) => ({ type: 'photo', media: fs.createReadStream(p), caption: i === 0 ? cap : undefined }));
      await bot.sendMediaGroup(chatId, media, sendKw).catch(e => log('warn', 'sendMediaGroup:', e.message));
    }
    for (const p of photos) safeDelete(p);
    dbIncDl(uid);
    return;
  }

  // ── Group: auto download best video + audio ───────────────────────────────
  if (isGroup) {
    const fmts   = (info.formats || []).filter(f => f.height);
    const bestFmt = fmts.sort((a, b) => (b.height || 0) - (a.height || 0))[0];
    const sendKw  = threadId ? { message_thread_id: threadId } : {};
    const cap     = t(uid, 'done_cap');

    await edit(chatId, waitMsg.message_id, '⬇️ Загружаю видео...');
    const videoFile = await downloadFile(url, 'video').catch(() => null);

    await edit(chatId, waitMsg.message_id, '⬇️ Загружаю аудио...');
    const audioFile = await downloadFile(url, 'audio').catch(() => null);

    await edit(chatId, waitMsg.message_id, '📤 Отправляю...');

    if (videoFile) {
      const sizeMb = fs.statSync(videoFile).size / 1024 / 1024;
      if (sizeMb <= 49) {
        await bot.sendVideo(chatId, fs.createReadStream(videoFile), { caption: cap, supports_streaming: true, ...sendKw })
          .catch(e => log('warn', 'sendVideo:', e.message));
      }
      safeDelete(videoFile);
    }
    if (audioFile) {
      await bot.sendAudio(chatId, fs.createReadStream(audioFile), { caption: cap, ...sendKw })
        .catch(e => log('warn', 'sendAudio:', e.message));
      safeDelete(audioFile);
    }

    try { await bot.deleteMessage(chatId, waitMsg.message_id); } catch {}
    dbIncDl(uid);
    return;
  }

  // ── Private: auto-download or quality picker ──────────────────────────────
  const userRow = dbGet(uid);
  if (userRow && userRow.auto_dl) {
    await edit(chatId, waitMsg.message_id, t(uid, 'downloading'));
    return doDownload(bot, uid, chatId, waitMsg.message_id, url, 'best', 1280, threadId);
  }

  // Build quality picker keyboard
  const formats = [];
  const seen    = new Set();
  for (const f of (info.formats || []).sort((a, b) => (b.height || 0) - (a.height || 0))) {
    const h = f.height;
    if (h && !seen.has(h) && h >= 360) {
      seen.add(h);
      formats.push({ id: f.format_id, height: h });
      if (formats.length >= 5) break;
    }
  }

  const kbRows = formats.map(f => [{ text: `📹 ${f.height}p`, callback_data: `dl_${uid}_${f.height}` }]);
  kbRows.push([{ text: t(uid, 'audio_only'), callback_data: `dl_${uid}_audio` }]);

  // Store for callback
  setState(uid, {
    pending_dl: {
      url,
      width: info.width || 1280,
      formats: formats.reduce((acc, f) => { acc[f.height] = f.id; return acc; }, {}),
      chatId,
      threadId,
    }
  });

  const title = (info.title || '').slice(0, 80);
  edit(chatId, waitMsg.message_id, t(uid, 'choose_q', title), { reply_markup: { inline_keyboard: kbRows } });
});

// ════════════════════════════════════════════════════════════════════════════
//  DOWNLOAD EXECUTOR  (called by quality picker and search-download callbacks)
// ════════════════════════════════════════════════════════════════════════════

async function doDownload(bot, uid, chatId, msgId, url, fmtId, width, threadId) {
  try {
    const mode = fmtId === 'audio' ? 'audio' : 'video';
    const file = await downloadFile(url, mode);

    if (!file) {
      return edit(chatId, msgId, t(uid, 'err_dl', 'file not found'));
    }

    await edit(chatId, msgId, t(uid, 'sending'));

    const sizeMb = fs.statSync(file).size / 1024 / 1024;
    if (sizeMb > 49 && !file.endsWith('.mp3')) {
      await edit(chatId, msgId, t(uid, 'too_big', Math.round(sizeMb)));
      safeDelete(file);
      return;
    }

    const cap     = t(uid, 'done_cap');
    const sendKw  = threadId ? { message_thread_id: threadId } : {};

    if (mode === 'audio') {
      await bot.sendAudio(chatId, fs.createReadStream(file), { caption: cap, ...sendKw });
    } else {
      await bot.sendVideo(chatId, fs.createReadStream(file), { caption: cap, supports_streaming: true, ...sendKw });
    }

    try { await bot.deleteMessage(chatId, msgId); } catch {}
    safeDelete(file);
    dbIncDl(uid);

    // Promo for free users every 3rd download
    const updatedUser = dbGet(uid);
    if (!isPremium(uid) && updatedUser && updatedUser.downloads % 3 === 0) {
      send(chatId, t(uid, 'promo'), {
        reply_markup: { inline_keyboard: [[{ text: getLang(uid) === 'ru' ? '👑 Попробовать Premium' : '👑 Try Premium', callback_data: 'sub' }]] },
        ...(threadId ? { message_thread_id: threadId } : {}),
      });
    }
  } catch (err) {
    log('error', '[doDownload]', err.message);
    edit(chatId, msgId, t(uid, 'err_dl', err.message.slice(0, 120))).catch(() => {});
  }
}

// ════════════════════════════════════════════════════════════════════════════
//  CALLBACK QUERY HANDLER
// ════════════════════════════════════════════════════════════════════════════

bot.on('callback_query', async (q) => {
  const uid    = q.from.id;
  const data   = q.data || '';
  const chatId = q.message.chat.id;
  const msgId  = q.message.message_id;

  await bot.answerCallbackQuery(q.id).catch(() => {});

  // ── No-op ──────────────────────────────────────────────────────────────────
  if (data === 'noop') return;

  // ── Quality picker: dl_{uid}_{height|audio} ────────────────────────────────
  if (data.startsWith('dl_')) {
    const parts  = data.split('_');
    const tgtUid = parseInt(parts[1], 10);
    if (tgtUid !== uid) return; // ignore buttons from other users

    const s = getState(uid);
    const pending = s.pending_dl;
    if (!pending) return edit(chatId, msgId, '❌ Данные устарели. Отправь ссылку снова.');

    const heightOrAudio = parts[2];
    let fmtId = 'best';
    if (heightOrAudio === 'audio') {
      fmtId = 'audio';
    } else {
      const h = parseInt(heightOrAudio, 10);
      fmtId = pending.formats[h] || 'best';
    }

    clearState(uid, 'pending_dl');
    await edit(chatId, msgId, t(uid, 'downloading'));
    return doDownload(bot, uid, chatId, msgId, pending.url, fmtId, pending.width, pending.threadId);
  }

  // ── Search download: sdl_{index} ───────────────────────────────────────────
  if (data.startsWith('sdl_')) {
    const idx     = parseInt(data.split('_')[1], 10);
    const s       = getState(uid);
    const results = s.search_results;
    if (!results || !results[idx]) return;

    const prem  = isPremium(uid);
    const limit = prem ? PREM_SEARCH_DL : FREE_SEARCH_DL;
    const used  = dbGetSearchDl(uid);

    if (used >= limit) {
      return send(uid, prem ? t(uid, 'search_limit_prem', limit) : t(uid, 'search_limit_free', limit));
    }

    const url     = results[idx].url;
    const waitMsg = await bot.sendMessage(uid, t(uid, 'downloading'), { parse_mode: 'HTML' });
    dbIncSearchDl(uid);
    return doDownload(bot, uid, uid, waitMsg.message_id, url, 'best', 1280, null);
  }

  // ── Sub menu ───────────────────────────────────────────────────────────────
  if (data === 'sub') {
    return edit(chatId, msgId, t(uid, 'sub_menu', subStatus(uid)), { reply_markup: kbSub(uid) });
  }

  // ── Sub month selector ────────────────────────────────────────────────────
  if (data.startsWith('subm_')) {
    const [, action, curS] = data.split('_');
    let cur = parseInt(curS, 10);
    cur = action === 'dec' ? Math.max(1, cur - 1) : Math.min(MAX_MONTHS, cur + 1);
    return editKb(chatId, msgId, kbSub(uid, cur));
  }

  // ── Profile ────────────────────────────────────────────────────────────────
  if (data === 'profile') {
    const u = dbGet(uid) || { user_id: uid, first_name: q.from.first_name, downloads: 0, stars_spent: 0, joined_at: 0 };
    const joined = u.joined_at ? fmtDate(u.joined_at) : '?';
    const lang   = getLang(uid);
    return edit(chatId, msgId, t(uid, 'profile', u, premiumLabel(uid), joined), {
      reply_markup: { inline_keyboard: [[
        { text: '👑 Premium', callback_data: 'sub' },
        { text: lang === 'ru' ? '⚙️ Настройки' : '⚙️ Settings', callback_data: 'settings' },
      ]]}
    });
  }

  // ── Settings ───────────────────────────────────────────────────────────────
  if (data === 'settings') return edit(chatId, msgId, t(uid, 'settings'), { reply_markup: kbSettings(uid) });

  if (data === 'set_lang') {
    dbSet(uid, 'language', getLang(uid) === 'ru' ? 'en' : 'ru');
    return edit(chatId, msgId, t(uid, 'settings'), { reply_markup: kbSettings(uid) });
  }
  if (data === 'set_auto') {
    const u = dbGet(uid);
    dbSet(uid, 'auto_dl', u && u.auto_dl ? 0 : 1);
    return editKb(chatId, msgId, kbSettings(uid));
  }

  // ── Back to main ──────────────────────────────────────────────────────────
  if (data === 'back_main') {
    dbUpsert(uid, q.from.username || '', q.from.first_name || '');
    return edit(chatId, msgId, t(uid, 'start', q.from.first_name || 'друг'), { reply_markup: kbMain(uid) });
  }

  // ── Payment: trial ────────────────────────────────────────────────────────
  if (data === 'pay_trial') {
    const u = dbGet(uid);
    if (u && u.trial_used) return bot.answerCallbackQuery(q.id, { text: t(uid, 'trial_used'), show_alert: true }).catch(() => {});
    return bot.sendInvoice(uid, t(uid, 'sub_menu', '').split('\n')[0].replace(/<[^>]+>/g,''),
      'Попробуй все возможности Premium на 7 дней!',
      JSON.stringify({ type: 'trial', uid }),
      'XTR', [{ label: 'Trial 7 days', amount: TRIAL_STARS }]
    ).catch(e => log('warn', 'sendInvoice:', e.message));
  }

  // ── Payment: monthly ──────────────────────────────────────────────────────
  if (data.startsWith('pay_months_')) {
    const months = parseInt(data.split('_')[2], 10);
    const lang   = getLang(uid);
    const mw     = mword(months, lang);
    const stars  = calcPrice(months);
    return bot.sendInvoice(uid, `👑 Premium на ${months} ${mw}`,
      `Premium на ${months} ${mw}. Скачивай без ограничений!`,
      JSON.stringify({ type: 'monthly', uid, months }),
      'XTR', [{ label: `Premium ${months}mo`, amount: stars }]
    ).catch(e => log('warn', 'sendInvoice:', e.message));
  }

  // ── Payment: lifetime ─────────────────────────────────────────────────────
  if (data === 'pay_lifetime') {
    return bot.sendInvoice(uid, '♾️ Premium навсегда',
      'Вечный доступ ко всем функциям бота!',
      JSON.stringify({ type: 'lifetime', uid }),
      'XTR', [{ label: 'Premium Lifetime', amount: LIFETIME_STARS }]
    ).catch(e => log('warn', 'sendInvoice:', e.message));
  }

  // ── Gift flow ─────────────────────────────────────────────────────────────
  if (data === 'gift_start') {
    setState(uid, { awaiting_gift_target: true });
    return edit(chatId, msgId, t(uid, 'gift_ask'));
  }

  if (data.startsWith('gsubm_')) {
    const [, action, curS, toUidS] = data.split('_');
    let cur = parseInt(curS, 10);
    cur = action === 'dec' ? Math.max(1, cur - 1) : Math.min(MAX_MONTHS, cur + 1);
    return editKb(chatId, msgId, kbGiftSelect(uid, parseInt(toUidS, 10), cur));
  }

  if (data.startsWith('gpay_trial_')) {
    const toUid  = parseInt(data.split('_')[2], 10);
    const toUser = dbGet(toUid);
    const toName = (toUser && (toUser.username || toUser.first_name)) || String(toUid);
    return bot.sendInvoice(uid, `🎁 Подарок: Premium 7 дней → @${toName}`,
      'Тебе дарят Premium в боте @PuweDownloaderBot!',
      JSON.stringify({ type: 'gift_trial', uid, to_uid: toUid }),
      'XTR', [{ label: `Gift Trial→${toName}`, amount: TRIAL_STARS }]
    ).catch(e => log('warn', 'sendInvoice:', e.message));
  }

  if (data.startsWith('gpay_months_')) {
    const parts  = data.split('_');
    const months = parseInt(parts[2], 10);
    const toUid  = parseInt(parts[3], 10);
    const lang   = getLang(uid);
    const mw     = mword(months, lang);
    const stars  = calcPrice(months);
    const toUser = dbGet(toUid);
    const toName = (toUser && (toUser.username || toUser.first_name)) || String(toUid);
    return bot.sendInvoice(uid, `🎁 Подарок: Premium ${months} ${mw} → @${toName}`,
      'Тебе дарят Premium в боте @PuweDownloaderBot!',
      JSON.stringify({ type: 'gift_monthly', uid, to_uid: toUid, months }),
      'XTR', [{ label: `Gift ${months}mo→${toName}`, amount: stars }]
    ).catch(e => log('warn', 'sendInvoice:', e.message));
  }

  if (data.startsWith('gpay_life_')) {
    const toUid  = parseInt(data.split('_')[2], 10);
    const toUser = dbGet(toUid);
    const toName = (toUser && (toUser.username || toUser.first_name)) || String(toUid);
    return bot.sendInvoice(uid, `🎁 Подарок: Premium навсегда → @${toName}`,
      'Тебе дарят Premium в боте @PuweDownloaderBot!',
      JSON.stringify({ type: 'gift_lifetime', uid, to_uid: toUid }),
      'XTR', [{ label: `Gift Lifetime→${toName}`, amount: LIFETIME_STARS }]
    ).catch(e => log('warn', 'sendInvoice:', e.message));
  }

  // ── Admin ─────────────────────────────────────────────────────────────────
  if (data === 'admin_stats' && ADMIN_IDS.has(uid)) {
    return edit(chatId, msgId, t(uid, 'stats', dbStats()), { reply_markup: kbAdmin(uid) });
  }
  if (data === 'admin_bc' && ADMIN_IDS.has(uid)) {
    setState(uid, { awaiting_broadcast: true });
    return edit(chatId, msgId, t(uid, 'bc_ask'));
  }
});

// ════════════════════════════════════════════════════════════════════════════
//  PAYMENT HANDLERS  (Telegram Stars / XTR)
// ════════════════════════════════════════════════════════════════════════════

// Pre-checkout: always approve
bot.on('pre_checkout_query', (q) => {
  bot.answerPreCheckoutQuery(q.id, true).catch(e => log('warn', 'preCheckout:', e.message));
});

// Successful payment
bot.on('message', async (msg) => {
  if (!msg.successful_payment) return;

  const uid     = msg.from.id;
  const payment = msg.successful_payment;
  const stars   = payment.total_amount;
  const payload = JSON.parse(payment.invoice_payload);
  const pType   = payload.type;
  const lang    = getLang(uid);

  dbAddStars(uid, stars);
  dbLogTx(uid, stars, pType, payload.months || 0, payment.invoice_payload);

  const fromName = msg.from.first_name || 'Аноним';

  if (pType === 'trial') {
    dbAddPremium(uid, TRIAL_DAYS);
    dbMarkTrial(uid);
    send(uid, t(uid, 'paid_trial'));

  } else if (pType === 'monthly') {
    dbAddPremium(uid, payload.months * 30);
    send(uid, t(uid, 'paid_month', payload.months, mword(payload.months, lang)));

  } else if (pType === 'lifetime') {
    dbAddPremium(uid, -1);
    send(uid, t(uid, 'paid_life'));

  } else if (pType === 'gift_trial') {
    const toUid  = payload.to_uid;
    dbAddPremium(toUid, TRIAL_DAYS);
    const toUser = dbGet(toUid);
    const toName = (toUser && (toUser.username || toUser.first_name)) || String(toUid);
    const period = lang === 'ru' ? '7 дней' : '7 days';
    send(uid, t(uid, 'gift_ok', toName));
    send(toUid, t(toUid, 'gift_recv', fromName, period));

  } else if (pType === 'gift_monthly') {
    const toUid  = payload.to_uid;
    dbAddPremium(toUid, payload.months * 30);
    const toUser = dbGet(toUid);
    const toName = (toUser && (toUser.username || toUser.first_name)) || String(toUid);
    const mw     = mword(payload.months, lang);
    send(uid, t(uid, 'gift_ok', toName));
    send(toUid, t(toUid, 'gift_recv', fromName, `${payload.months} ${mw}`));

  } else if (pType === 'gift_lifetime') {
    const toUid  = payload.to_uid;
    dbAddPremium(toUid, -1);
    const toUser = dbGet(toUid);
    const toName = (toUser && (toUser.username || toUser.first_name)) || String(toUid);
    send(uid, t(uid, 'gift_ok', toName));
    send(toUid, t(toUid, 'gift_recv', fromName, '♾️ навсегда / forever'));
  }
});

// ════════════════════════════════════════════════════════════════════════════
//  STARTUP
// ════════════════════════════════════════════════════════════════════════════

bot.setMyCommands([
  { command: 'start',    description: '🎬 Главное меню' },
  { command: 'search',   description: '🔍 Поиск YouTube/TikTok' },
  { command: 'sub',      description: '👑 Premium подписка' },
  { command: 'profile',  description: '👤 Мой профиль' },
  { command: 'settings', description: '⚙️ Настройки' },
  { command: 'ticket',   description: '🎫 Поддержка (Premium)' },
  { command: 'help',     description: '📋 Справка' },
]).catch(() => {});

bot.on('polling_error', err => log('error', 'Polling error:', err.message));

log('info', `🤖 PuweDownloaderBot started (polling)`);
log('info', `   DB:   ${DB_FILE}`);
log('info', `   Admins: ${[...ADMIN_IDS].join(', ')}`);
