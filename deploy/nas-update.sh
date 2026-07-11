#!/bin/sh
# Synology NAS: the one self-healing deploy command. Run it and forget:
# it force-syncs the repo to origin/main (even into a non-empty folder,
# keeping your .env), recreates the whole stack -- bots, atomic services,
# thin transports, n8n and the canvas placeholder, all one compose project
# -- from the freshly published images, prunes old images, and pulls the
# local model itself so you never run `docker compose exec ... ollama pull`
# by hand.
#
# The image is built on GitHub and published to GHCR (image.yml), so the
# NAS never compiles torch -- it just downloads the ready image. The
# GHCR package and the repo are public, so no credentials are needed.
#
# Setup (once): Control Panel -> Task Scheduler -> Create -> Scheduled
# Task -> User-defined script; User: root; Schedule: e.g. weekly Sunday
# 23:00 (before Monday's week-clean); Run command:
#   sh /volume1/docker/minion2/deploy/nas-update.sh
#
# First-time install into a folder that already has files (this is what
# `git clone` refuses with "destination path already exists / not
# empty"): drop this repo's files there any way you like, then just run
# the script -- it turns the folder into a proper checkout on the first
# run (git init + fetch + hard reset) and keeps your .env. There is no
# separate clone step.

set -eu

REPO_URL='https://github.com/coppermilk/minion2'

# Locate the repo from THIS script's own path (deploy/nas-update.sh),
# so there is nothing to edit: the checkout is the script's grandparent.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

LOG="$REPO/deploy/update.log"
LOCKDIR="$REPO/deploy/.update.lock"
IMAGE='ghcr.io/coppermilk/minion2:latest'

# DSM Task Scheduler hands the script a minimal PATH; locate tools by
# absolute path when `command -v` comes up empty.
DOCKER="$(command -v docker || echo /usr/local/bin/docker)"
GIT="$(command -v git || echo /usr/bin/git)"

# docker compose v2 (Container Manager plugin) vs the old v1 binary.
if "$DOCKER" compose version >/dev/null 2>&1; then
    compose() { "$DOCKER" compose "$@"; }
else
    DC="$(command -v docker-compose || echo /usr/local/bin/docker-compose)"
    compose() { "$DC" "$@"; }
fi

cd "$REPO"

# Bounded telemetry: roll the log past ~5 MB (keep one .old).
if [ -f "$LOG" ] && [ "$(wc -c <"$LOG")" -gt 5242880 ]; then
    mv "$LOG" "$LOG.old"
fi
exec >>"$LOG" 2>&1
echo "===== $(date '+%Y-%m-%d %H:%M:%S') update start ====="

# Single-flight: a slow model pull must not overlap the next schedule.
if ! mkdir "$LOCKDIR" 2>/dev/null; then
    echo 'another update is still running; skip'
    exit 0
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT

# --- 1. Self-healing sync to origin/main -----------------------------
# Works whether or not the folder is already a git repo, and never
# needs an empty directory: `git init` + hard reset overwrites tracked
# files in place where `git clone` would refuse. `.env` is git-ignored,
# so it is preserved untouched across every run.
if [ ! -d "$REPO/.git" ]; then
    echo 'no .git: initialising the checkout in place'
    "$GIT" init
fi
if "$GIT" remote get-url origin >/dev/null 2>&1; then
    "$GIT" remote set-url origin "$REPO_URL"
else
    "$GIT" remote add origin "$REPO_URL"
fi
"$GIT" fetch --prune origin main
"$GIT" reset --hard origin/main
echo "at $("$GIT" rev-parse --short HEAD)"

# --- 2. Pull the new image BEFORE touching the running bots ----------
# set -e aborts here on a bad pull, while the bots still serve the old
# image -- a failed pull never leaves them stopped.
compose pull

# --- 3. Clean recreate -----------------------------------------------
# down --remove-orphans drops containers (incl. any renamed/removed
# service) for a clean slate; up -d recreates from the pulled image.
# The ollama-models named volume and the /data weights survive `down`,
# so nothing re-downloads and the gap is seconds.
compose down --remove-orphans
compose up -d

# Remove now-dangling old layers so the NAS stays bounded.
"$DOCKER" image prune -f

# --- 4. Ensure the local model is present ----------------------------
# Read OLLAMA_MODEL from .env (default qwen2.5vl:7b), wait for the
# ollama service to answer, then pull. Idempotent: a no-op once the
# blob is present. Best-effort -- a failed pull is logged and retried
# next run, never aborting the deploy (so `set -e` is relaxed here).
MODEL="$(sed -n 's/^[[:space:]]*OLLAMA_MODEL[[:space:]]*=[[:space:]]*//p' \
    .env 2>/dev/null | tail -n1)"
[ -n "$MODEL" ] || MODEL='qwen2.5vl:7b'

echo "ensuring model $MODEL"
i=0
until compose exec -T ollama ollama list >/dev/null 2>&1; do
    i=$((i + 1))
    if [ "$i" -ge 30 ]; then
        echo 'ollama did not become ready; skipping model pull this run'
        break
    fi
    sleep 2
done
if [ "$i" -lt 30 ]; then
    if compose exec -T ollama ollama pull "$MODEL"; then
        echo "model ready: $MODEL"
    else
        echo "model pull failed; will retry next run"
    fi
fi

echo "===== $(date '+%Y-%m-%d %H:%M:%S') update done ====="
