FROM python:3.12-slim

# ffmpeg is required by discord.py for voice playback
RUN apt-get update && \
    apt-get install -y --no-install-recommends ffmpeg && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot

RUN mkdir -p /app/sounds /app/config

# Stamped in by the GitHub Actions workflow at build time, so the running
# container always knows exactly which commit/build it's running - shown
# on the dashboard rather than needing a manually-bumped version number.
ARG GIT_SHA=unknown
ARG BUILD_TIME=unknown

ENV GIT_SHA=${GIT_SHA} \
    BUILD_TIME=${BUILD_TIME} \
    CONFIG_DIR=/app/config \
    SOUNDS_DIR=/app/sounds \
    WEB_PORT=8080

EXPOSE 8080

CMD ["python", "-m", "bot.main"]
