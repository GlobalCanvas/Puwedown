// Package downloader wraps yt-dlp as a subprocess.
//
// Design decisions:
//   - Each download runs in its own goroutine; Go's scheduler handles the
//     concurrency automatically — no thread pool needed.
//   - We never transcode: video formats are constrained to single-file
//     containers (muxed audio+video) so FFmpeg is not required.
//   - stdout/stderr are captured and surfaced as structured errors.
//   - A configurable semaphore limits simultaneous yt-dlp processes to avoid
//     saturating the CPU / network during traffic spikes.
package downloader

import (
	"context"
	"encoding/json"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strings"
	"sync"
	"time"
)

// ─────────────────────────────────────────────────────────────────────────────
// Semaphore — cap concurrent yt-dlp child processes
// ─────────────────────────────────────────────────────────────────────────────

type semaphore chan struct{}

func newSemaphore(n int) semaphore { return make(chan struct{}, n) }
func (s semaphore) Acquire()       { s <- struct{}{} }
func (s semaphore) Release()       { <-s }

// ─────────────────────────────────────────────────────────────────────────────
// Public types
// ─────────────────────────────────────────────────────────────────────────────

// VideoInfo holds the metadata returned by /api/info.
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

// FormatInfo describes a single available video quality.
type FormatInfo struct {
	FormatID   string  `json:"format_id"`
	Height     int     `json:"height"`
	FormatNote string  `json:"format_note"`
	Filesize   *int64  `json:"filesize"`
}

// DownloadResult is returned after a successful yt-dlp download.
type DownloadResult struct {
	FilePath string
	Filename string
	Size     int64
}

// SearchResult is a single item from a yt-dlp search query.
type SearchResult struct {
	Title     string `json:"title"`
	URL       string `json:"url"`
	Thumbnail string `json:"thumbnail"`
	Duration  string `json:"duration"`
	Views     string `json:"views"`
}

// Downloader runs yt-dlp as a subprocess.
type Downloader struct {
	downloadsDir string
	sem          semaphore
}

// New creates a Downloader.
//   - downloadsDir: directory where files are written
//   - maxConcurrent: max simultaneous yt-dlp processes (0 → 4)
func New(downloadsDir string, maxConcurrent int) *Downloader {
	if maxConcurrent <= 0 {
		maxConcurrent = 4
	}
	return &Downloader{
		downloadsDir: downloadsDir,
		sem:          newSemaphore(maxConcurrent),
	}
}

// ─────────────────────────────────────────────────────────────────────────────
// Info — extract metadata without downloading
// ─────────────────────────────────────────────────────────────────────────────

// rawInfo is used only for JSON unmarshalling from yt-dlp's --dump-json output.
type rawInfo struct {
	Title     string      `json:"title"`
	Thumbnail string      `json:"thumbnail"`
	Duration  float64     `json:"duration"`
	ViewCount *int64      `json:"view_count"`
	Extractor string      `json:"extractor_key"`
	Formats   []rawFormat `json:"formats"`
}

type rawFormat struct {
	FormatID   string  `json:"format_id"`
	Height     *int    `json:"height"`
	FormatNote string  `json:"format_note"`
	Filesize   *int64  `json:"filesize"`
}

// Info fetches video metadata via `yt-dlp --dump-json`.
// The context can be used to impose a per-request timeout.
func (d *Downloader) Info(ctx context.Context, videoURL string) (*VideoInfo, error) {
	d.sem.Acquire()
	defer d.sem.Release()

	args := []string{
		"--dump-json",
		"--no-warnings",
		"--quiet",
		"--no-playlist",
		videoURL,
	}

	out, err := runYTDLP(ctx, args)
	if err != nil {
		return nil, fmt.Errorf("yt-dlp info: %w", err)
	}

	var raw rawInfo
	if err := json.Unmarshal(out, &raw); err != nil {
		return nil, fmt.Errorf("parse info JSON: %w", err)
	}

	info := &VideoInfo{
		URL:       videoURL,
		Title:     truncate(raw.Title, 200),
		Thumbnail: raw.Thumbnail,
		Duration:  int(raw.Duration),
		ViewCount: raw.ViewCount,
		Extractor: raw.Extractor,
	}
	if raw.Duration > 0 {
		info.DurationStr = fmtDuration(int(raw.Duration))
	}

	// Deduplicate and limit to the 6 best heights (≥144p)
	seen := map[int]bool{}
	for i := len(raw.Formats) - 1; i >= 0; i-- {
		f := raw.Formats[i]
		if f.Height == nil || *f.Height < 144 {
			continue
		}
		if seen[*f.Height] {
			continue
		}
		seen[*f.Height] = true
		info.Formats = append(info.Formats, FormatInfo{
			FormatID:   f.FormatID,
			Height:     *f.Height,
			FormatNote: f.FormatNote,
			Filesize:   f.Filesize,
		})
		if len(info.Formats) >= 6 {
			break
		}
	}
	return info, nil
}

// ─────────────────────────────────────────────────────────────────────────────
// Download — fetch a video to disk
// ─────────────────────────────────────────────────────────────────────────────

// formatSelector chooses muxed (single-file) video without FFmpeg.
// The fallback chain matches the Python original exactly.
const videoFormatSelector = "" +
	"best[vcodec!=none][acodec!=none][ext=mp4]" +
	"/best[vcodec!=none][acodec!=none]" +
	"/best[ext=mp4]" +
	"/best"

const audioFormatSelector = "bestaudio[ext=m4a]/bestaudio[ext=webm]/bestaudio"

// Download runs yt-dlp and returns information about the saved file.
// mode is "video" or "audio"; formatID overrides the default selector when set.
func (d *Downloader) Download(ctx context.Context, videoURL, fileID, mode, formatID string) (*DownloadResult, error) {
	d.sem.Acquire()
	defer d.sem.Release()

	outTpl := filepath.Join(d.downloadsDir, fileID+".%(ext)s")

	var fmtSel string
	switch {
	case mode == "audio":
		fmtSel = audioFormatSelector
	case formatID != "" && formatID != "best":
		// User picked a specific format from /api/info — honour it.
		// Append a safe fallback in case the format is video-only.
		fmtSel = formatID + "/best[vcodec!=none][acodec!=none]/best"
	default:
		fmtSel = videoFormatSelector
	}

	args := []string{
		"--output", outTpl,
		"--format", fmtSel,
		"--no-warnings",
		"--quiet",
		"--no-playlist",
		"--fragment-retries", "3",
		"--retries", "3",
		"--no-part", // write directly, no .part files
		videoURL,
	}

	if _, err := runYTDLP(ctx, args); err != nil {
		return nil, fmt.Errorf("yt-dlp download: %w", err)
	}

	return d.findDownloadedFile(fileID)
}

// findDownloadedFile locates the file yt-dlp wrote for the given fileID prefix.
func (d *Downloader) findDownloadedFile(fileID string) (*DownloadResult, error) {
	pattern := filepath.Join(d.downloadsDir, fileID+".*")
	matches, err := filepath.Glob(pattern)
	if err != nil {
		return nil, err
	}

	for _, path := range matches {
		// Skip any leftover .part files
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

// ─────────────────────────────────────────────────────────────────────────────
// Search — return up to 100 results
// ─────────────────────────────────────────────────────────────────────────────

// rawSearchEntry is the flat entry format returned by --extract-flat.
type rawSearchEntry struct {
	ID       string  `json:"id"`
	Title    string  `json:"title"`
	URL      string  `json:"url"`
	Webpage  string  `json:"webpage_url"`
	Thumb    string  `json:"thumbnail"`
	Duration float64 `json:"duration"`
	Views    *int64  `json:"view_count"`
	Uploader string  `json:"uploader"`
}

type rawSearchResult struct {
	Entries []rawSearchEntry `json:"entries"`
}

// Search returns up to 100 results for query on the given platform ("yt" or "tiktok").
func (d *Downloader) Search(ctx context.Context, query, platform string) ([]SearchResult, error) {
	d.sem.Acquire()
	defer d.sem.Release()

	var searchURL string
	if platform == "tiktok" {
		searchURL = "tiktok:search:" + query
	} else {
		searchURL = "ytsearch100:" + query
	}

	args := []string{
		"--dump-json",
		"--flat-playlist",
		"--no-warnings",
		"--quiet",
		searchURL,
	}

	// Search can take a while — use a longer implicit deadline.
	out, err := runYTDLP(ctx, args)
	if err != nil {
		return nil, fmt.Errorf("yt-dlp search: %w", err)
	}

	// yt-dlp emits a single JSON object with an "entries" array.
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
		if len(title) > 80 {
			title = title[:80]
		}

		results = append(results, SearchResult{
			Title:     title,
			URL:       link,
			Thumbnail: e.Thumb,
			Duration:  fmtDuration(int(e.Duration)),
			Views:     fmtViews(e.Views),
		})
	}
	return results, nil
}

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

// runYTDLP executes yt-dlp with args and returns combined stdout output.
// stderr is captured and included in the error message on failure.
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
		// Truncate very long error output
		if len(msg) > 300 {
			msg = msg[:300]
		}
		return nil, fmt.Errorf("%s", msg)
	}
	return []byte(stdout.String()), nil
}

// fmtDuration converts seconds to "H:MM:SS" or "M:SS".
func fmtDuration(seconds int) string {
	if seconds <= 0 {
		return ""
	}
	h := seconds / 3600
	m := (seconds % 3600) / 60
	s := seconds % 60
	if h > 0 {
		return fmt.Sprintf("%d:%02d:%02d", h, m, s)
	}
	return fmt.Sprintf("%d:%02d", m, s)
}

// fmtViews formats a view count for display (e.g. "1.4M 👁").
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

// truncate cuts s to max runes.
func truncate(s string, max int) string {
	runes := []rune(s)
	if len(runes) > max {
		return string(runes[:max])
	}
	return s
}

// FileIDFromTime generates a short unique ID based on the current nanosecond.
// This avoids the uuid dependency while remaining collision-resistant enough
// for temporary filenames.
func FileIDFromTime() string {
	return fmt.Sprintf("%x", time.Now().UnixNano())[:12]
}

// Ensure the semaphore is a sync primitive, not just a channel alias.
var _ sync.Locker = (*mutexAdapter)(nil)

type mutexAdapter struct{ sem semaphore }

func (m *mutexAdapter) Lock()   { m.sem.Acquire() }
func (m *mutexAdapter) Unlock() { m.sem.Release() }
