# One image, N containers on one mount (BLUEPRINT 14).
FROM python:3.14-slim

# ffmpeg/ffprobe for the video adapter; cron supervises batch bots.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg cron \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY minion_core ./minion_core
COPY minions ./minions

# CPU-only torch from the PyTorch index (BLUEPRINT 12); heavy extras
# are baked into the one shared image.
RUN pip install --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        '.[ml,llm,links]'

COPY docker/crontab /etc/cron.d/minions
RUN chmod 0644 /etc/cron.d/minions

# DRIVE and TORCH_HOME come from compose; STATE/CACHE live on /data.
CMD ["python", "-m", "minions._template.main"]
