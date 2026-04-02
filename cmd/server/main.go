// PuweDownloader — Go rewrite of the Python Mini App backend.
//
// Architecture overview:
//   - chi router with CORS middleware on every route
//   - Auth middleware injects user ID into context on protected routes
//   - yt-dlp runs as a subprocess; concurrency is capped by a semaphore
//   - SQLite (WAL mode) shared with the Telegram bot process
//   - Files are served with http.ServeContent for Range support (large files)
//   - Two-layer cleanup: per-file timer + hourly background sweep
package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"mime"
	"net/http"
	"os"
	"os/signal"
	"path/filepath"
	"strings"
	"syscall"
	"time"

	"github.com/go-chi/chi/v5"
	chimw "github.com/go-chi/chi/v5/middleware"

	"github.com/puwe/downloader/config"
	"github.com/puwe/downloader/internal/auth"
	"github.com/puwe/downloader/internal/cleanup"
	"github.com/puwe/downloader/internal/db"
	"github.com/puwe/downloader/internal/downloader"
	mw "github.com/puwe/downloader/internal/middleware"
)

// ─────────────────────────────────────────────────────────────────────────────
// Server — holds all shared dependencies
// ─────────────────────────────────────────────────────────────────────────────

type Server struct {
	cfg        *config.Config
	db         *db.DB
	dl         *downloader.Downloader
	loginStore *auth.LoginTokenStore
}

// ─────────────────────────────────────────────────────────────────────────────
// main
// ─────────────────────────────────────────────────────────────────────────────

func main() {
	cfg := config.Load()

	// Ensure downloads directory exists
	if err := os.MkdirAll(cfg.DownloadsDir, 0755); err != nil {
		log.Fatalf("cannot create downloads dir: %v", err)
	}

	// Open shared SQLite database
	database, err := db.Open(cfg.BotDB)
	if err != nil {
		log.Fatalf("db: %v", err)
	}
	defer database.Close()

	srv := &Server{
		cfg:        cfg,
		db:         database,
		dl:         downloader.New(cfg.DownloadsDir, 4),
		loginStore: auth.NewLoginTokenStore("login_tokens.json"),
	}

	// Start background file sweeper (scan every 10 min, remove files > 1 hour old)
	cleanup.StartSweeper(cfg.DownloadsDir, 10*time.Minute, time.Hour)

	// Build router
	r := chi.NewRouter()
	r.Use(chimw.Recoverer)   // recover from panics, return 500
	r.Use(chimw.RealIP)      // trust X-Forwarded-For from reverse proxy
	r.Use(chimw.RequestID)   // add X-Request-Id header
	r.Use(mw.CORS)           // permissive CORS for Telegram WebView

	// ── Static / mini app ──
	r.Get("/", srv.handleIndex)
	r.Get("/miniapp.html", srv.handleIndex)
	r.Get("/login/{token}", srv.handleLogin)

	// ── API — public (no auth required) ──
	r.Post("/api/auth/telegram", srv.handleAuthTelegram)

	// ── API — protected (auth middleware injects uid) ──
	r.Group(func(r chi.Router) {
		r.Use(mw.Auth(database, cfg.SecretSalt))

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
		// WriteTimeout must be long enough for large file downloads.
		// Set to 0 (unlimited) and let the handler context manage deadlines.
		WriteTimeout: 0,
		IdleTimeout:  120 * time.Second,
	}

	// Graceful shutdown on SIGINT / SIGTERM
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

// ─────────────────────────────────────────────────────────────────────────────
// JSON helper
// ─────────────────────────────────────────────────────────────────────────────

func writeJSON(w http.ResponseWriter, status int, v any) {
	w.Header().Set("Content-Type", "application/json; charset=utf-8")
	w.WriteHeader(status)
	json.NewEncoder(w).Encode(v)
}

// requireUID extracts the uid from context and writes 401 if zero.
// Returns 0 when the request should be aborted.
func requireUID(w http.ResponseWriter, r *http.Request) int64 {
	uid := mw.UIDFromContext(r.Context())
	if uid == 0 {
		writeJSON(w, http.StatusUnauthorized, map[string]any{
			"ok": false, "error": "Unauthorized",
		})
	}
	return uid
}

// ─────────────────────────────────────────────────────────────────────────────
// Handlers
// ─────────────────────────────────────────────────────────────────────────────

// GET /
func (s *Server) handleIndex(w http.ResponseWriter, r *http.Request) {
	http.ServeFile(w, r, "miniapp.html")
}

// GET /login/{token}
// One-time link generated by the Telegram bot. Consumes the token, mints a
// session token, and returns an HTML page that saves it to localStorage.
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

	sessionToken := auth.MakeToken(uid, s.cfg.SecretSalt)
	isPrem := s.db.IsPremium(uid)

	firstName := ""
	username := ""
	if u != nil {
		firstName = u.FirstName
		username = u.Username
	}

	userJSON, _ := json.Marshal(map[string]any{
		"id":         uid,
		"first_name": firstName,
		"username":   username,
		"is_premium": isPrem,
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

// POST /api/auth/telegram
// Validates Telegram initData and returns a session token.
func (s *Server) handleAuthTelegram(w http.ResponseWriter, r *http.Request) {
	var body struct {
		InitData string `json:"init_data"`
	}
	if err := json.NewDecoder(r.Body).Decode(&body); err != nil {
		writeJSON(w, 400, map[string]any{"ok": false, "error": "bad request"})
		return
	}

	tgUser, err := auth.VerifyInitData(body.InitData, s.cfg.BotToken)
	if err != nil {
		log.Printf("initData verify: %v", err)
		writeJSON(w, 401, map[string]any{"ok": false, "error": "Invalid Telegram data"})
		return
	}

	s.db.UpsertUser(tgUser.ID, tgUser.Username, tgUser.FirstName)
	token := auth.MakeToken(tgUser.ID, s.cfg.SecretSalt)
	isPrem := s.db.IsPremium(tgUser.ID)

	writeJSON(w, 200, map[string]any{
		"ok":    true,
		"token": token,
		"user": map[string]any{
			"id":         tgUser.ID,
			"first_name": tgUser.FirstName,
			"username":   tgUser.Username,
			"is_premium": isPrem,
		},
	})
}

// GET /api/auth/me
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
			"id":         uid,
			"first_name": u.FirstName,
			"username":   u.Username,
			"is_premium": s.db.IsPremium(uid),
		},
	})
}

// GET /api/limits
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
		"ok":         true,
		"used":       used,
		"limit":      limit,
		"is_premium": isPrem,
	})
}

// POST /api/info
// Returns video metadata without downloading.
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

	// 30-second timeout for metadata fetch
	ctx, cancel := context.WithTimeout(r.Context(), 30*time.Second)
	defer cancel()

	info, err := s.dl.Info(ctx, videoURL)
	if err != nil {
		writeJSON(w, 200, map[string]any{"ok": false, "error": truncateErr(err)})
		return
	}

	writeJSON(w, 200, map[string]any{
		"ok":           true,
		"url":          info.URL,
		"title":        info.Title,
		"thumbnail":    info.Thumbnail,
		"duration":     info.Duration,
		"duration_str": info.DurationStr,
		"view_count":   info.ViewCount,
		"extractor":    info.Extractor,
		"formats":      info.Formats,
	})
}

// POST /api/download
// Downloads a video and returns a temporary download URL.
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

	fileID := downloader.FileIDFromTime()

	// Large downloads can take minutes — use a generous timeout.
	// The client connection stays open throughout.
	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Minute)
	defer cancel()

	result, err := s.dl.Download(ctx, videoURL, fileID, mode, body.FormatID)
	if err != nil {
		writeJSON(w, 200, map[string]any{"ok": false, "error": truncateErr(err)})
		return
	}

	// Enforce optional file size limit
	if s.cfg.MaxFileSizeMB > 0 && result.Size > s.cfg.MaxFileSizeMB*1024*1024 {
		os.Remove(result.FilePath)
		writeJSON(w, 200, map[string]any{
			"ok":    false,
			"error": fmt.Sprintf("Файл слишком большой (%d МБ).", result.Size/1024/1024),
		})
		return
	}

	cleanup.ScheduleDelete(result.FilePath, s.cfg.FileTTLSec)

	writeJSON(w, 200, map[string]any{
		"ok":           true,
		"file_id":      fileID,
		"filename":     result.Filename,
		"size":         result.Size,
		"download_url": "/api/file/" + result.Filename,
	})
}

// POST /api/search
// Returns search results without consuming the daily download quota.
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
		results = nil // return empty list, not an error
	}

	used, _ := s.db.GetSearchDownloads(uid)
	limit := s.cfg.FreeLimit
	if s.db.IsPremium(uid) {
		limit = s.cfg.PremiumLimit
	}

	writeJSON(w, 200, map[string]any{
		"ok":      true,
		"results": results,
		"used":    used,
		"limit":   limit,
	})
}

// POST /api/search-download
// Downloads a video chosen from search results. Enforces daily quota.
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
			"ok":    false,
			"error": fmt.Sprintf("Лимит %d/день исчерпан", limit),
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

	fileID := downloader.FileIDFromTime()

	ctx, cancel := context.WithTimeout(r.Context(), 10*time.Minute)
	defer cancel()

	result, err := s.dl.Download(ctx, videoURL, fileID, "video", "")
	if err != nil {
		writeJSON(w, 200, map[string]any{"ok": false, "error": truncateErr(err)})
		return
	}

	// Increment counter only after a successful download
	s.db.IncSearchDownloads(uid)
	used++

	cleanup.ScheduleDelete(result.FilePath, s.cfg.FileTTLSec)

	writeJSON(w, 200, map[string]any{
		"ok":           true,
		"file_id":      fileID,
		"filename":     result.Filename,
		"download_url": "/api/file/" + result.Filename,
		"used":         used,
		"limit":        limit,
	})
}

// DELETE /api/delete/{id}
// Immediately removes all files with the given file ID prefix.
func (s *Server) handleDelete(w http.ResponseWriter, r *http.Request) {
	uid := requireUID(w, r)
	if uid == 0 {
		return
	}

	rawID := chi.URLParam(r, "id")
	// Sanitize: only allow hex characters, max 16 chars
	safeID := sanitizeFileID(rawID)
	if safeID == "" {
		writeJSON(w, 400, map[string]any{"ok": false, "error": "invalid id"})
		return
	}

	pattern := filepath.Join(s.cfg.DownloadsDir, safeID+".*")
	matches, _ := filepath.Glob(pattern)
	for _, path := range matches {
		if err := os.Remove(path); err == nil {
			log.Printf("delete: removed %s (user %d)", filepath.Base(path), uid)
		}
	}
	writeJSON(w, 200, map[string]any{"ok": true})
}

// GET /api/file/{name}
// Serves a previously downloaded file.
//
// Uses http.ServeContent which:
//   - Sends Content-Length so the browser can show download progress
//   - Supports Range requests for resumable downloads
//   - Handles If-Modified-Since / ETag caching
//   - Streams from disk — never loads the whole file into memory
func (s *Server) handleFile(w http.ResponseWriter, r *http.Request) {
	uid := requireUID(w, r)
	if uid == 0 {
		return
	}

	// Prevent path traversal: take only the base name
	fname := filepath.Base(chi.URLParam(r, "name"))
	fpath := filepath.Join(s.cfg.DownloadsDir, fname)

	f, err := os.Open(fpath)
	if err != nil {
		writeJSON(w, 404, map[string]any{"ok": false, "error": "Not found"})
		return
	}
	defer f.Close()

	stat, err := f.Stat()
	if err != nil {
		writeJSON(w, 500, map[string]any{"ok": false, "error": "stat error"})
		return
	}

	// Detect MIME type from extension; fall back to octet-stream
	ext := filepath.Ext(fname)
	contentType := mime.TypeByExtension(ext)
	if contentType == "" {
		contentType = "application/octet-stream"
	}

	w.Header().Set("Content-Type", contentType)
	w.Header().Set("Content-Disposition", `attachment; filename="`+fname+`"`)
	w.Header().Set("Cache-Control", "public, max-age=120")

	// ServeContent handles Range, ETag, and streaming automatically.
	http.ServeContent(w, r, fname, stat.ModTime(), f)
}

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

// truncateErr returns the first 200 characters of an error string.
func truncateErr(err error) string {
	s := err.Error()
	if len(s) > 200 {
		return s[:200]
	}
	return s
}

// sanitizeFileID allows only hex digits and limits length to 16 chars.
// Returns "" if the input fails validation.
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
