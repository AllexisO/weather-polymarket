#!/bin/sh
# 17.10 (решение Alex): скилл agy с Claude Code вместо agy — отсеять варианты, не тратя лимит Gemini (он общий с живыми agy).
# Та же изолированная коробка, что agy_box.sh (weather-lab-agybox): видна только рабочая папка ($2 → /work), чистая папка входа
# на каждое письмо, корень только для чтения, без root. Вход — отдельный ключ Alex (claude setup-token) из data/agy_box/claude_token:
# не вход этой сессии, обновлять его внутри нечего. Запуск: claude_box.sh <файл письма> <рабочая папка> [секунд] → JSON claude -p.
set -e
LAB="$(cd "$(dirname "$0")" && pwd)"
PROMPT="$1"; WORK="$2"; T="${3:-300}"
TMPH="$(mktemp -d "$LAB/data/agy_box/tmp/c.XXXXXX")"
# CLAUDE_TRACE=<папка> — сохранить запись разговора (какие команды Claude запускал), 17.10 разбор «почему Claude хуже Gemini»
trap '[ -n "$CLAUDE_TRACE" ] && cp "$TMPH"/home/.claude/projects/*/*.jsonl "$CLAUDE_TRACE/$(basename "$WORK").jsonl" 2>/dev/null; rm -rf "$TMPH"' EXIT
mkdir -p "$TMPH/home/.claude"
umask 077
printf 'CLAUDE_CODE_OAUTH_TOKEN=%s\nDISABLE_AUTOUPDATER=1\nCLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC=1\nHOME=/home/agy\n' \
  "$(tr -d '\n' < "$LAB/data/agy_box/claude_token")" > "$TMPH/env"
# MAX_THINKING_TOKENS — размышление перед ответом (17.10: не подействовало, ~260 токенов как без него); CLAUDE_EFFORT=high|max — флаг --effort
[ -n "$MAX_THINKING_TOKENS" ] && echo "MAX_THINKING_TOKENS=$MAX_THINKING_TOKENS" >> "$TMPH/env"
chmod 644 "$TMPH/env"; chmod 777 "$TMPH/home" "$TMPH/home/.claude"
BIN="$(readlink -f "$HOME/.local/bin/claude")"
# 18.10: точная модель вместо псевдонима (псевдоним Claude Code сам выбирает последнюю версию)
case "${CLAUDE_MODEL:-haiku}" in haiku) CM=claude-haiku-5-5;; sonnet) CM=claude-sonnet-5-5;; *) CM="$CLAUDE_MODEL";; esac
timeout $((T + 30)) ${DOCKER:-docker} run --rm -i --user "${AGY_UID:-$(id -u)}:${AGY_GID:-$(id -g)}" --read-only --tmpfs /tmp:size=64m \
  --cap-drop ALL --security-opt no-new-privileges --memory 1g --pids-limit 256 \
  --env-file "$TMPH/env" -v "$TMPH/home:/home/agy" -v "$BIN:/usr/local/bin/claude:ro" \
  -v "$(cd "$WORK" && pwd):/work" -w /work --entrypoint timeout weather-lab-agybox "$T" \
  claude -p "$(cat "$PROMPT")" --model "$CM" ${CLAUDE_EFFORT:+--effort "$CLAUDE_EFFORT"} --output-format json --dangerously-skip-permissions
