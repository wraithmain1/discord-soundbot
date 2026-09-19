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

ENV CONFIG_DIR=/app/config \
    SOUNDS_DIR=/app/sounds \
    WEB_PORT=8080

EXPOSE 8080

CMD ["python", "-m", "bot.main"]
