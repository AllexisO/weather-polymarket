#!/bin/sh
# 21.10 (решение Alex): «ИИ с дневником» в фоне — docs/DIARY.md. Gemma в виртуальной машине на NAS; не запускается, пока идёт прогон скилла.
# Запуск: ./diary_run.sh <шаг> <город> <месяц> [...]   например: ./diary_run.sh cycle atlanta 2025-12 2026-01 --days=3
cd /mnt/applications/docker/stacks/weather-lab || exit 1
mkdir -p data/research/diary
LOG="data/research/diary/$2_$1.log"
setsid nohup python3 app/diary_lab.py "$@" >> "$LOG" 2>&1 < /dev/null &
echo "запущено: $*, лог $LOG"
