# ── Build stage ────────────────────────────────────────────────────────────────
FROM golang:1.22-bookworm AS builder

WORKDIR /src
COPY go.mod ./
RUN go mod tidy
RUN go mod download

COPY . .
# CGO is required for go-sqlite3
RUN CGO_ENABLED=1 GOOS=linux go build -ldflags="-s -w" -o /puwedownloader ./cmd/server

# ── Runtime stage ──────────────────────────────────────────────────────────────
FROM debian:bookworm-slim

# Install yt-dlp and its runtime dependency (Python 3)
RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install latest yt-dlp binary
RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
    -o /usr/local/bin/yt-dlp && chmod +x /usr/local/bin/yt-dlp

WORKDIR /app

COPY --from=builder /puwedownloader /app/puwedownloader

# miniapp.html must be mounted or copied here at runtime
# COPY miniapp.html /app/miniapp.html

RUN mkdir -p /app/webapp_dl

ENV WEBAPP_PORT=8080
ENV DOWNLOADS_DIR=/app/webapp_dl
ENV BOT_DB=/data/bot.db
ENV FILE_TTL_SEC=120
ENV MAX_FILE_SIZE_MB=0

EXPOSE 8080

# bot.db lives on a persistent volume shared with the Telegram bot container
VOLUME ["/data"]

ENTRYPOINT ["/app/puwedownloader"]
