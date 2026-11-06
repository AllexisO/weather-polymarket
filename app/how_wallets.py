"""
«Как работает» → «Кошельки» (05.11, Alex: «для каждого кошелька, который сейчас работает, вся логика от и до, со схемами»).

spec(wallet) — схема решения кошелька: узлы «когда → данные → модель → шанс → выбор → покупка → итог» и таблица параметров.
Утренние кошельки по снимкам (weather_paper.place) — собираются из настроек weather_paper (поле шансов, полосы, перевес, пределы
цены, ставка), то есть показывают то, что код делает сейчас; поменял настройку — схема поменялась сама. Кошельки со своим
скриптом (LLM, дневная модель, вечерние, повтор) — описаны здесь вручную по их коду и app/wallet_docs.py; поменял правило —
поправь и здесь. Полное описание (идея, проверки) — из wallet_docs.WALLET_DOCS.
"""

import ast
from pathlib import Path
from types import SimpleNamespace

_NEED = ("WALLETS", "MAKER_WALLETS", "NO_WALLETS", "EDGE_BY_WALLET", "MIN_EDGE", "NO_BAND", "NO_BAND_NOCAP", "TOP_RANK", "YES_BAND",
         "YES_FLOOR", "YES_CEIL", "CONF_SKIP", "CITY_PICK", "SHIFT_WALLETS", "PICK_ASK", "PICK_RATIO", "MIN_PRICE_BY_WALLET", "MIN_PRICE",
         "YES_MIN", "MAX_PRICE", "STAKE_BY_EDGE", "STAKE_BY_SHIFT", "STAKE_BY_WALLET", "STAKE", "FAST_WALLETS", "START_TS")
_SAFE = (ast.Expression, ast.Constant, ast.Dict, ast.Set, ast.List, ast.Tuple, ast.Name, ast.Load, ast.UnaryOp, ast.USub, ast.UAdd)


def _paper_settings():
    """Настройки кошельков из weather_paper.py — читаем текст, а не импортируем: в контейнере сайта нет requests и др. Берём только
    присваивания верхнего уровня из литералов (и имён ранее прочитанных констант, как START_TS → SHIFT_START_TS)."""
    tree = ast.parse((Path(__file__).parent / "weather_paper.py").read_text())
    ns = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and len(node.targets) == 1 and isinstance(node.targets[0], ast.Name):
            expr = ast.Expression(node.value)
            if all(isinstance(x, _SAFE) for x in ast.walk(expr)):
                try:
                    ns[node.targets[0].id] = eval(compile(expr, "weather_paper", "eval"), {"__builtins__": {}}, dict(ns))
                except (NameError, TypeError):
                    pass
    missing = [k for k in _NEED if k not in ns]
    assert not missing, f"how_wallets: в weather_paper.py не прочитаны {missing}"
    return SimpleNamespace(**{k: ns[k] for k in _NEED})


wp = _paper_settings()

# поле шансов в снимке → (короткое имя, что это)
MODELS = {
    "ml3_model_p": ("v3", "LightGBM: прогнозы 16 погодных моделей, утренние замеры, условия дня и мнение рынка в 08:00"),
    "ml3c_model_p": ("смесь v3", "35% v3 + 65% рынка"),
    "ml4_model_p": ("v4", "v3 с более сложными деревьями (31 лист)"),
    "ml4c_model_p": ("смесь v4", "35% v4 + 65% рынка"),
    "ml4e_model_p": ("v4e", "v4, среднее 3 обучений"),
    "ml4ec_model_p": ("смесь v4e", "35% v4e + 65% рынка"),
    "ml5_model_p": ("v5", "«от рынка»: отправная точка — ожидаемый максимум по ценам 08:00, модель учит поправку; среднее 3 обучений"),
    "ml5c_model_p": ("смесь v5", "35% v5 + 65% рынка"),
    "ml531_model_p": ("v5_31", "v5 с 31 листом, среднее 3 обучений"),
    "ml531c_model_p": ("смесь v5_31", "35% v5_31 + 65% рынка"),
    "ml531m_model_p": ("смесь v5_31 + поправка рынка", "35% v5_31 + 65% рынка, цены рынка поправлены: logit p′ = 0.066 + 1.061·logit p"),
    "v2b_model_p": ("П1", "«поправка к рынку» (Argon): LightGBM на каждый вариант поверх цены и медианы v5_31"),
    "v2o_model_p": ("П1 + замер", "П1 с признаком утреннего замера"),
}


def _c(p):
    return f"{p * 100:.0f}¢"


def _snapshot_spec(w):
    field = {**wp.WALLETS, **wp.MAKER_WALLETS, **wp.NO_WALLETS}[w]
    short, about = MODELS.get(field, (field, ""))
    no = w in wp.NO_WALLETS
    maker = w in wp.MAKER_WALLETS
    edge = wp.EDGE_BY_WALLET.get(w, wp.MIN_EDGE)
    side = "«нет»" if no else "«да»"
    blend = "смесь" in short
    fast = w in wp.FAST_WALLETS
    when = "08:00-08:30 местного — первый утренний снимок, где у модели есть оценка" if fast else "первый утренний снимок до 12:00 местного"
    # кандидаты
    cand = []
    if no and w in wp.NO_BAND:
        lo, hi = wp.NO_BAND[w]
        cand.append(f"варианты, где «да» стоит {_c(lo)}-{_c(hi)} и модель считает их переоценёнными")
    if w in wp.TOP_RANK:
        cand.append(f"только {wp.TOP_RANK[w]} самых дорогих варианта")
    if w in wp.YES_BAND:
        lo, hi = wp.YES_BAND[w]
        cand.append(f"«да» за {_c(lo)}-{_c(hi)}, где шанс выше цены")
    if w in wp.YES_FLOOR:
        cand.append(f"«да» не дешевле {_c(wp.YES_FLOOR[w])}" + (f" и не дороже {_c(wp.YES_CEIL[w])}" if w in wp.YES_CEIL else ""))
    if w in wp.CONF_SKIP:
        cand.append(f"рынок уверен (фаворит ≥ {_c(wp.CONF_SKIP[w])} доли) — пропуск дня")
    if w in wp.CITY_PICK:
        cfg = wp.CITY_PICK[w]
        cand.append(f"только лучшая половина городов: где {cfg['source']} за {cfg['look']} дней был точнее рынка (пересчёт раз в неделю)")
    if w in wp.SHIFT_WALLETS:
        cand.append(f"только если центр модели сдвинут от рынка на ≥ {wp.SHIFT_WALLETS[w]}°C")
    if not cand:
        cand.append("все варианты города с оценкой модели")
    need = "шанс выше цены" if edge == 0 else f"перевес ≥ {edge * 100:.0f} п.п."
    if w in wp.PICK_ASK:
        pick = f"наибольший перевес шанса над ценой продажи + комиссией, {need}"
    elif w in wp.PICK_RATIO:
        pick = "наибольшее отношение шанс / цена"
    else:
        pick = f"наибольший перевес (шанс {side} − цена), {need}"
    min_price = wp.MIN_PRICE_BY_WALLET.get(w, wp.MIN_PRICE)
    if not no and w not in wp.YES_BAND:
        min_price = max(min_price, wp.YES_MIN)
    if w in wp.YES_BAND:
        price_rule = "цена покупки в живом стакане — тоже в полосе"
    elif no:
        price_rule = f"цена «нет» {_c(min_price)}-{_c(wp.MAX_PRICE)}"
    else:
        price_rule = f"цена {_c(min_price)}-{_c(wp.MAX_PRICE)}"
    if w in wp.STAKE_BY_EDGE:
        mult, bank, lo_s, hi_s = wp.STAKE_BY_EDGE[w]
        stake = f"по перевесу: {mult:g} доли Келли от ${bank:.0f}, ${lo_s:g}-${hi_s:g}"
    elif w in wp.STAKE_BY_SHIFT:
        tiers = wp.STAKE_BY_SHIFT[w][1]
        stake = "по сдвигу центра v5 от рынка: " + ", ".join(f"≤ {lim:g}°C — ${s:g}" if lim < 50 else f"больше — ${s:g}" for lim, s in tiers)
    else:
        stake = f"${wp.STAKE_BY_WALLET.get(w, wp.STAKE):g}"
    if maker:
        buy = "своя заявка по нижней цене стакана, без комиссии; ждём, пока кто-то продаст, до полудня"
    else:
        lim = "шанс − перевес"
        if (no and w in wp.NO_BAND and w not in wp.NO_BAND_NOCAP) or w in wp.YES_BAND:
            lim += ", и не дороже цены + 1¢"
        buy = f"по живому стакану (чужие заявки), не дороже {lim}, с комиссией 0.05·p·(1−p)"
    nodes = [
        ("blue", "clock", "Когда", when),
        ("pink", "cloud", "Данные", "прогнозы 16 погодных моделей, замеры с утра, цены всех вариантов"),
        ("violet", "chart", "Модель", short + (f" — {about}" if about else "")),
        ("yellow", "target", "Выбор", "; ".join(cand) + f" → {pick}, {price_rule}"),
        ("green", "cart", "Покупка", f"{side}, {stake}; {buy}"),
        ("green", "coin", "Итог", "по итогу Polymarket: выиграл — $1 за долю, проиграл — 0"),
    ]
    params = [
        ("Когда решает", when),
        ("Модель", f"{short} ({field})" + (f" — {about}" if about else "")),
        ("Шанс", "смесь модели с рынком" if blend else "чистая модель, без смеси с рынком"),
        ("Сторона", side),
        ("Кандидаты", "; ".join(cand)),
        ("Выбор", pick),
        ("Цена", price_rule + (" — дешёвые «да» почти никогда не выигрывают" if not no and w not in wp.YES_BAND else "")),
        ("Ставка", stake + "; минимум 5 долей (правило Polymarket)"),
        ("Покупка", buy),
        ("Одна ставка", "на город и день маркета; не исполнилось — в этот день больше не пробует"),
        ("С какого времени", (wp.START_TS.get(w) or "")[:16].replace("T", " ") + " UTC" if wp.START_TS.get(w) else "с запуска кошельков"),
    ]
    return {"family": "Утренние по снимку (weather_paper.py)", "script": "weather_paper.py · weather_ml_fast.py", "nodes": nodes, "params": params}


_LLM_BUY = "по живому стакану, не дороже шанс − 5 п.п., с комиссией"
_LLM = {
    "llm_gem": ("Gemini 3.8 Flash через OpenRouter, длинное письмо с памятью ошибок", "шансы Gemini по вариантам с поправкой уверенности по прошлым ответам"),
    "llm_gem_agy": ("та же Gemini и то же письмо, но через Antigravity (бесплатно)", "шансы Gemini, поправка уверенности как у llm_gem"),
    "llm_agy": ("Gemini через Antigravity, короткое письмо, ответ — один максимум дня", "максимум ± разброс прошлых ошибок в этот час → шансы по вариантам (считает код)"),
    "llm_agy_nomem": ("как llm_agy, но в письме нет прошлых прогнозов и ошибок", "как у llm_agy — отличается только память"),
    "llm_agy_p": ("Gemini через Antigravity, короткое письмо, ответ — шансы по вариантам", "шансы Gemini как есть, без поправки уверенности"),
    "llm_mix": ("без своих запросов: Gemini этого часа + утренний LightGBM v3", "50% Gemini + 50% v3"),
    "llm_mix5": ("без своих запросов: Gemini этого часа + утренний LightGBM v5", "50% Gemini + 50% v5"),
    "llm_skill": ("Gemini через Antigravity как агент: читает SKILL.md и сама считает на Python", "шансы всех вариантов от скилла"),
}


def _llm_spec(w):
    who, prob = _LLM[w]
    when = "раз в день в 08:10 местного" if w == "llm_skill" else "каждый час с 08:05 местного, первый час с сигналом"
    nodes = [
        ("blue", "clock", "Когда", when),
        ("pink", "cloud", "Данные", "письмо о дне: прогнозы моделей, замеры с утра, цены всех вариантов"),
        ("violet", "robot", "LLM", who),
        ("violet", "chart", "Шанс", prob + "; варианты ниже уже измеренного максимума — 0"),
        ("yellow", "target", "Выбор", "лучший перевес «да» или «нет» ≥ 5 п.п. над ценой продавца, цена 10-90¢"),
        ("green", "cart", "Покупка", f"$2, {_LLM_BUY}"),
        ("green", "coin", "Итог", "по итогу Polymarket"),
    ]
    params = [("Когда решает", when), ("Кто считает", who), ("Шанс", prob), ("Города", "5 городов США; у llm_agy с 10.10 ещё Лондон, Нью-Йорк, Париж, Токио"),
              ("Выбор", "лучший перевес «да» или «нет» ≥ 5 п.п., цена 10-90¢"), ("Ставка", "$2, одна на город в день"), ("Покупка", _LLM_BUY)]
    return {"family": "LLM (weather_llm_hour.py)", "script": "weather_llm_skill.py" if w == "llm_skill" else "weather_llm_hour.py",
            "nodes": nodes, "params": params}


_DAY = {
    "ml_day": ("перевес «да» или «нет» ≥ 3 п.п. над ценой продавца, цена 3-95¢", "$2, по живому стакану, не дороже шанс − 3 п.п."),
    "ml_day_conf": ("как ml_day, но если фаворит ≥ 60¢ доли — пропуск этого часа", "$2, по живому стакану, не дороже шанс − 3 п.п."),
    "ml_day_fav": ("«да» 50-95¢ со смесью выше цены, наибольший перевес", "$2, не дороже min(смесь, цена + 1¢); вне 50-95¢ — нет"),
    "ml_day_nohigh": ("только 12 и 14 ч: «нет» на варианты выше максимума с утра, «да» 5-25¢, смесь ниже цены на 3+ п.п.",
                      "«нет» не дороже 1 − смесь − 3 п.п., $2"),
}


def _day_spec(w):
    pick, buy = _DAY[w]
    when = "12 и 14 ч местного" if w == "ml_day_nohigh" else "10, 12 и 14 ч местного (в :06), первый час с сигналом"
    nodes = [
        ("blue", "clock", "Когда", when),
        ("pink", "cloud", "Данные", "прогнозы на день, все замеры с утра (сводки не старше 2 ч), цена рынка в этот час"),
        ("violet", "chart", "Модель", "дневная LightGBM (ml_day): видит максимум с утра"),
        ("violet", "scale", "Шанс", "смесь 35% модели + 65% рынка"),
        ("yellow", "target", "Выбор", pick),
        ("green", "cart", "Покупка", buy + ", с комиссией"),
        ("green", "coin", "Итог", "по итогу Polymarket"),
    ]
    params = [("Когда решает", when), ("Модель", "дневная LightGBM, переобучается каждую ночь"), ("Шанс", "35% модели + 65% рынка"),
              ("Выбор", pick), ("Покупка", buy), ("Одна ставка", "на город в день; не исполнилось — больше не пробует")]
    return {"family": "Дневная модель (weather_ml_day.py)", "script": "weather_ml_day.py", "nodes": nodes, "params": params}


_EVE = {
    "fav_eve": ("20:00-20:14", "v3 накануне (q_eve)", ""),
    "fav_eve4e": ("20:00-20:14", "v4e накануне (q_eve4e, среднее 3 обучений)", ""),
    "fav_eve22": ("22:00-22:14", "v3 накануне (q_eve)", "только город-дни, где fav_eve в 20:00 не купил; "),
}


def _eve_spec(w):
    hours, model, extra = _EVE[w]
    when = f"{hours} местного НАКАНУНЕ — решение на завтра"
    nodes = [
        ("blue", "clock", "Когда", when),
        ("pink", "cloud", "Данные", "прогнозы 16 моделей «за сутки» (свежие дневные выпуски), условия ECMWF 11-17 ч, сегодняшний максимум (METAR), цены на завтра"),
        ("violet", "chart", "Модель", model + " — без замеров дня, их ещё нет"),
        ("violet", "scale", "Шанс", "смесь 35% модели + 65% рынка"),
        ("yellow", "target", "Выбор", extra + "«да» 50-95¢ со смесью выше цены, наибольший перевес"),
        ("green", "cart", "Покупка", "$2 по живому стакану, не дороже min(смесь, цена + 1¢); вне 50-95¢ — нет"),
        ("green", "coin", "Итог", "на следующий день, по итогу Polymarket"),
    ]
    params = [("Когда решает", when), ("Модель", model + ", переобучается каждую ночь в 05:20"), ("Шанс", "35% модели + 65% рынка"),
              ("Выбор", extra + "«да» 50-95¢, смесь выше цены, наибольший перевес"),
              ("Покупка", "$2, не дороже min(смесь, цена + 1¢), с комиссией"),
              ("Нет сводок METAR", "решение откладывается до следующего запуска в окне (каждые 5 мин)")]
    return {"family": "Вечерние (weather_fav_eve.py)", "script": f"weather_{w}.py", "nodes": nodes, "params": params}


def _copy_spec(w):
    nodes = [
        ("blue", "clock", "Когда", "как только сильный трейдер купил — только сделки накануне дня маркета"),
        ("pink", "user", "Кого", "30 лучших трейдеров погоды за 14 дней"),
        ("yellow", "target", "Выбор", "та же доля, что купил трейдер"),
        ("green", "cart", "Покупка", "$2, не дороже их цены + 2¢, с комиссией"),
        ("green", "coin", "Итог", "по итогу Polymarket"),
    ]
    params = [("Когда решает", "сразу после покупки сильного трейдера, только сделки накануне дня маркета"), ("Кого повторяем", "30 лучших трейдеров погоды за 14 дней"),
              ("Покупка", "$2, не дороже их цены + 2¢"), ("Старт", "$1000")]
    return {"family": "Повтор за трейдерами (weather_copy_live.py)", "script": "weather_copy_live.py", "nodes": nodes, "params": params}


def spec(w):
    if w in _EVE:
        return _eve_spec(w)
    if w in _DAY:
        return _day_spec(w)
    if w in _LLM:
        return _llm_spec(w)
    if w == "copy":
        return _copy_spec(w)
    if w in {**wp.WALLETS, **wp.MAKER_WALLETS, **wp.NO_WALLETS}:
        return _snapshot_spec(w)
    return None
