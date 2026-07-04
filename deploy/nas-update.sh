#!/bin/sh
# Synology NAS auto-update: pull the prebuilt image, restart the bots.
#
# The image is built on GitHub and published to GHCR (image.yml), so
# the NAS never compiles torch -- it just downloads the ready image,
# and thanks to the layer order only the small code layer changes.
#
# Runs from DSM Task Scheduler as a root user-defined script. The repo
# and (once you flip its visibility) the GHCR package are public, so
# both `git fetch` and `docker pull` need no credentials. Safe by
# design: the new image is pulled BEFORE the running bots are touched,
# so a failed fetch or pull never leaves the bots stopped -- they keep
# serving the old image until a good one is in hand.
#
# One-time: make the GHCR package public so the pull is anonymous --
# github.com/coppermilk?tab=packages -> minion2 -> Package settings ->
# Change visibility -> Public. (Otherwise add a `docker login ghcr.io`
# with a read:packages token before the pull below.)
#
# Setup (once): Control Panel -> Task Scheduler -> Create ->
# Scheduled Task -> User-defined script; User: root; Schedule: e.g.
# weekly Sunday 23:00 (before Monday's week-clean); Run command:
#   sh /volume1/docker/minion2/deploy/nas-update.sh
#
# First-time checkout (also a one-shot Task Scheduler command):
#   git clone https://github.com/coppermilk/minion2 /volume1/docker/minion2
# then copy your single .env into that folder.

set -eu

# --- EDIT ME: absolute path to the cloned repo on the NAS ----------
REPO='/volume1/docker/minion2'
# -------------------------------------------------------------------

LOG="$REPO/deploy/update.log"
LOCKDIR="$REPO/deploy/.update.lock"

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

# Single-flight: a slow build must not overlap the next schedule.
if ! mkdir "$LOCKDIR" 2>/dev/null; then
    echo 'another update is still running; skip'
    exit 0
fi
trap 'rmdir "$LOCKDIR" 2>/dev/null || true' EXIT

# Fetch and hard-reset to origin/main (compose file + this script).
# .env is git-ignored, so it is never touched; any accidental local
# edit to a tracked file is discarded on purpose -- this is a deploy
# checkout, not a workspace.
"$GIT" fetch --prune origin main
"$GIT" reset --hard origin/main
echo "at $("$GIT" rev-parse --short HEAD)"

# Remember the current image before pulling, so we can drop exactly
# that one afterwards if the pull brought a different digest.
IMAGE='ghcr.io/coppermilk/minion2:latest'
old_id="$("$DOCKER" images --no-trunc -q "$IMAGE" 2>/dev/null || true)"

# Converge to the published image, idempotently. `pull` grabs the new
# GHCR image (a fast no-op when the digest is unchanged); set -e
# aborts here on a bad pull, before the running bots are touched.
compose pull

# `up -d` recreates only the containers whose image actually changed
# and starts anything not yet running -- so the SAME task both does
# the first-ever start after a clone and every later update, with no
# needless restarts when nothing moved.
compose up -d

# The old image is now untagged and unused (the containers were
# recreated onto the new one). Remove it explicitly when the digest
# changed, then sweep any other dangling layers so the NAS stays
# bounded -- no pile of stale <none> images.
new_id="$("$DOCKER" images --no-trunc -q "$IMAGE" 2>/dev/null || true)"
if [ -n "$old_id" ] && [ "$old_id" != "$new_id" ]; then
    echo "removing old image $old_id"
    "$DOCKER" rmi "$old_id" 2>/dev/null || true
fi
"$DOCKER" image prune -f

echo "===== $(date '+%Y-%m-%d %H:%M:%S') update done ====="
