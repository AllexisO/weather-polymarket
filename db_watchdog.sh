#!/bin/bash
# Сторож базы (2026-09-27, требование Alex: «если зависло — сразу откинуть»).
# Крон каждые 2 минуты. Проверяет, что в базу можно писать (app/db_check.py, ждёт до 20 с).
# 3 неудачи подряд (~6 мин) — снимает виновника:
#   1) останавливает скрипты крона (collector), идущие дольше 10 минут, — по одному, с самого
#      долгого, после каждого проверяя, освободилась ли база;
#   2) если не помогло — перезапускает слушатель (copier) и сайт (dashboard);
#   3) пишет data/ALERT_DB_LOCKED — сайт показывает красную плашку 24 часа.
# Лог: data/logs/db_watchdog.log (пишется только при проблемах).
LAB=/mnt/applications/docker/stacks/weather-lab
cd "$LAB" || exit 1
STATE=data/db_watchdog.state
ALERT=data/ALERT_DB_LOCKED
LOG=data/logs/db_watchdog.log
# для проверки сторожа: WATCH_DB=/data/research/research.sqlite3 MIN_AGE=30 ./db_watchdog.sh
DBENV="-e POLY_LAB_DB=${WATCH_DB:-/data/db/polymarket_lab.sqlite3}"
MIN_AGE=${MIN_AGE:-600}
log() { echo "$(date '+%F %T') $*" >> "$LOG"; }

if timeout 60 sudo docker exec $DBENV weather-lab-dashboard python /app/db_check.py >/dev/null 2>&1; then
  [ -f "$STATE" ] && { log "база снова свободна"; rm -f "$STATE"; }
  exit 0
fi
# сайт не отвечает — проверяем отдельным контейнером
if ! sudo docker ps --format '{{.Names}}' | grep -q '^weather-lab-dashboard$'; then
  timeout 90 sudo docker compose run --rm $DBENV -e JOB_TIMEOUT=60 collector db_check.py >/dev/null 2>&1 && exit 0
fi
n=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$n" > "$STATE"
log "база занята (проверка $n подряд)"
[ "$n" -lt 3 ] && exit 0

log "СНИМАЮ БЛОКИРОВКУ"
free() { timeout 60 sudo docker exec $DBENV weather-lab-dashboard python /app/db_check.py >/dev/null 2>&1; }
now=$(date +%s)
stopped=""
# по одному, начиная с самого долгого; после каждого — проверка, не освободилась ли база
for row in $(for id in $(sudo docker ps --filter name=weather-lab-collector-run --format '{{.ID}}'); do
               echo "$(( now - $(date -d "$(sudo docker inspect -f '{{.State.StartedAt}}' "$id")" +%s) )):$id"; done | sort -t: -k1 -nr); do
  age=${row%%:*}; id=${row#*:}
  [ "$age" -gt "$MIN_AGE" ] || continue
  what=$(sudo docker inspect -f '{{join .Args " "}}' "$id" | tr '\n' ' ' | cut -c1-120)
  sudo docker stop -t 20 "$id" >/dev/null
  log "остановлен скрипт: $what (шёл $((age / 60)) мин)"
  stopped="$stopped $what;"
  sleep 3
  if free; then log "база освободилась"; break; fi
done
if ! free; then
  # скрипты ни при чём — долгие соединения у слушателя и сайта
  sudo docker compose restart copier dashboard >/dev/null 2>&1
  log "перезапущены copier и dashboard"
  stopped="$stopped перезапущены слушатель и сайт;"
fi
echo "$(date '+%d.%m %H:%M') база была занята ~$((n * 2)) мин — сторож снял блокировку:${stopped:- сама освободилась}" > "$ALERT"
rm -f "$STATE"
