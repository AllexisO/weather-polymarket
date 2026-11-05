#!/bin/bash
# Проверка после ЛЮБОГО изменения кода (правило Alex, 2026-09-27): все кошельки,
# колонки, сборка скриптов + все страницы дашборда. Не заканчивать работу, пока не зелёное.
# Запуск: ./check.sh   (код выхода 1 — есть ошибки)
cd "$(dirname "$0")"
fail=0
sudo docker compose run --rm collector preflight.py 2>&1 | grep -v -i warning; [ "${PIPESTATUS[0]}" = 0 ] || fail=1
echo; echo "Страницы дашборда:"
sudo docker compose restart dashboard >/dev/null 2>&1; sleep 5
wallets=$(sudo docker compose run --rm collector -c "import weather_paper as w; print(' '.join([*w.WALLETS, *w.MAKER_WALLETS, *w.NO_WALLETS, 'copy', 'obs', 'obs_fmi', 'obs_fast', 'obs_rt', 'obs_wethr', 'obs_speci', 'ml_day', 'ml_day_fav', 'ml_day_conf', 'fav_eve', 'fav_eve22', 'fav_eve4e', 'llm_gem', 'llm_ds', 'llm_mix', 'llm_cal', 'llm_agy', 'llm_agy_bet', 'llm_agy_nomem', 'llm_gem_agy', 'llm_agy_p', 'llm_agy_cal', 'llm_skill', 'llm_mix5']))" 2>/dev/null | tail -1)
for u in /dashboard /skill /skill/log /diary /diary/log /real /real/pool /real/pool_refill /real/pool_mix /real/llm /real/techno /real/dry /status /backup /paper /training /bets /events /audit /notes /services /system /system/jobs /system/jobs/weather_book_rec /system/flows /system/flows/live /how /how/wallet-live /how/wallets /how/wallets/fav /how/wallets/ml5_cal /how/wallets/no_mid /how/wallets/ml_day /how/wallets/llm_skill /how/wallets/fav_eve /how/wallets/copy /system/jobs/weather_paper /system/jobs/backup_db /system/jobs/weather_ml_day_train /cities /cities/chongqing /cities/nyc /traders /models /models/v3 /models/v5 /models/ens /models/main /models/day /llm "/llm?w=llm_ds&city=dallas" "/llm?w=llm_gem&city=miami&d=2026-10-03" "/llm?w=llm_ds&city=london" "/llm?w=llm_agy" "/llm?w=llm_agy_bet" "/llm?w=llm_agy_nomem&city=tokyo" "/llm?w=llm_mix" "/llm?w=llm_cal"; do
  c=$(curl -s -o /dev/null -w '%{http_code}' "localhost:8093$u"); [ "$c" = 200 ] || { echo "✗ $u → $c"; fail=1; }
done
for w in $wallets; do
  c=$(curl -s -o /dev/null -w '%{http_code}' "localhost:8093/paper?w=$w"); [ "$c" = 200 ] || { echo "✗ /paper?w=$w → $c"; fail=1; }
done
[ $fail = 0 ] && echo "✓ все страницы и кошельки открываются" && echo "ПРОВЕРКА ПРОЙДЕНА" || echo "ПРОВЕРКА НЕ ПРОЙДЕНА"
exit $fail
