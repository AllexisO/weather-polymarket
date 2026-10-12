#!/bin/bash
# Резервная копия всех баз weather-lab (2026-10-02, решение Alex): база на одном SSD без зеркала — копия на другом
# физическом диске (пул personal_data, HDD). Запуск: крон каждую ночь в 03:10 (крон спокоен; в 03:45 — проверка базы).
#
# Как: VACUUM INTO из базы, открытой только на чтение, — целостная копия на один момент (режим WAL: скрипты продолжают
# писать, копия их не блокирует), заодно без пустых страниц; проверка копии (PRAGMA quick_check); сжатие zstd (~в 4 раза).
# Хранение: последние KEEP_DAYS дней + копии воскресений за KEEP_WEEKS недель. Итог — data/backup_status.json
# (его читает ежедневная проверка, weather_night_check.py).
# Ключи: .env → env.backup (права 600, папки 700). Восстановление ключей: cp "<папка>/env.backup" .env
# Восстановление: zstd -d "<папка>/polymarket_lab.sqlite3.zst" -o data/db/polymarket_lab.sqlite3 (при остановленном кроне).
set -u
cd /mnt/applications/docker/stacks/weather-lab || exit 1
DEST="/mnt/personal_data/Backups/Projects/Weather Polymarket"
KEEP_DAYS=7
KEEP_WEEKS=4
DAY=$(date +%F)
OUT="$DEST/$DAY"
mkdir -p "$OUT"
rm -f "$OUT/.files.tsv"
t0=$(date +%s)
status=ok
msg=""
# 11.10 (решение Alex): + стаканы бота-мейкера data/books — восстановить больше неоткуда
for f in data/db/*.sqlite3 data/books/*.sqlite3; do
  n=$(basename "$f")
  tmp="$OUT/$n"
  rm -f "$tmp" "$tmp.zst"
  if ! sqlite3 "file:$f?mode=ro" "VACUUM INTO '$tmp'" 2>>"$OUT/errors.txt"; then
    status=fail; msg+="$n: копия не создана; "; continue
  fi
  qc=$(sqlite3 "$tmp" "PRAGMA quick_check" 2>&1 | head -1)
  if [ "$qc" != "ok" ]; then status=fail; msg+="$n: проверка копии — $qc; "; fi
  src_b=$(stat -c %s "$f")
  if ! zstd -q -T0 -10 --rm "$tmp" -o "$tmp.zst"; then status=fail; msg+="$n: сжатие не удалось; "; fi
  # 11.10: по каждой базе — для страницы /backup (backup_report.py)
  printf '%s\t%s\t%s\t%s\n' "$n" "$src_b" "$(stat -c %s "$tmp.zst" 2>/dev/null || echo 0)" "$qc" >> "$OUT/.files.tsv"
done
cp data/db/*.json "$OUT/" 2>/dev/null
# 11.10 (решение Alex): скрипты и итоги исследований (не в GitHub); без копии базы для исследований и тестовой duckdb
if tar -C data -cf - --exclude='research/research.sqlite3*' --exclude='research/bench/c.duckdb' research | zstd -q -T0 -10 -o "$OUT/research.tar.zst" -f; then
  printf '%s\t%s\t%s\t%s\n' "research.tar" "$(du -sb --exclude='research.sqlite3*' --exclude='c.duckdb' data/research | cut -f1)" "$(stat -c %s "$OUT/research.tar.zst")" "ok" >> "$OUT/.files.tsv"
else status=fail; msg+="research: архив не создан; "; fi
# 02.10 (Alex: «если SSD умрёт, все ключи пропадут»): ключи API — тоже в копию, читать может только владелец
install -m 600 .env "$OUT/env.backup" || { status=fail; msg+=".env: не скопирован; "; }
chmod 700 "$OUT" "$DEST"
[ -s "$OUT/errors.txt" ] || rm -f "$OUT/errors.txt"
# старые копии: оставить KEEP_DAYS последних дней и воскресенья за KEEP_WEEKS недель
for d in "$DEST"/20??-??-??; do
  b=$(basename "$d")
  age=$(( ( $(date +%s) - $(date -d "$b" +%s) ) / 86400 ))
  [ "$age" -lt "$KEEP_DAYS" ] && continue
  [ "$(date -d "$b" +%u)" = "7" ] && [ "$age" -lt $(( KEEP_WEEKS * 7 )) ] && continue
  rm -rf "$d"
done
size=$(du -sb "$OUT" | cut -f1)
took=$(( $(date +%s) - t0 ))
printf '{"at": "%s", "status": "%s", "msg": "%s", "dir": "%s", "size_bytes": %s, "took_s": %s}\n' \
  "$(date -Iseconds)" "$status" "$msg" "$OUT" "$size" "$took" > data/backup_status.json
# 11.10: история и опись копий для страницы /backup (сайт диска копий не видит)
python3 "$(pwd)/backup_report.py" "$OUT" "$status" "$took" "$msg" || echo "отчёт для /backup не записан"
echo "$(date '+%F %T') копия $status за ${took} с, $(du -sh "$OUT" | cut -f1) — $OUT ${msg}"
[ "$status" = ok ]
