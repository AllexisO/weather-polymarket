#!/bin/bash
# Ночная резервная копия базы на другой физический диск (2026-09-26, решение Alex).
# База: пул applications (SSD Samsung 500 ГБ) -> копии: пул personal_data (отдельный HDD).
# 1) .backup — согласованный снимок средствами SQLite, на тот же SSD (быстро: база
#    в режиме delete-journal, на время снимка запись в неё ждёт — поэтому снимок короткий);
# 2) проверка целостности снимка; 3) сжатие zstd сразу на HDD; 4) храним 7 последних.
# Восстановление: zstd -d polymarket_lab-ДАТА.sqlite3.zst -o polymarket_lab.sqlite3
set -euo pipefail
LAB=/mnt/applications/docker/stacks/weather-lab
SRC=$LAB/data/db/polymarket_lab.sqlite3
DST=/mnt/personal_data/backups/weather-lab
KEEP=7
TS=$(date +%F)
TMP=$LAB/data/db/.backup-$TS.sqlite3
mkdir -p "$DST"
trap 'rm -f "$TMP"' EXIT
start=$(date +%s)
sqlite3 "$SRC" ".timeout 120000" ".backup '$TMP'"
snap=$(( $(date +%s) - start ))
chk=$(sqlite3 "$TMP" "PRAGMA quick_check;")
if [ "$chk" != "ok" ]; then echo "$(date -Is) ОШИБКА: снимок повреждён: $chk"; exit 1; fi
zstd -q -T0 -3 -f "$TMP" -o "$DST/polymarket_lab-$TS.sqlite3.zst"
ls -1t "$DST"/polymarket_lab-*.sqlite3.zst | tail -n +$((KEEP + 1)) | xargs -r rm -f
size=$(du -h "$DST/polymarket_lab-$TS.sqlite3.zst" | cut -f1)
sqlite3 "$SRC" ".timeout 120000" "CREATE TABLE IF NOT EXISTS job_runs (job TEXT PRIMARY KEY, finished_at TEXT NOT NULL);
  INSERT OR REPLACE INTO job_runs VALUES ('db_backup', strftime('%Y-%m-%dT%H:%M:%S+00:00', 'now'));"
echo "$(date -Is) ок: снимок ${snap} с, копия $size, всего копий $(ls "$DST"/polymarket_lab-*.zst | wc -l), всего $(du -sh "$DST" | cut -f1)"
