# ── Build stage ─────────────────────────────────────────
FROM golang:1.22-bookworm AS builder

WORKDIR /src

# Копируем ВЕСЬ проект сразу
COPY . .

# Автоматически подтягиваем зависимости и создаём go.sum
RUN go mod tidy

# Собираем
RUN CGO_ENABLED=1 GOOS=linux go build -ldflags="-s -w" -o /puwedownloader ./cmd/server


# ── Runtime stage ───────────────────────────────────────
FROM debian:bookworm-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    python3 \
    ca-certificates \
    curl \
    && rm -rf /var/lib/apt/lists/*

RUN curl -L https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp \
    -o /usr/local/bin/yt-dlp && chmod +x /usr/local/bin/yt-dlp

WORKDIR /app

COPY --from=builder /puwedownloader /app/puwedownloader

RUN mkdir -p /app/webapp_dl

ENV WEBAPP_PORT=8080
ENV DOWNLOADS_DIR=/app/webapp_dl
ENV BOT_DB=/data/bot.db
ENV FILE_TTL_SEC=120
ENV MAX_FILE_SIZE_MB=0

EXPOSE 8080

VOLUME ["/data"]

ENTRYPOINT ["/app/puwedownloader"]
