# Telethon premium-emoji userbot.
FROM python:3.12-slim

WORKDIR /app

# Install deps first so they cache across code edits.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY premium_emoji.py main.py login.py ./

# Keep the session FILE on /data — mount a volume there so the login survives
# container restarts and machine shutdowns (log in once, then silent forever).
# ".session" is appended by the app.
ENV TELEGRAM_SESSION_FILE=/data/telethon_premium_emoji
VOLUME ["/data"]

# Unbuffered logs so `docker logs` shows output immediately.
ENV PYTHONUNBUFFERED=1

CMD ["python", "main.py"]
