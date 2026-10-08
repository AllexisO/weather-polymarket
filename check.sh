#!/bin/bash
# Проверка после ЛЮБОГО изменения кода (правило Alex, 2026-09-27): все кошельки,
# колонки, сборка скриптов + все страницы дашборда. Не заканчивать работу, пока не зелёное.
# Запуск: ./check.sh   (код выхода 1 — есть ошибки)
cd "$(dirname "$0")"
fail=0
sudo docker compose run --rm collector preflight.py 2>&1 | grep -v -i warning; [ "${PIPESTATUS[0]}" = 0 ] || fail=1
echo; echo "Страницы дашборда:"
sudo docker compose restart dashboard >/dev/null 2>&1; sleep 5
wallets=$(sudo docker compose run --rm collector -c "import weather_paper as w; print(' '.join([*w.WALLETS, *w.MAKER_WALLETS, *w.NO_WALLETS, 'copy', 'obs', 'obs_fmi', 'obs_fast', 'obs_rt', 'obs_wethr']))" 2>/dev/null | tail -1)
for u in /real /status /paper /training /bets /events /audit /notes /services /cities /cities/chongqing /cities/nyc /traders /models /models/v3 /models/v5 /models/ens /models/main /models/day /mm /mm/mm_all /mm/mm_ws_z30?tab=fills /mm/mm_pol?tab=quotes /mm/mm_own?tab=markets /llm "/llm?w=llm_ds&city=dallas" "/llm?w=llm_gem&city=miami&d=2026-10-03" "/llm?w=llm_ds&city=london"; do
  c=$(curl -s -o /dev/null -w '%{http_code}' "localhost:8093$u"); [ "$c" = 200 ] || { echo "✗ $u → $c"; fail=1; }
done
for w in $wallets; do
  c=$(curl -s -o /dev/null -w '%{http_code}' "localhost:8093/paper?w=$w"); [ "$c" = 200 ] || { echo "✗ /paper?w=$w → $c"; fail=1; }
done
[ $fail = 0 ] && echo "✓ все страницы и кошельки открываются" && echo "ПРОВЕРКА ПРОЙДЕНА" || echo "ПРОВЕРКА НЕ ПРОЙДЕНА"
exit $fail
