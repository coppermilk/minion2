#!/bin/sh
# Synology NAS auto-update: pull latest main, rebuild, restart the bots.
#
# Runs from DSM Task Scheduler as a root user-defined script. The repo
# is public, so `git fetch` needs no credentials. Safe by design:
# the image is rebuilt BEFORE the running bots are touched, so a
# failed pull or build never leaves the bots stopped -- they keep
# serving the old image until a good build is ready.
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

# Fetch and hard-reset to origin/main. .env is git-ignored, so it is
# never touched; any accidental local edit to a tracked file is
# discarded on purpose -- this is a deploy checkout, not a workspace.
"$GIT" fetch --prune origin main
before="$("$GIT" rev-parse HEAD)"
"$GIT" reset --hard origin/main
after="$("$GIT" rev-parse origin/main)"

if [ "$before" = "$after" ]; then
    echo "already current at $after; bots left running"
    exit 0
fi
echo "updating $before -> $after"

# Build first (bots still up on the old image). set -e aborts here on
# a bad build, before anything is stopped.
compose build

# Clean swap: stop the old containers, start the new ones.
compose down
compose up -d

# Keep the NAS volume bounded: drop the now-dangling old layers.
"$DOCKER" image prune -f

echo "===== $(date '+%Y-%m-%d %H:%M:%S') update done ($after) ====="
