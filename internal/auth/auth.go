// Package auth handles all authentication concerns:
//   - Telegram WebApp initData HMAC verification
//   - Session token minting / verification (SHA-256 rolling daily tokens)
//   - One-time login tokens shared with the Telegram bot via a JSON file
package auth

import (
	"crypto/hmac"
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"log"
	"net/url"
	"os"
	"sort"
	"strings"
	"sync"
	"time"

	"github.com/puwe/downloader/internal/db"
)

// ─────────────────────────────────────────────────────────────────────────────
// Session tokens
// ─────────────────────────────────────────────────────────────────────────────

// MakeToken generates a daily-rolling SHA-256 token for the given user.
// Tokens are valid for ~3 days (today and the two previous days are accepted).
func MakeToken(uid int64, salt string) string {
	day := time.Now().Unix() / 86400
	raw := fmt.Sprintf("%d:%s:%d", uid, salt, day)
	h := sha256.Sum256([]byte(raw))
	return hex.EncodeToString(h[:])
}

// VerifyToken checks whether token belongs to any known user.
// It checks the current day and the two preceding days to handle midnight
// rollovers without forcing users to re-authenticate.
// Returns the user ID on success, 0 on failure.
func VerifyToken(token, salt string, database *db.DB) int64 {
	if token == "" {
		return 0
	}
	ids, err := database.AllUserIDs()
	if err != nil {
		log.Printf("auth.VerifyToken: db error: %v", err)
		return 0
	}

	now := time.Now().Unix() / 86400
	for _, uid := range ids {
		for offset := int64(0); offset < 3; offset++ {
			day := now - offset
			raw := fmt.Sprintf("%d:%s:%d", uid, salt, day)
			h := sha256.Sum256([]byte(raw))
			expected := hex.EncodeToString(h[:])
			if hmac.Equal([]byte(expected), []byte(token)) {
				return uid
			}
		}
	}
	return 0
}

// ─────────────────────────────────────────────────────────────────────────────
// Telegram WebApp initData verification
// ─────────────────────────────────────────────────────────────────────────────

// TelegramUser is the subset of the Telegram user object we care about.
type TelegramUser struct {
	ID        int64  `json:"id"`
	FirstName string `json:"first_name"`
	Username  string `json:"username"`
	IsPremium bool   `json:"is_premium"` // Telegram's own premium flag (ignored for limits)
}

// VerifyInitData validates the HMAC signature of a Telegram Mini App initData
// string and returns the embedded user object.
//
// Algorithm (per Telegram docs):
//  1. Remove the hash= field from the data string.
//  2. Sort remaining fields alphabetically and join with \n.
//  3. Derive HMAC-SHA256 key: HMAC-SHA256("WebAppData", botToken).
//  4. Compute HMAC-SHA256(dataCheckString, key).
//  5. Compare hex digest to the hash field.
//  6. Reject if auth_date is older than 5 minutes.
func VerifyInitData(initData, botToken string) (*TelegramUser, error) {
	values, err := url.ParseQuery(initData)
	if err != nil {
		return nil, fmt.Errorf("parse initData: %w", err)
	}

	checkHash := values.Get("hash")
	if checkHash == "" {
		return nil, fmt.Errorf("missing hash")
	}
	values.Del("hash")

	// Build sorted key=value data-check string
	var pairs []string
	for k, vs := range values {
		pairs = append(pairs, k+"="+vs[0])
	}
	sort.Strings(pairs)
	dataCheckString := strings.Join(pairs, "\n")

	// Derive HMAC key from "WebAppData" + botToken
	mac := hmac.New(sha256.New, []byte("WebAppData"))
	mac.Write([]byte(botToken))
	secretKey := mac.Sum(nil)

	// Compute expected hash
	mac2 := hmac.New(sha256.New, secretKey)
	mac2.Write([]byte(dataCheckString))
	computed := hex.EncodeToString(mac2.Sum(nil))

	if !hmac.Equal([]byte(computed), []byte(checkHash)) {
		return nil, fmt.Errorf("signature mismatch")
	}

	// Reject stale auth_date (> 5 minutes old)
	authDate := values.Get("auth_date")
	if authDate != "" {
		var ts int64
		fmt.Sscanf(authDate, "%d", &ts)
		if time.Now().Unix()-ts > 300 {
			return nil, fmt.Errorf("initData expired")
		}
	}

	// Parse the user JSON object
	userJSON := values.Get("user")
	if userJSON == "" {
		return nil, fmt.Errorf("missing user field")
	}
	var u TelegramUser
	if err := json.Unmarshal([]byte(userJSON), &u); err != nil {
		return nil, fmt.Errorf("parse user: %w", err)
	}
	return &u, nil
}

// ─────────────────────────────────────────────────────────────────────────────
// One-time login tokens (shared with the Telegram bot via login_tokens.json)
// ─────────────────────────────────────────────────────────────────────────────

type loginEntry struct {
	UID     int64   `json:"uid"`
	Expires float64 `json:"expires"` // Unix timestamp (float to match Python's time.time())
}

// LoginTokenStore provides thread-safe access to the shared login_tokens.json
// file written by the Telegram bot.
type LoginTokenStore struct {
	mu   sync.Mutex
	path string
}

// NewLoginTokenStore creates a store backed by the given JSON file path.
func NewLoginTokenStore(path string) *LoginTokenStore {
	return &LoginTokenStore{path: path}
}

func (s *LoginTokenStore) load() map[string]loginEntry {
	data, err := os.ReadFile(s.path)
	if err != nil {
		return map[string]loginEntry{}
	}
	var m map[string]loginEntry
	if err := json.Unmarshal(data, &m); err != nil {
		return map[string]loginEntry{}
	}
	return m
}

func (s *LoginTokenStore) save(m map[string]loginEntry) {
	data, err := json.Marshal(m)
	if err != nil {
		return
	}
	os.WriteFile(s.path, data, 0600)
}

// Consume validates and removes a one-time login token.
// Returns the associated user ID, or 0 if invalid / expired.
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
