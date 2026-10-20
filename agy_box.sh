#!/bin/sh
# 16.10 (решение Alex): Antigravity (agy) со своими инструментами — только в изолированном контейнере weather-lab-agybox.
# Песочница самого agy (--sandbox) не защищает: проверка 16.10 — читала папки сервера, выходила в сеть, писала в /tmp.
# Здесь внутри видно только: рабочую папку ($2 → /work), копию входа в Google (data/agy_box/home) и сам agy (только чтение).
# Нет проекта, .env, базы. Корень контейнера только для чтения, без прав root, без повышения прав, предел памяти/процессов.
# (без группы docker после перезагрузки — DOCKER="sudo -n docker")
# Запуск: agy_box.sh <файл письма> <рабочая папка> [секунд]  → JSON ответа agy в stdout.
set -e
LAB="$(cd "$(dirname "$0")" && pwd)"
PROMPT="$1"; WORK="$2"; T="${3:-300}"
# 16.10: у каждого письма — своя чистая папка входа (только ключи Google, без истории разговоров agy: conversations, brain,
# conversation_summaries) — ответы не зависят от других писем и вариантов. Обновлённый ключ входа — обратно в общую папку.
H="$LAB/data/agy_box/home"
TMPH="$(mktemp -d "$LAB/data/agy_box/tmp/h.XXXXXX")"
trap 'rm -rf "$TMPH"' EXIT
mkdir -p "$TMPH/.gemini/antigravity-cli"
for f in google_accounts.json oauth_creds.json jetski-standalone-oauth-token settings.json state.json projects.json trustedFolders.json \
         antigravity-cli/antigravity-oauth-token antigravity-cli/installation_id antigravity-cli/jetski_state.pbtxt antigravity-cli/settings.json; do
  [ -f "$H/.gemini/$f" ] && cp -p "$H/.gemini/$f" "$TMPH/.gemini/$f"
done
rc=0
timeout $((T + 30)) ${DOCKER:-docker} run --rm -i --user "${AGY_UID:-$(id -u)}:${AGY_GID:-$(id -g)}" --read-only --tmpfs /tmp:size=64m \
  --cap-drop ALL --security-opt no-new-privileges --memory 1g --pids-limit 256 \
  -e HOME=/home/agy -v "$TMPH:/home/agy" -v "$HOME/.local/bin/agy:/usr/local/bin/agy:ro" \
  -v "$(cd "$WORK" && pwd):/work" -w /work weather-lab-agybox \
  -p "$(cat "$PROMPT")" --model "${AGY_MODEL:-gemini-3.8-flash-medium}" --dangerously-skip-permissions --output-format json \
  --print-timeout "${T}s" || rc=$?
for f in antigravity-cli/antigravity-oauth-token oauth_creds.json; do
  if [ -f "$TMPH/.gemini/$f" ] && [ "$TMPH/.gemini/$f" -nt "$H/.gemini/$f" ]; then
    ( flock 9; cp -p "$TMPH/.gemini/$f" "$H/.gemini/$f.new" && mv "$H/.gemini/$f.new" "$H/.gemini/$f" ) 9>"$LAB/data/agy_box/tmp/.token.lock"
  fi
done
# 17.10 (Alex: «понять, что скилл сделал»): AGY_TRACE=<папка> — сохранить разговор agy (шаги, команды, вывод) без ключей входа
if [ -n "$AGY_TRACE" ]; then
  tar -czf "$AGY_TRACE/$(basename "$WORK").tgz" -C "$TMPH/.gemini/antigravity-cli" brain 2>/dev/null || true   # brain — шаги и вывод
fi
exit $rc
