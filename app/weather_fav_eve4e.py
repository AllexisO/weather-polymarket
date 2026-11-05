"""
fav накануне на v4e — кошелёк fav_eve4e (05.11, Alex «да, делаем»).

Тот же код, что weather_fav_eve.py (20:00-20:14 местного накануне, правило fav, смесь 35/65, $2), меняем одно: модель — v4e накануне
(31 лист, среднее 3 обучений; weather_ml_live.train_eve4e → data/ml/q_eve4e_s*) вместо v3 (q_eve). Ставит независимо от fav_eve.
Зачем: по неделям 21.08-02.11 (data/research/_fav_eve_models_1105.py, строгий порог до результата — больше v3 в $ всего и в каждой
половине, обе ≥ +5%, удача ≤ 1%): v4e 168 ставок +$69.40 (+22.3% / +18.9%, удача 0.0%) против v3 125, +$50.11 (+21.1% / +19.0%).
Шум обучения v3 (_fav_eve_seeds_1105.py): другое зерно +19.5%, среднее 3 обучений +17.0% — v3 устойчива, разница не от зерна.
Порог шага 1 — заметка (решение 27.11). Следы — fav_eve4e_preds. Запуск: docker compose run --rm collector weather_fav_eve4e.py [--dry] [--city london]
"""

import weather_fav_eve as fe

fe.WALLET, fe.JOB, fe.PREDS_TABLE, fe.MODEL = "fav_eve4e", "weather_fav_eve4e", "fav_eve4e_preds", "q_eve4e"

if __name__ == "__main__":
    fe.main()
