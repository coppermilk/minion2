# One image, N containers on one mount (BLUEPRINT 14).
FROM python:3.14-slim

# ffmpeg/ffprobe for the video adapter; cron supervises batch bots.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg cron \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Dependencies first, code second: a code-only change must not
# re-download torch. This layer re-runs only when pyproject.toml
# changes; the runtime extras are pinned here (dev stays out).
COPY pyproject.toml ./
RUN python -c "\
import tomllib; \
p = tomllib.load(open('pyproject.toml', 'rb'))['project']; \
extras = [x for k in ('ml', 'llm', 'links') \
          for x in p['optional-dependencies'][k]]; \
print('\n'.join(p['dependencies'] + extras))" > /tmp/requirements.txt \
    && pip install --no-cache-dir \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        -r /tmp/requirements.txt \
    && rm /tmp/requirements.txt

# CPU-only torch from the PyTorch index (BLUEPRINT 12); heavy extras
# are baked into the one shared image. The code layer installs in
# seconds: every dependency is already satisfied above.
COPY README.md ./
COPY minion_core ./minion_core
COPY minions ./minions
RUN pip install --no-cache-dir --no-deps .

COPY docker/crontab /etc/cron.d/minions
RUN chmod 0644 /etc/cron.d/minions

# DRIVE and TORCH_HOME come from compose; STATE/CACHE live on /data.
CMD ["python", "-m", "minions._template.main"]
