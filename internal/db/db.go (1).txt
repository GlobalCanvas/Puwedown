// Package db provides a thread-safe wrapper around the shared SQLite database.
// The schema is identical to the one created by bot.py so both processes can
// share the same bot.db file without conflict.
package db

import (
	"database/sql"
	"fmt"
	"log"
	"time"

	_ "github.com/mattn/go-sqlite3" // CGO SQLite driver
)

// DB wraps sql.DB and exposes domain-level helpers.
type DB struct {
	sql *sql.DB
}

// User mirrors the `users` table row.
type User struct {
	UserID       int64
	Username     string
	FirstName    string
	Language     string
	AutoDL       int
	PremiumUntil int64 // -1 = lifetime, 0 = none, >0 = unix timestamp
	Downloads    int
	JoinedAt     int64
	LastSeen     int64
}

// Open opens (or creates) the SQLite file and ensures the schema exists.
func Open(path string) (*DB, error) {
	// WAL mode + busy timeout make concurrent reads from bot + webapp safe.
	dsn := fmt.Sprintf("file:%s?_journal_mode=WAL&_busy_timeout=5000&_foreign_keys=on", path)
	sql, err := sql.Open("sqlite3", dsn)
	if err != nil {
		return nil, fmt.Errorf("db open: %w", err)
	}

	// SQLite performs best with a small pool; one writer, multiple readers.
	sql.SetMaxOpenConns(10)
	sql.SetMaxIdleConns(5)

	d := &DB{sql: sql}
	if err := d.migrate(); err != nil {
		return nil, fmt.Errorf("db migrate: %w", err)
	}
	return d, nil
}

// migrate creates any missing tables/indexes.
// It is safe to call repeatedly — all statements use IF NOT EXISTS.
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

// Close shuts down the database connection pool.
func (d *DB) Close() { d.sql.Close() }

// GetUser returns the user row for uid, or nil if not found.
func (d *DB) GetUser(uid int64) (*User, error) {
	row := d.sql.QueryRow(
		`SELECT user_id, username, first_name, language, auto_dl, premium_until,
		        downloads, joined_at, last_seen
		   FROM users WHERE user_id = ?`, uid)

	u := &User{}
	err := row.Scan(&u.UserID, &u.Username, &u.FirstName, &u.Language,
		&u.AutoDL, &u.PremiumUntil, &u.Downloads, &u.JoinedAt, &u.LastSeen)
	if err == sql.ErrNoRows {
		return nil, nil
	}
	if err != nil {
		return nil, fmt.Errorf("get user %d: %w", uid, err)
	}
	return u, nil
}

// UpsertUser inserts or updates username / first_name / last_seen.
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

// IsPremium returns true when the user has an active premium subscription.
func (d *DB) IsPremium(uid int64) bool {
	u, err := d.GetUser(uid)
	if err != nil {
		log.Printf("db.IsPremium error: %v", err)
		return false
	}
	if u == nil {
		return false
	}
	// -1 = lifetime
	return u.PremiumUntil == -1 || u.PremiumUntil > time.Now().Unix()
}

// GetSearchDownloads returns the number of search-downloads made today by uid.
func (d *DB) GetSearchDownloads(uid int64) (int, error) {
	today := time.Now().Format("2006-01-02")
	var count int
	err := d.sql.QueryRow(
		`SELECT count FROM search_downloads WHERE user_id = ? AND date_str = ?`,
		uid, today).Scan(&count)
	if err == sql.ErrNoRows {
		return 0, nil
	}
	return count, err
}

// IncSearchDownloads atomically increments today's search-download counter.
func (d *DB) IncSearchDownloads(uid int64) error {
	today := time.Now().Format("2006-01-02")
	_, err := d.sql.Exec(`
		INSERT INTO search_downloads (user_id, date_str, count) VALUES (?, ?, 1)
		ON CONFLICT(user_id, date_str) DO UPDATE SET count = count + 1`,
		uid, today)
	return err
}

// AllUserIDs returns all user_id values — needed for token verification.
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
