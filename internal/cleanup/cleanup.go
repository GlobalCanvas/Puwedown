// Package cleanup handles automatic deletion of temporary downloaded files.
//
// Two mechanisms work in tandem:
//  1. ScheduleDelete — per-file timer started after every successful download.
//     Fires exactly once after the configured TTL.
//  2. Sweeper — background goroutine that periodically scans the directory and
//     removes anything older than one hour. This catches files whose timers
//     were lost (e.g. after a server restart).
package cleanup

import (
	"log"
	"os"
	"path/filepath"
	"time"
)

// ScheduleDelete removes filePath after delay seconds in a background goroutine.
// The goroutine is a daemon — it does not prevent program exit.
func ScheduleDelete(filePath string, delaySec int) {
	go func() {
		time.Sleep(time.Duration(delaySec) * time.Second)
		if err := os.Remove(filePath); err == nil {
			log.Printf("cleanup: auto-deleted %s", filepath.Base(filePath))
		}
	}()
}

// StartSweeper launches a background goroutine that scans dir every interval
// and removes files whose modification time is older than maxAge.
//
// Call this once at startup. It runs until the process exits.
func StartSweeper(dir string, interval, maxAge time.Duration) {
	go func() {
		// Run once immediately on startup to clear files from a prior run.
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
		if err != nil {
			continue
		}
		if info.ModTime().Before(cutoff) {
			path := filepath.Join(dir, entry.Name())
			if err := os.Remove(path); err == nil {
				log.Printf("cleanup: swept old file %s", entry.Name())
			}
		}
	}
}
