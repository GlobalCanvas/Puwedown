// Package config loads and exposes all application configuration.
// Values are read from environment variables with sensible defaults.
package config

import (
	"os"
	"strconv"
)

// Config holds all runtime configuration for the server.
type Config struct {
	BotToken      string // Telegram bot token — used for initData HMAC verification
	BotDB         string // Path to the shared SQLite database (bot.db)
	DownloadsDir  string // Directory where yt-dlp writes temporary files
	SecretSalt    string // Salt used when minting session tokens
	Port          string // TCP port the HTTP server listens on
	FreeLimit     int    // Max search-downloads per day for free users
	PremiumLimit  int    // Max search-downloads per day for premium users
	FileTTLSec    int    // Seconds until a downloaded file is auto-deleted
	MaxFileSizeMB int64  // Hard cap for served files (0 = no limit)
}

// Load reads config from the environment, applying defaults where needed.
func Load() *Config {
	return &Config{
		BotToken:      getEnv("BOT_TOKEN", ""),
		BotDB:         getEnv("BOT_DB", "bot.db"),
		DownloadsDir:  getEnv("DOWNLOADS_DIR", "webapp_dl"),
		SecretSalt:    getEnv("SECRET_SALT", "puwe_webapp_v1"),
		Port:          getEnv("WEBAPP_PORT", "8080"),
		FreeLimit:     getEnvInt("FREE_DL_DAY", 3),
		PremiumLimit:  getEnvInt("PREMIUM_DL_DAY", 12),
		FileTTLSec:    getEnvInt("FILE_TTL_SEC", 120),
		MaxFileSizeMB: int64(getEnvInt("MAX_FILE_SIZE_MB", 0)), // 0 = unlimited
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
