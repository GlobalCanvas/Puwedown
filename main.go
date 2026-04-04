package main

import (
	"context"
	"crypto/hmac"
	"crypto/sha256"
	"database/sql"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"mime"
	"net/http"
	"net/url"
	"os"
	"os/exec"
	"os/signal"
	"path/filepath"
	"sort"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/go-chi/chi/v5"
	chimw "github.com/go-chi/chi/v5/middleware"
	_ "github.com/mattn/go-sqlite3"
)

// ═══════════════════════════════════════════════════════════════
// CONFIG
// ═══════════════════════════════════════════════════════════════

type Config struct {
	BotToken      string
	BotDB         string
	DownloadsDir  string
	SecretSalt    string
	Port          string
	FreeLimit     int
	PremiumLimit  int
	FileTTLSec    int
	MaxFileSizeMB int64
}

func loadConfig() *Config {
	return &Config{
		BotToken:      getEnv("BOT_TOKEN", ""),
		BotDB:         getEnv("BOT_DB", "bot.db"),
		DownloadsDir:  getEnv("DOWNLOADS_DIR", "webapp_dl"),
		SecretSalt:    getEnv("SECRET_SALT", "puwe_webapp_v1"),
		Port:          getEnv("WEBAPP_PORT", "8080"),
		FreeLimit:     getEnvInt("FREE_DL_DAY", 3),
		PremiumLimit:  getEnvInt("PREMIUM_DL_DAY", 12),
		FileTTLSec:    getEnvInt("FILE_TTL_SEC", 120),
		MaxFileSizeMB: int64(getEnvInt("MAX_FILE_SIZE_MB", 0)),
	}
}

func getEnv(key, fallback string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return fallback
}

func getEnvInt(key string, fallback int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil {
			return n
		}
	}
	return fallback
}

// ═══════════════════════════════════════════════════════════════
// DATABASE
// ═══════════════════════════════════════════════════════════════

type DB struct {
	sql *sql.DB
}

type User struct {
	UserID       int64
	Username     string
	FirstName    string
	PremiumUntil int64
}

func openDB(path string) (*DB, error) {
	dsn := fmt.Sprintf("file:%s?_journal_mode=WAL&_busy_timeout=5000&_foreign_keys=on", path)
	sqlDB, err := sql.Open("sqlite3", dsn)
	if err != nil {
		return nil, fmt.Errorf("db open: %w", err)
	}
	sqlDB.SetMaxOpenConns(10)
	sqlDB.SetMaxIdleConns(5)

	d := &DB{sql: sqlDB}
	if err := d.migrate(); err != nil {
		return nil, fmt.Errorf("db migrate: %w", err)
	}
	return d, nil
}

func (d *DB) migrate() error {
	_, err := d.sql.Exec(`
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
	CREATE TABLE IF NOT EXISTS search_downloads (
		id       INTEGER PRIMARY KEY AUTOINCREMENT,
		user_id  INTEGER,
		date_str TEXT,
		count    INTEGER DEFAULT 0,
		UNIQUE(user_id, date_str)
	);
	CREATE INDEX IF NOT EXISTS idx_premium ON users(premium_until);
	`)
	return err
}

func (d *DB) Close() { d.sql.Close() }

func (d *DB) GetUser(uid int64) (*User, error) {
	u := &User{}
	err := d.sql.QueryRow(
		`SELECT user_id, username, first_name, premium_until FROM users WHERE user_id = ?`, uid,
	).Scan(&u.UserID, &u.Username, &u.FirstName, &u.PremiumUntil)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	return u, err
}

func (d *DB) UpsertUser(uid int64, username, firstName string) error {
	now := time.Now().Unix()
	_, err := d.sql.Exec(`
		INSERT INTO users (user_id, username, first_name, joined_at, last_seen)
		VALUES (?, ?, ?, ?, ?)
		ON CONFLICT(user_id) DO UPDATE SET
			username   = excluded.username,
			first_name = excluded.first_name,
			last_seen  = excluded.last_seen`,
		uid, username, firstName, now, now)
	return err
}

func (d *DB) IsPremium(uid int64) bool {
	u, err := d.GetUser(uid)
	if err != nil || u == nil {
		return false
	}
	return u.PremiumUntil == -1 || u.PremiumUntil > time.Now().Unix()
}

func (d *DB) GetSearchDownloads(uid int64) (int, error) {
	today := time.Now().Format("2006-01-02")
	var count int
	err := d.sql.QueryRow(
		`SELECT count FROM search_downloads WHERE user_id = ? AND date_str = ?`, uid, today,
	).Scan(&count)
	if err == sql.ErrNoRows {
		return 0, nil
	}
	return count, err
}

func (d *DB) IncSearchDownloads(uid int64) error {
	today := time.Now().Format("2006-01-02")
	_, err := d.sql.Exec(`
		INSERT INTO search_downloads (user_id, date_str, count) VALUES (?, ?, 1)
		ON CONFLICT(user_id, date_str) DO UPDATE SET count = count + 1`,
		uid, today)
	return err
}

func (d *DB) AllUserIDs() ([]int64, error) {
	rows, err := d.sql.Query(`SELECT user_id FROM users`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []int64
	for rows.Next() {
		var id int64
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

// ═══════════════════════════════════════════════════════════════
// AUTH
// ═══════════════════════════════════════════════════════════════

func makeToken(uid int64, salt string) string {
	day := time.Now().Unix() / 86400
	raw := fmt.Sprintf("%d:%s:%d", uid, salt, day)
	h := sha256.Sum256([]byte(raw))
	return hex.EncodeToString(h[:])
}

func verifyToken(token, salt string, db *DB) int64 {
	if token == "" {
		return 0
	}
	ids, err := db.AllUserIDs()
	if err != nil {
		return 0
	}
	now := time.Now().Unix() / 86400
	for _, uid := range ids {
		for offset := int64(0); offset < 3; offset++ {
			raw := fmt.Sprintf("%d:%s:%d", uid, salt, now-offset)
			h := sha256.Sum256([]byte(raw))
			expected := hex.EncodeToString(h[:])
			if hmac.Equal([]byte(expected), []byte(token)) {
				return uid
			}
		}
	}
	return 0
}

type TelegramUser struct {
	ID        int64  `json:"id"`
	FirstName string `json:"first_name"`
	Username  string `json:"username"`
}

func verifyInitData(initData, botToken string) (*TelegramUser, error) {
	values, err := url.ParseQuery(initData)
	if err != nil {
		return nil, fmt.Errorf("parse initData: %w", err)
	}
	checkHash := values.Get("hash")
	if checkHash == "" {
		return nil, fmt.Errorf("missing hash")
	}
	values.Del("hash")

	var pairs []string
	for k, vs := range values {
		pairs = append(pairs, k+"="+vs[0])
	}
	sort.Strings(pairs)
	dataCheckString := strings.Join(pairs, "\n")

	mac := hmac.New(sha256.New, []byte("WebAppData"))
	mac.Write([]byte(botToken))
	secretKey := mac.Sum(nil)

	mac2 := hmac.New(sha256.New, secretKey)
	mac2.Write([]byte(dataCheckString))
	computed := hex.EncodeToString(mac2.Sum(nil))

	if !hmac.Equal([]byte(computed), []byte(checkHash)) {
		return nil, fmt.Errorf("signature mismatch")
	}

	var ts int64
	fmt.Sscanf(values.Get("auth_date"), "%d", &ts)
	if time.Now().Unix()-ts > 300 {
		return nil, fmt.Errorf("initData expired")
	}

	var u TelegramUser
	if err := json.Unmarshal([]byte(values.Get("user")), &u); err != nil {
		return nil, fmt.Errorf("parse user: %w", err)
	}
	return &u, nil
}

// ─── One-time login tokens (shared with bot via login_tokens.json) ───

type loginEntry struct {
	UID     int64   `json:"uid"`
	Expires float64 `json:"expires"`
}

type LoginTokenStore struct {
	mu   sync.Mutex
	path string
}

func newLoginTokenStore(path string) *LoginTokenStore {
	return &LoginTokenStore{path: path}
}

func (s *LoginTokenStore) load() map[string]loginEntry {
	data, err := os.ReadFile(s.path)
	if err != nil {
		return map[string]loginEntry{}
	}
	var m map[string]loginEntry
	json.Unmarshal(data, &m)
	return m
}

func (s *LoginTokenStore) save(m map[string]loginEntry) {
	data, _ := json.Marshal(m)
	os.WriteFile(s.path, data, 0600)
}

func (s *LoginTokenStore) Consume(token string) int64 {
	s.mu.Lock()
	defer s.mu.Unlock()
	data := s.load()
	entry, ok := data[token]
	if !ok {
		return 0
	}
	delete(data, token)
	s.save(data)
	if float64(time.Now().Unix()) > entry.Expires {
		return 0
	}
	return entry.UID
}

// ═══════════════════════════════════════════════════════════════
// DOWNLOADER (yt-dlp subprocess)
// ═══════════════════════════════════════════════════════════════

type semaphore chan struct{}

func newSemaphore(n int) semaphore { return make(chan struct{}, n) }
func (s semaphore) Acquire()       { s <- struct{}{} }
func (s semaphore) Release()       { <-s }

type Downloader struct {
	dir string
	sem semaphore
}

func newDownloader(dir string, maxConcurrent int) *Downloader {
	if maxConcurrent <= 0 {
		maxConcurrent = 4
	}
	return &Downloader{dir: dir, sem: newSemaphore(maxConcurrent)}
}

type VideoInfo struct {
	URL         string       `json:"url"`
	Title       string       `json:"title"`
	Thumbnail   string       `json:"thumbnail"`
	Duration    int          `json:"duration"`
	DurationStr string       `json:"duration_str"`
	ViewCount   *int64       `json:"view_count"`
	Extractor   string       `json:"extractor"`
	Formats     []FormatInfo `json:"formats"`
}

type FormatInfo struct {
	FormatID   string `json:"format_id"`
	Height     int    `json:"height"`
	FormatNote string `json:"format_note"`
	Filesize   *int64 `json:"filesize"`
}

type DownloadResult struct {
	FilePath string
	Filename string
	Size     int64
}

type SearchResult struct {
	Title     string `json:"title"`
	URL       string `json:"url"`
	Thumbnail string `json:"thumbnail"`
	Duration  string `json:"duration"`
	Views     string `json:"views"`
}

// raw structs for yt-dlp JSON parsing
type rawInfo struct {
	Title     string      `json:"title"`
	Thumbnail string      `json:"thumbnail"`
	Duration  float64     `json:"duration"`
	ViewCount *int64      `json:"view_count"`
	Extractor string      `json:"extractor_key"`
	Formats   []rawFormat `json:"formats"`
}

type rawFormat struct {
	FormatID   string `json:"format_id"`
	Height     *int   `json:"height"`
	FormatNote string `json:"format_note"`
	Filesize   *int64 `json:"filesize"`
}

type rawSearchResult struct {
	Entries []struct {
		ID       string  `json:"id"`
		Title    string  `json:"title"`
		URL      string  `json:"url"`
		Webpage  string  `json:"webpage_url"`
		Thumb    string  `json:"thumbnail"`
		Duration float64 `json:"duration"`
		Views    *int64  `json:"view_count"`
		Uploader string  `json:"uploader"`
	} `json:"entries"`
}

const videoFormatSelector = "" +
	"best[vcodec!=none][acodec!=none][ext=mp4]" +
	"/best[vcodec!=none][acodec!=none]" +
	"/best[ext=mp4]" +
	"/best"

const audioFormatSelector = "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio"

func runYTDLP(ctx context.Context, args []string) ([]byte, error) {
	cmd := exec.CommandContext(ctx, "yt-dlp", args...)
	var stdout, stderr strings.Builder
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	if err := cmd.Run(); err != nil {
		msg := strings.TrimSpace(stderr.String())
		if msg == "" {
			msg = err.Error()
		}
		if len(msg) > 300 {
			msg = msg[:300]
		}
		return nil, fmt.Errorf("%s", msg)
	}
	return []byte(stdout.String()), nil
}

func (d *Downloader) Info(ctx context.Context, videoURL string) (*VideoInfo, error) {
	d.sem.Acquire()
	defer d.sem.Release()

	out, err := runYTDLP(ctx, []string{
		"--dump-json", "--no-warnings", "--quiet", "--no-playlist", videoURL,
	})
	if err != nil {
		return nil, err
	}

	var raw rawInfo
	if err := json.Unmarshal(out, &raw); err != nil {
		return nil, fmt.Errorf("parse info JSON: %w", err)
	}

	info := &VideoInfo{
		URL:       videoURL,
		Title:     truncateStr(raw.Title, 200),
		Thumbnail: raw.Thumbnail,
		Duration:  int(raw.Duration),
		ViewCount: raw.ViewCount,
		Extractor: raw.Extractor,
	}
	if raw.Duration > 0 {
		info.DurationStr = fmtDuration(int(raw.Duration))
	}

	seen := map[int]bool{}
	for i := len(raw.Formats) - 1; i >= 0; i-- {
		f := raw.Formats[i]
		if f.Height == nil || *f.Height < 144 || seen[*f.Height] {
			continue
		}
		seen[*f.Height] = true
		info.Formats = append(info.Formats, FormatInfo{
			FormatID: f.FormatID, Height: *f.Height,
			FormatNote: f.FormatNote, Filesize: f.Filesize,
		})
		if len(info.Formats) >= 6 {
			break
		}
	}
	return info, nil
}

func (d *Downloader) Download(ctx context.Context, videoURL, fileID, mode, formatID string) (*DownloadResult, error) {
	d.sem.Acquire()
	defer d.sem.Release()

	outTpl := filepath.Join(d.dir, fileID+".%(ext)s")

	var fmtSel string
	switch {
	case mode == "audio":
		fmtSel = audioFormatSelector
	case formatID != "" && formatID != "best":
		fmtSel = formatID + "/best[vcodec!=none][acodec!=none]/best"
	default:
		fmtSel = videoFormatSelector
	}

	_, err := runYTDLP(ctx, []string{
		"--output", outTpl,
		"--format", fmtSel,
		"--no-warnings", "--quiet", "--no-playlist",
		"--fragment-retries", "3", "--retries", "3", "--no-part",
		videoURL,
	})
	if err != nil {
		return nil, err
	}

	matches, _ := filepath.Glob(filepath.Join(d.dir, fileID+".*"))
	for _, path := range matches {
		if strings.HasSuffix(path, ".part") {
			continue
		}
		info, err := os.Stat(path)
		if err != nil || info.Size() < 1024 {
			continue
		}
		return &DownloadResult{
			FilePath: path,
			Filename: filepath.Base(path),
			Size:     info.Size(),
		}, nil
	}
	return nil, fmt.Errorf("downloaded file not found for id %s", fileID)
}

func (d *Downloader) Search(ctx context.Context, query, platform string) ([]SearchResult, error) {
	d.sem.Acquire()
	defer d.sem.Release()

	searchURL := "ytsearch100:" + query
	if platform == "tiktok" {
		searchURL = "tiktok:search:" + query
	}

	out, err := runYTDLP(ctx, []string{
		"--dump-json", "--flat-playlist", "--no-warnings", "--quiet", searchURL,
	})
	if err != nil {
		return nil, err
	}

	var raw rawSearchResult
	if err := json.Unmarshal(out, &raw); err != nil {
		return nil, fmt.Errorf("parse search JSON: %w", err)
	}

	var results []SearchResult
	for _, e := range raw.Entries {
		if len(results) >= 100 {
			break
		}
		link := e.URL
		if link == "" {
			link = e.Webpage
		}
		if link == "" && e.ID != "" {
			if platform == "tiktok" {
				link = fmt.Sprintf("https://www.tiktok.com/@%s/video/%s", e.Uploader, e.ID)
			} else {
				link = "https://www.youtube.com/watch?v=" + e.ID
			}
		}
		if link == "" {
			continue
		}
		title := e.Title
		if title == "" {
			title = "Unknown"
		}
		results = append(results, SearchResult{
			Title:     truncateStr(title, 80),
			URL:       link,
			Thumbnail: e.Thumb,
			Duration:  fmtDuration(int(e.Duration)),
			Views:     fmtViews(e.Views),
		})
	}
	return results, nil
}

func fileIDFromTime() string {
	return fmt.Sprintf("%x", time.Now().UnixNano())[:12]
}

// ═══════════════════════════════════════════════════════════════
// CLEANUP
// ═══════════════════════════════════════════════════════════════

func scheduleDelete(filePath string, delaySec int) {
	go func() {
		time.Sleep(time.Duration(delaySec) * time.Second)
		if err := os.Remove(filePath); err == nil {
			log.Printf("cleanup: auto-deleted %s", filepath.Base(filePath))
		}
	}()
}

func startSweeper(dir string, interval, maxAge time.Duration) {
	go func() {
		sweep(dir, maxAge)
		ticker := time.NewTicker(interval)
		defer ticker.Stop()
		for range ticker.C {
			sweep(dir, maxAge)
		}
	}()
}

func sweep(dir string, maxAge time.Duration) {
	entries, err := os.ReadDir(dir)
	if err != nil {
		return
	}
	cutoff := time.Now().Add(-maxAge)
	for _, entry := range entries {
		if entry.IsDir() {
			continue
		}
		info, err := entry.Info()
		if err != nil || !info.ModTime().Before(cutoff) {
			continue
		}
		os.Remove(filepath.Join(dir, entry.Name()))
	}
}

// ═══════════════════════════════════════════════════════════════
// MIDDLEWARE
// ═══════════════════════════════════════════════════════════════

type contextKey string

const uidKey contextKey = "uid"

func corsMiddleware(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		w.Header().Set("Access-Control-Allow-Origin", "*")
		w.Header().Set("Access-Control-Allow-Headers", "Content-Type, X-Token")
		w.Header().Set("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
		if r.Method == http.MethodOptions {
			w.WriteHeader(http.StatusNoContent)
			return
		}
		next.ServeHTTP(w, r)
	})
}

func authMiddleware(db *DB, salt string) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			token := r.Header.Get("X-Token")
			uid := verifyToken(token, salt, db)
			ctx := context.WithValue(r.Context(), uidKey, uid)
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

func uidFromCtx(ctx context.Context) int64 {
	uid, _ := ctx.Value(uidKey).(int64)
	return uid
}

// ═══════════════════════════════════════════════════════════════
// SERVER
// ═══════════════════════════════════════════════════════════════

type Server struct {
	cfg        *Config
	db         *DB
	dl         *Downloader
	loginStore *LoginTokenStore
}

func main() {
	cfg := loadConfig()

	if err := os.MkdirAll(cfg.DownloadsDir, 0755); err != nil {
		log.Fatalf("cannot create downloads dir: %v", err)
	}

	db, err := openDB(cfg.BotDB)
	if err != nil {
		log.Fatalf("db: %v", err)
	}
	defer db.Close()

	srv := &Server{
		cfg:        cfg,
		db:         db,
		dl:         newDownloader(cfg.DownloadsDir, 4),
		loginStore: newLoginTokenStore("login_tokens.json"),
	}

	startSweeper(cfg.DownloadsDir, 10*time.Minute, time.Hour)

	r := chi.NewRouter()
	r.Use(chimw.Recoverer)
	r.Use(chimw.RealIP)
	r.Use(corsMiddleware)

	r.Get("/", srv.handleIndex)
	r.Get("/miniapp.html", srv.handleIndex)
	r.Get("/login/{token}", srv.handleLogin)
	r.Post("/api/auth/telegram", srv.handleAuthTelegram)

	r.Group(func(r chi.Router) {
		r.Use(authMiddleware(db, cfg.SecretSalt))
		r.Get("/api/auth/me", srv.handleAuthMe)
		r.Get("/api/limits", srv.handleLimits)
		r.Post("/api/info", srv.handleInfo)
		r.Post("/api/download", srv.handleDownload)
		r.Post("/api/search", srv.handleSearch)
		r.Post("/api/search-download", srv.handleSearchDownload)
		r.Delete("/api/delete/{id}", srv.handleDelete)
		r.Get("/api/file/{name}", srv.handleFile)
	})

	httpSrv := &http.Server{
		Addr:         "0.0.0.0:" + cfg.Port,
		Handler:      r,
		ReadTimeout:  30 * time.Second,
		WriteTimeout: 0,
		IdleTimeout:  120 * time.Second,
	}

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)

	go func() {
		log.Printf("🌐 PuweDownloader listening on http://0.0.0.0:%s", cfg.Port)
		if err := httpSrv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatalf("server: %v", err)
		}
	}()

	<-quit
	log.Println("shutting down…")
	ctx, cancel := context.WithTimeout(context.Background(), 15*time.Second)
	defer cancel()
	httpSrv.Shutdown(ctx)
}

// ═══════════════════════════════════════════════════════════════
// HANDLERS
// ═══════════════════════════════════════════════════════════════

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

func requireUID(w http.ResponseWriter, r *http.Request) int64 {
	uid := uidFromCtx(r.Context())
	if uid == 0 {
		writeJSON(w, http.StatusUnauthorized, map[string]any{"ok": false, "error": "Unauthorized"})
	}
	return uid
}

func (s *Server) handleIndex(w http.ResponseWriter, r *http.Request) {
	http.ServeFile(w, r, "miniapp.html")
}

func (s *Server) handleLogin(w http.ResponseWriter, r *http.Request) {
	token := chi.URLParam(r, "token")
	uid := s.loginStore.Consume(token)
	if uid == 0 {
		w.Header().Set("Content-Type", "text/html; charset=utf-8")
		fmt.Fprint(w, `<html><body><h2>🚫 Link is invalid or expired.</h2><p>Request a new one in the bot: /start</p></body></html>`)
		return
	}

	u, _ := s.db.GetUser(uid)
	if u == nil {
		s.db.UpsertUser(uid, "", "")
		u, _ = s.db.GetUser(uid)
	}

	sessionToken := makeToken(uid, s.cfg.SecretSalt)
	isPrem := s.db.IsPremium(uid)

	firstName, username := "", ""
	if u != nil {
		firstName = u.FirstName
		username = u.Username
	}

	userJSON, _ := json.Marshal(map[string]any{
		"id": uid, "first_name": firstName,
		"username": username, "is_premium": isPrem,
	})
	tokenJSON, _ := json.Marshal(sessionToken)

	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	fmt.Fprintf(w, `<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Вход...</title>
<script>
  localStorage.setItem('pw_token', %s);
  localStorage.setItem('pw_user', %s);
  window.location.replace('/');
</script>
</head><body>
<p>🔑 Выполняем вход... <a href="/">Нажмите сюда если не перенаправило</a></p>
</body></html>`, tokenJSON, string(userJSON))
}

func (s *Server) handleAuthTelegram(w http.ResponseWriter, r *http.Request) {
	var body struct {
		InitData string `json:"init_data"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeJSON(w, 400, map[string]any{"ok": false, "error": "bad request"})
		return
	}
	tgUser, err := verifyInitData(body.InitData, s.cfg.BotToken)
	if err != nil {
		writeJSON(w, 401, map[string]any{"ok": false, "error": "Invalid Telegram data"})
		return
	}
	s.db.UpsertUser(tgUser.ID, tgUser.Username, tgUser.FirstName)
	token := makeToken(tgUser.ID, s.cfg.SecretSalt)
	writeJSON(w, 200, map[string]any{
		"ok": true, "token": token,
		"user": map[string]any{
			"id": tgUser.ID, "first_name": tgUser.FirstName,
			"username": tgUser.Username, "is_premium": s.db.IsPremium(tgUser.ID),
		},
	})
}

func (s *Server) handleAuthMe(w http.ResponseWriter, r *http.Request) {
	uid := requireUID(w, r)
	if uid == 0 {
		return
	}
	u, err := s.db.GetUser(uid)
	if err != nil || u == nil {
		writeJSON(w, 404, map[string]any{"ok": false, "error": "User not found"})
		return
	}
	writeJSON(w, 200, map[string]any{
		"ok": true,
		"user": map[string]any{
			"id": uid, "first_name": u.FirstName,
			"username": u.Username, "is_premium": s.db.IsPremium(uid),
		},
	})
}

func (s *Server) handleLimits(w http.ResponseWriter, r *http.Request) {
	uid := requireUID(w, r)
	if uid == 0 {
		return
	}
	isPrem := s.db.IsPremium(uid)
	limit := s.cfg.FreeLimit
	if isPrem {
		limit = s.cfg.PremiumLimit
	}
	used, _ := s.db.GetSearchDownloads(uid)
	writeJSON(w, 200, map[string]any{
		"ok": true, "used": used, "limit": limit, "is_premium": isPrem,
	})
}

func (s *Server) handleInfo(w http.ResponseWriter, r *http.Request) {
	uid := requireUID(w, r)
	if uid == 0 {
		return
	}
	var body struct {
		URL string `json:"url"`
	}
	json.NewDecoder(r.Body).Decode(&body)
	videoURL := strings.TrimSpace(body.URL)
	if videoURL == "" {
		writeJSON(w, 400, map[string]any{"ok": false, "error": "No URL"})
		return
	}
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()

	info, err := s.dl.Info(ctx, videoURL)
	if err != nil {
		writeJSON(w, 200, map[string]any{"ok": false, "error": truncateErr(err)})
		return
	}
	writeJSON(w, 200, map[string]any{
		"ok": true, "url": info.URL, "title": info.Title,
		"thumbnail": info.Thumbnail, "duration": info.Duration,
		"duration_str": info.DurationStr, "view_count": info.ViewCount,
		"extractor": info.Extractor, "formats": info.Formats,
	})
}

func (s *Server) handleDownload(w http.ResponseWriter, r *http.Request) {
	uid := requireUID(w, r)
	if uid == 0 {
		return
	}
	var body struct {
		URL      string `json:"url"`
		FormatID string `json:"format_id"`
		Mode     string `json:"mode"`
	}
	json.NewDecoder(r.Body).Decode(&body)
	videoURL := strings.TrimSpace(body.URL)
	if videoURL == "" {
		writeJSON(w, 400, map[string]any{"ok": false, "error": "No URL"})
		return
	}
	mode := body.Mode
	if mode == "" {
		mode = "video"
	}
	fileID := fileIDFromTime()
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Minute)
	defer cancel()

	result, err := s.dl.Download(ctx, videoURL, fileID, mode, body.FormatID)
	if err != nil {
		writeJSON(w, 200, map[string]any{"ok": false, "error": truncateErr(err)})
		return
	}
	if s.cfg.MaxFileSizeMB > 0 && result.Size > s.cfg.MaxFileSizeMB*1024*1024 {
		os.Remove(result.FilePath)
		writeJSON(w, 200, map[string]any{
			"ok": false, "error": fmt.Sprintf("Файл слишком большой (%d МБ).", result.Size/1024/1024),
		})
		return
	}
	scheduleDelete(result.FilePath, s.cfg.FileTTLSec)
	writeJSON(w, 200, map[string]any{
		"ok": true, "file_id": fileID, "filename": result.Filename,
		"size": result.Size, "download_url": "/api/file/" + result.Filename,
	})
}

func (s *Server) handleSearch(w http.ResponseWriter, r *http.Request) {
	uid := requireUID(w, r)
	if uid == 0 {
		return
	}
	var body struct {
		Query    string `json:"query"`
		Platform string `json:"platform"`
	}
	json.NewDecoder(r.Body).Decode(&body)
	query := strings.TrimSpace(body.Query)
	if query == "" {
		writeJSON(w, 400, map[string]any{"ok": false, "error": "No query"})
		return
	}
	platform := body.Platform
	if platform == "" {
		platform = "yt"
	}
	ctx, cancel := context.WithTimeout(r.Context(), 60*time.Second)
	defer cancel()

	results, err := s.dl.Search(ctx, query, platform)
	if err != nil {
		log.Printf("search error: %v", err)
		results = nil
	}
	used, _ := s.db.GetSearchDownloads(uid)
	limit := s.cfg.FreeLimit
	if s.db.IsPremium(uid) {
		limit = s.cfg.PremiumLimit
	}
	writeJSON(w, 200, map[string]any{
		"ok": true, "results": results, "used": used, "limit": limit,
	})
}

func (s *Server) handleSearchDownload(w http.ResponseWriter, r *http.Request) {
	uid := requireUID(w, r)
	if uid == 0 {
		return
	}
	isPrem := s.db.IsPremium(uid)
	limit := s.cfg.FreeLimit
	if isPrem {
		limit = s.cfg.PremiumLimit
	}
	used, _ := s.db.GetSearchDownloads(uid)
	if used >= limit {
		writeJSON(w, 429, map[string]any{
			"ok": false, "error": fmt.Sprintf("Лимит %d/день исчерпан", limit),
		})
		return
	}
	var body struct {
		URL string `json:"url"`
	}
	json.NewDecoder(r.Body).Decode(&body)
	videoURL := strings.TrimSpace(body.URL)
	if videoURL == "" {
		writeJSON(w, 400, map[string]any{"ok": false, "error": "No URL"})
		return
	}
	fileID := fileIDFromTime()
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Minute)
	defer cancel()

	result, err := s.dl.Download(ctx, videoURL, fileID, "video", "")
	if err != nil {
		writeJSON(w, 200, map[string]any{"ok": false, "error": truncateErr(err)})
		return
	}
	s.db.IncSearchDownloads(uid)
	used++
	scheduleDelete(result.FilePath, s.cfg.FileTTLSec)
	writeJSON(w, 200, map[string]any{
		"ok": true, "file_id": fileID, "filename": result.Filename,
		"download_url": "/api/file/" + result.Filename,
		"used": used, "limit": limit,
	})
}

func (s *Server) handleDelete(w http.ResponseWriter, r *http.Request) {
	uid := requireUID(w, r)
	if uid == 0 {
		return
	}
	safeID := sanitizeFileID(chi.URLParam(r, "id"))
	if safeID == "" {
		writeJSON(w, 400, map[string]any{"ok": false, "error": "invalid id"})
		return
	}
	matches, _ := filepath.Glob(filepath.Join(s.cfg.DownloadsDir, safeID+".*"))
	for _, path := range matches {
		os.Remove(path)
	}
	writeJSON(w, 200, map[string]any{"ok": true})
}

func (s *Server) handleFile(w http.ResponseWriter, r *http.Request) {
	uid := requireUID(w, r)
	if uid == 0 {
		return
	}
	fname := filepath.Base(chi.URLParam(r, "name"))
	fpath := filepath.Join(s.cfg.DownloadsDir, fname)

	f, err := os.Open(fpath)
	if err != nil {
		writeJSON(w, 404, map[string]any{"ok": false, "error": "Not found"})
		return
	}
	defer f.Close()

	stat, _ := f.Stat()
	contentType := mime.TypeByExtension(filepath.Ext(fname))
	if contentType == "" {
		contentType = "application/octet-stream"
	}
	w.Header().Set("Content-Type", contentType)
	w.Header().Set("Content-Disposition", `attachment; filename="`+fname+`"`)
	w.Header().Set("Cache-Control", "public, max-age=120")
	http.ServeContent(w, r, fname, stat.ModTime(), f)
}

// ═══════════════════════════════════════════════════════════════
// HELPERS
// ═══════════════════════════════════════════════════════════════

func truncateErr(err error) string {
	s := err.Error()
	if len(s) > 200 {
		return s[:200]
	}
	return s
}

func truncateStr(s string, max int) string {
	r := []rune(s)
	if len(r) > max {
		return string(r[:max])
	}
	return s
}

func sanitizeFileID(id string) string {
	if len(id) > 16 {
		id = id[:16]
	}
	for _, c := range id {
		if !((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F')) {
			return ""
		}
	}
	return id
}

func fmtDuration(seconds int) string {
	if seconds <= 0 {
		return ""
	}
	h, m, s := seconds/3600, (seconds%3600)/60, seconds%60
	if h > 0 {
		return fmt.Sprintf("%d:%02d:%02d", h, m, s)
	}
	return fmt.Sprintf("%d:%02d", m, s)
}

func fmtViews(v *int64) string {
	if v == nil || *v == 0 {
		return ""
	}
	n := *v
	switch {
	case n >= 1_000_000:
		return fmt.Sprintf("%.1fM 👁", float64(n)/1_000_000)
	case n >= 1_000:
		return fmt.Sprintf("%dK 👁", n/1_000)
	default:
		return fmt.Sprintf("%d 👁", n)
	}
}
