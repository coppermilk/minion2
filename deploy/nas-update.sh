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
# Everything rides that one image now, including the Telethon aggregator
# userbot (telethon is baked in via the `tg` extra) -- there is no separate
# premium-emoji image to build any more.
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
# The aggregator runs as its OWN compose project (isolated CPU, independent
# restart); this file + project name deploy it alongside the main stack.
AGG_FILE="$REPO/docker-compose.aggregator.yml"
AGG_PROJECT='minion2-agg'

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
# The lock is released by the EXIT trap -- but a hard kill (NAS reboot,
# OOM, DSM killing the task, `kill -9`) skips the trap and leaves a STALE
# lock that would then block EVERY future run forever. So first clear a
# lock older than 2h (no healthy run lasts that long), then acquire.
if [ -d "$LOCKDIR" ] \
    && [ -n "$(find "$LOCKDIR" -maxdepth 0 -mmin +120 2>/dev/null)" ]; then
    echo 'stale lock (>2h old); clearing it'
    rmdir "$LOCKDIR" 2>/dev/null || true
fi
if ! mkdir "$LOCKDIR" 2>/dev/null; then
    echo 'another update is still running; skip'
    exit 0
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT

# --- 1. Self-healing sync to origin/main -----------------------------
# Works whether or not the folder is already a git repo, and never
# needs an empty directory: `git init` + hard reset overwrites tracked
# files in place where `git clone` would refuse. `.env` is git-ignored,
# so it is preserved untouched across every run -- and so is a
# `telethon.session` file kept in the checkout: `git reset --hard` never
# touches ignored, untracked files, and this script runs no `git clean`.
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

# --- 3. Clean recreate (resilient to broken/stuck containers) --------
# down --remove-orphans drops containers (incl. any renamed/removed
# service) for a clean slate; up -d recreates from the pulled image.
# The ollama-models named volume and the /data weights survive `down`,
# so nothing re-downloads and the gap is seconds.
#
# Relax `set -e` for the teardown+up: a single broken container (state
# Dead/Removing, or a leftover from an interrupted run causing a
# "container name already in use" conflict) must NOT abort the script
# and leave the whole stack stopped. Tear down best-effort, force-remove
# any project containers that survived, then bring the stack up with one
# retry -- and only then fail loudly if it truly did not come up.
set +e
compose down --remove-orphans --timeout 30

# Force-remove any project containers still lingering (stuck/broken), so
# the recreate below can never die on a name conflict.
strays="$(compose ps -aq 2>/dev/null)"
[ -n "$strays" ] && "$DOCKER" rm -f $strays 2>/dev/null

# The aggregator now rides the shared image; drop the obsolete standalone
# premium-emoji image if an earlier version of this deploy built it
# (`image prune` only removes DANGLING images, not this tagged one).
"$DOCKER" image rm -f minion2-premium-emoji 2>/dev/null

compose up -d --remove-orphans
up_rc=$?
if [ "$up_rc" -ne 0 ]; then
    echo "up failed (rc=$up_rc); retrying once after 5s"
    sleep 5
    compose up -d --remove-orphans
    up_rc=$?
fi
set -e
if [ "$up_rc" -ne 0 ]; then
    echo 'ERROR: stack did not come up (see errors above)'
    exit 1
fi

# --- 3b. The aggregator: its OWN compose project ---------------------
# Separate project so its CPU is capped and a runaway can be restarted alone
# without touching the main stack (the main `down --remove-orphans` above
# drops the old in-project aggregator container; this recreates it here).
# Best-effort: a failure is logged but never aborts the rest of the deploy.
if [ -f "$AGG_FILE" ]; then
    set +e
    compose -p "$AGG_PROJECT" -f "$AGG_FILE" pull
    compose -p "$AGG_PROJECT" -f "$AGG_FILE" up -d --remove-orphans
    agg_rc=$?
    set -e
    if [ "$agg_rc" -eq 0 ]; then
        echo 'aggregator project up'
    else
        echo 'WARN: aggregator project did not come up (see above)'
    fi
fi

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
