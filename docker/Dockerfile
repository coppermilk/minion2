# One image, N containers (BLUEPRINT 14): the bots (on a /data mount) and
# the atomic web services (stateless) run from this same image. The bots'
# heavy stack (torch, ffmpeg, yt-dlp) and the services' web layer (fastapi,
# uvicorn, mcp) are both baked in; compose picks which minion each container
# runs. There is no separate service image.
# 3.12, not 3.13/3.14: facenet-pytorch pins numpy<2, and numpy<2 has
# no wheels past cp312 (cp313 wheels start at numpy 2.1). On a newer
# Python pip would resolve numpy down to 1.26 and build it from
# source -- but slim has no compiler, so the build fails. 3.12 gets a
# prebuilt numpy 1.26 wheel; the code floor is 3.11, so this is fine.
FROM python:3.12-slim

# ffmpeg/ffprobe for the video adapter; cron supervises batch bots.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg cron \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Unbuffered stdout/stderr so `docker logs` (Container Manager's Log tab)
# shows every line as it happens, not in delayed blocks.
ENV PYTHONUNBUFFERED=1

# Dependencies first, code second: a code-only change must not
# re-download torch. This layer re-runs only when pyproject.toml
# changes; the runtime extras are pinned here (dev stays out).
COPY pyproject.toml ./
RUN python -c "\
import tomllib; \
p = tomllib.load(open('pyproject.toml', 'rb'))['project']; \
extras = [x for k in ('ml', 'llm', 'links', 'tg') \
          for x in p['optional-dependencies'][k]]; \
print('\n'.join(p['dependencies'] + extras))" > /tmp/requirements.txt \
    && pip install --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# The services' web layer on top of the heavy stack (pure-python wheels,
# seconds). Its own layer so a torch/pyproject change need not re-fetch it,
# and a web-layer bump need not touch torch.
RUN pip install --no-cache-dir \
    'fastapi>=0.115' 'uvicorn>=0.30' 'pydantic>=2.7' 'mcp>=1.2'

# CPU-only torch from the PyTorch index (BLUEPRINT 12); heavy extras
# are baked into the one shared image. The code layer installs in
# seconds: every dependency is already satisfied above.
COPY README.md ./
COPY minion_core ./minion_core
COPY minions ./minions
COPY services ./services
RUN pip install --no-cache-dir --no-deps .

COPY docker/crontab /etc/cron.d/minions
RUN chmod 0644 /etc/cron.d/minions

# No runnable default: every real bot is selected by compose, which
# sets its command plus DRIVE=/data and the .env. Running the image
# directly (docker run / Container Manager "run image") would have no
# DRIVE and crash-loop on a confusing traceback, so the default is a
# one-line pointer to the right way in instead.
CMD ["sh", "-c", "echo 'minion2: run a bot via docker compose (docker-compose.yml sets its command, DRIVE=/data and .env) -- not by launching this image directly.' >&2; exit 1"]
