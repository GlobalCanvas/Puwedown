// Package middleware provides reusable chi middleware for the HTTP server.
package middleware

import (
	"context"
	"net/http"

	"github.com/puwe/downloader/internal/auth"
	"github.com/puwe/downloader/internal/db"
)

type contextKey string

const uidKey contextKey = "uid"

// CORS adds permissive CORS headers — appropriate for a Telegram Mini App
// where the origin is a Telegram WebView iframe, not a real browser origin.
func CORS(next http.Handler) http.Handler {
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

// Auth reads the X-Token header, verifies it, and injects the uid into the
// request context.  Unprotected routes (e.g. /api/auth/telegram) should NOT
// use this middleware — they handle auth themselves.
func Auth(database *db.DB, salt string) func(http.Handler) http.Handler {
	return func(next http.Handler) http.Handler {
		return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
			token := r.Header.Get("X-Token")
			uid := auth.VerifyToken(token, salt, database)
			// Inject uid even if 0; handlers decide whether 0 is acceptable.
			ctx := context.WithValue(r.Context(), uidKey, uid)
			next.ServeHTTP(w, r.WithContext(ctx))
		})
	}
}

// UIDFromContext extracts the authenticated user ID injected by Auth middleware.
// Returns 0 if not present or authentication failed.
func UIDFromContext(ctx context.Context) int64 {
	uid, _ := ctx.Value(uidKey).(int64)
	return uid
}
