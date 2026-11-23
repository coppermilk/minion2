#!/bin/sh
# Отчёт о версии на «Версии» админки сайта (daetotyoma.com/admin/versions/): что стоит на NAS и прошёл ли деплой.
# Облако не видит NAS, поэтому NAS сообщает о себе сам. Вызовы:
#   sh deploy/report-version.sh deploy   1 "обновлено до 2209f14"          # после удачного up
#   sh deploy/report-version.sh rollback 0 "НЕЗДОРОВ: props, sort — откат"  # после отката
#   sh deploy/report-version.sh heartbeat                                   # раз в день из DSM Task Scheduler
#   sh deploy/report-version.sh error    0 "compose не прошёл политику"
# Аргументы: событие (deploy|rollback|heartbeat|start|stop|error), ok (1|0, по умолчанию по событию), сообщение одной строкой,
# необязательный файл с подробностями (текст, попадёт в details). Коммит — git rev-parse HEAD, образ — docker image inspect.
# Настройки из .env рядом с репозиторием: VERSIONS_KEY (ключ BOARD_KEY сайта, обязателен), VERSIONS_URL
# (по умолчанию https://daetotyoma.com/admin/versions), VERSIONS_PROJECT (minion2-nas). Нет сети — отчёт ложится в
# bots/_data/state/versions-outbox.jsonl (или deploy/.versions-outbox.jsonl) и досылается при следующем запуске. Никогда не падает:
# деплой не должен ломаться из-за отчёта (код возврата 0 всегда, кроме отсутствия ключа — тогда 0 и строка в лог).
set -u
EVENT="${1:-heartbeat}"; OK="${2:-}"; MESSAGE="${3:-}"; DETAILS_FILE="${4:-}"
HERE="$(cd "$(dirname "$0")" && pwd)"; REPO="$(cd "$HERE/.." && pwd)"
ENV_FILE="$REPO/.env"
val() { [ -f "$ENV_FILE" ] && sed -n "s/^[[:space:]]*$1[[:space:]]*=[[:space:]]*//p" "$ENV_FILE" | head -n 1 | sed "s/^['\"]//; s/['\"]$//" | tr -d '\r'; }
KEY="$(val VERSIONS_KEY)"; URL="$(val VERSIONS_URL)"; PROJECT="$(val VERSIONS_PROJECT)"; DRIVE="$(val DRIVE_NAS)"
[ -n "$URL" ] || URL='https://daetotyoma.com/admin/versions'
[ -n "$PROJECT" ] || PROJECT='minion2-nas'
STATE_DIR="${DRIVE:+$DRIVE/bots/_data/state}"; [ -n "$STATE_DIR" ] && [ -d "$STATE_DIR" ] || STATE_DIR="$HERE"
OUTBOX="$STATE_DIR/versions-outbox.jsonl"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') версии: $*"; }
if [ -z "$KEY" ]; then log "VERSIONS_KEY не задан в .env — отчёт не отправлен"; exit 0; fi
case "$EVENT" in deploy|rollback|heartbeat|start|stop|error) ;; *) log "неизвестное событие $EVENT"; exit 0 ;; esac
if [ -z "$OK" ]; then case "$EVENT" in rollback|error) OK=0 ;; *) OK=1 ;; esac; fi
# JSON-строка: кавычки, обратные слеши и переводы строк экранируются (без python и jq — на NAS их может не быть)
js() { printf '%s' "$1" | sed 's/\\/\\\\/g; s/"/\\"/g' | awk 'BEGIN{ORS=""} NR>1{print "\\n"} {print}'; }
COMMIT="$(git -C "$REPO" rev-parse HEAD 2>/dev/null || true)"
BRANCH="$(git -C "$REPO" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
IMAGE=""; DOCKER="$(command -v docker || echo /usr/local/bin/docker)"
if [ -x "$DOCKER" ]; then IMAGE="$("$DOCKER" image inspect ghcr.io/coppermilk/minion2:latest --format '{{index .RepoDigests 0}}' 2>/dev/null || true)"; fi
DETAILS=""; [ -n "$DETAILS_FILE" ] && [ -f "$DETAILS_FILE" ] && DETAILS="$(tail -c 3500 "$DETAILS_FILE")"
HOST="nas-$(hostname 2>/dev/null || echo nas)"
BODY="{\"event\":\"$EVENT\",\"ok\":$( [ "$OK" = "1" ] && echo true || echo false ),\"host\":\"$(js "$HOST")\",\"commit\":\"$(js "$COMMIT")\",\"branch\":\"$(js "$BRANCH")\",\"image\":\"$(js "$IMAGE")\",\"message\":\"$(js "$MESSAGE")\",\"details\":\"$(js "$DETAILS")\",\"finished_at\":\"$(date -u '+%Y-%m-%dT%H:%M:%SZ')\"}"
send() { curl -sS -m 20 -o /dev/null -w '%{http_code}' -X PUT "$URL/$PROJECT" -H 'Content-Type: application/json' -H "X-Board-Key: $KEY" --data-binary "$1" 2>/dev/null; }
# сначала — недосланное из прошлых запусков
if [ -s "$OUTBOX" ]; then
  REST="$OUTBOX.tmp"; : > "$REST"
  while IFS= read -r line; do [ -n "$line" ] || continue; code="$(send "$line")"; case "$code" in 2*) ;; *) printf '%s\n' "$line" >> "$REST" ;; esac; done < "$OUTBOX"
  mv "$REST" "$OUTBOX"; [ -s "$OUTBOX" ] || rm -f "$OUTBOX"
fi
CODE="$(send "$BODY")"
case "$CODE" in
  2*) log "отправлено: $EVENT ok=$OK ${COMMIT:+$(printf '%s' "$COMMIT" | cut -c1-7)} → $URL/$PROJECT" ;;
  4*) log "сайт отверг отчёт ($CODE) — проверьте VERSIONS_KEY или тело: $BODY" ;;
  *) printf '%s\n' "$BODY" >> "$OUTBOX"; log "сайт недоступен ($CODE) — отчёт отложен в $OUTBOX" ;;
esac
exit 0
