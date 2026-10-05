"""
Сигналы AI RainBot («Анализ Топ-15» на rainbot.finance) — прибыльны ли (2026-09-30, Alex: «ради интереса»).
Их AI-анализ не публичный (считается их ключом по кнопке) — вызывать его скриптом не будем. Alex сохраняет выдачу (PDF),
сигналы переносятся в data/research/rainbot_ai/<день>.json (action, question, city, bucket, yes_price на момент анализа).
Итог — у Polymarket (gamma events, закрытый маркет). Ставка $2 на сторону сигнала по их цене + 1¢, комиссия забирающего
0.05 × p × (1 − p) на долю. Рядом — что на том же варианте было у нас (смесь v3+рынок в первом снимке дня, если город наш).
Запуск: docker compose run --rm collector weather_study_rainbot_ai.py
"""
import glob
import json
import os
import re
import sqlite3
from datetime import datetime

import requests

DIR = "/data/research/rainbot_ai"
GAMMA = "https://gamma-api.polymarket.com"
STAKE = 2.0


def outcome(q, day):
    city = re.search(r"temperature in (.+?) be ", q).group(1)
    city = re.sub(r"\s*\(.*?\)", "", city).strip().lower().replace(" ", "-")
    city = {"new-york-city": "nyc"}.get(city, city)   # 01.10: у Polymarket в адресе маркета Нью-Йорк — «nyc»
    d = datetime.strptime(day + " 2026", "%B %d %Y")
    kind = "highest" if "highest" in q else "lowest"
    slug = f"{kind}-temperature-in-{city}-on-{d.strftime('%B').lower()}-{d.day}-{d.year}"
    ev = requests.get(f"{GAMMA}/events", params={"slug": slug}, timeout=30).json()
    if not ev:
        return None, "маркет не найден"
    for m in ev[0]["markets"]:
        if m.get("question", "").strip() == q.strip():
            if not m.get("closed"):
                return None, "ещё не закрыт"
            pr = json.loads(m["outcomePrices"])
            return float(pr[0]), None
    return None, "вариант не найден"


def main():
    tot = {"n": 0, "won": 0, "pnl": 0.0, "stake": 0.0, "wait": 0}
    for f in sorted(glob.glob(os.path.join(DIR, "*.json"))):
        sigs = json.load(open(f))
        print(f"== {os.path.basename(f)}: сигналов {sum(s['action'] != 'SKIP' for s in sigs)}")
        for s in sigs:
            if s["action"] == "SKIP" or s.get("yes_price") is None:
                continue
            fin, why = outcome(s["question"], s["day"])
            yes = s["action"] == "BUY YES"
            p = min((s["yes_price"] if yes else 1 - s["yes_price"]) + 0.01, 0.99)
            if fin is None:
                tot["wait"] += 1
                print(f"   {s['action']:8s} {s['city'][:16]:16s} {s['bucket']:18s} по {p * 100:.0f}¢ — {why}")
                continue
            win = fin > 0.5 if yes else fin < 0.5
            sh = STAKE / (p + 0.05 * p * (1 - p))
            pnl = (sh if win else 0.0) - STAKE
            tot["n"] += 1; tot["won"] += win; tot["pnl"] += pnl; tot["stake"] += STAKE
            print(f"   {s['action']:8s} {s['city'][:16]:16s} {s['bucket']:18s} по {p * 100:.0f}¢ — {'угадал' if win else 'мимо'} {pnl:+.2f}$")
    if tot["n"]:
        print(f"\nИТОГ: {tot['n']} ставок по $2, угадано {tot['won']}, итог {tot['pnl']:+.2f}$ ({100 * tot['pnl'] / tot['stake']:+.1f}% от поставленного); ждут итога {tot['wait']}")
    else:
        print(f"\nзакрытых пока нет; ждут итога {tot['wait']}")


if __name__ == "__main__":
    main()
