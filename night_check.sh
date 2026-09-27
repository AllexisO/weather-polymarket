#!/bin/bash
# Вечерняя проверка перед ночью (2026-09-28, просьба Alex: «в 23:30 видеть, что может сломаться ночью,
# и починить до сна»). Крон: 30 23 * * *. Контейнер не видит крон и docker хоста — поэтому сначала
# снимаем их сюда (data/night/), затем в контейнере запускается weather_night_check.py.
LAB=/mnt/applications/docker/stacks/weather-lab
cd "$LAB" || exit 1
mkdir -p data/night
crontab -l > data/night/crontab.txt 2>/dev/null
sudo docker ps -a --filter "name=weather-lab" --format '{{.Names}}|{{.State}}|{{.RunningFor}}|{{.Status}}' > data/night/docker_ps.txt 2>/dev/null
# режим: morning (07:00) / midday (13:00) / evening (23:30, по умолчанию)
exec sudo docker compose run --rm -e JOB_TIMEOUT=900 collector weather_night_check.py "${1:-evening}"
