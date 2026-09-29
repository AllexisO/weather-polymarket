"""
Правила готовых ботов из ссылок Alex (2026-09-29), на нашей смеси вместо их прогнозов:
technosheen/weatherbot — «да» 8-30¢, вариант в топ-4 по цене, смесь ≥ цены×(1+edge), один на город-день;
AadiXD200/polymarket-weather-bot — «нет» по цене 20-75¢, смесь ниже цены «да» на 8+ п.п.;
neobrother — «лесенка» из дешёвых вариантов ≤ 3¢, где смесь выше цены.
Июль-авг — по цене 08:00, с 21.08 — по настоящим сделкам. Порог: плюс после комиссии в обоих периодах. Только на копии.
"""
import weather_study_0926 as base
from weather_study_links import show


def techno(edge):
    def pick(d):
        rank = sorted(d["keys"], key=lambda k: -d["price"][d["keys"].index(k)])[:4]
        c = [(d["blend"][b] / p, b, p) for b, p in zip(d["keys"], d["price"])
             if b in rank and 0.08 <= p <= 0.30 and d["blend"][b] >= p * (1 + edge)]
        if not c:
            return []
        _, b, p = max(c)
        return [(b, "yes", p, 2.0)]
    return pick


def aadi(gap):
    def pick(d):  # цена «нет» 20-75¢ = цена «да» 25-80¢
        c = [(p - d["blend"][b], b, p) for b, p in zip(d["keys"], d["price"]) if 0.25 <= p <= 0.80 and d["blend"][b] < p - gap]
        if not c:
            return []
        _, b, p = max(c)
        return [(b, "no", 1 - p, 2.0)]
    return pick


def neo(d):
    return [(b, "yes", p, 0.25) for b, p in zip(d["keys"], d["price"]) if 0.003 <= p <= 0.03 and d["blend"][b] > p]


if __name__ == "__main__":
    days = base.load_days()
    ja = [d for d in days if "2026-07-01" <= d["date"] < "2026-09-01"]
    late = [d for d in days if d["date"] >= base.FIRST_TRADE]
    print("=== technosheen: «да» 8-30¢ в топ-4 по цене ===")
    for e in (0.0, 0.5, 1.0):
        show(f"смесь ≥ цены × {1 + e:.1f}", techno(e), ja, late)
    print("\n=== AadiXD200: «нет», цена «да» 25-80¢ ===")
    for g in (0.03, 0.08):
        show(f"смесь ниже цены на {g * 100:.0f}+ п.п.", aadi(g), ja, late)
    print("\n=== neobrother: лесенка из вариантов ≤ 3¢ ($0.25 каждый) ===")
    show("все варианты 0.3-3¢, где смесь выше цены", neo, ja, late)
