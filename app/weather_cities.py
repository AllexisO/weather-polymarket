"""
Все города погодных маркетов Polymarket, которые резолвятся по METAR-
станции аэропорта (NOAA timeseries, см. описание маркета) — для
стратегии по живым замерам (weather_obs_live.py), истории цен
(weather_price_history.py) и истории замеров (weather_station_obs.py).

С 2026-09-23 (решение Alex) все 26 городов используют и прогнозные
модели (weather_edge.CITIES строится отсюда) — ради скорости набора
статистики по кошелькам (6 городов давали 3-6 ставок в день). До этого
прогнозы работали на 6 городах ("мало, но глубоко", решение 2026-09-22),
а 26 — только стратегия по замерам. Отобраны по объёму торгов. Гонконг не подходит —
резолвится по обсерватории (HKO), а не по METAR.

icao — станция из описания маркета; iem — код той же станции в архиве
Iowa Mesonet (у США — без "K"); nws5 — у станций США есть 5-минутные
замеры через api.weather.gov (Polymarket их НЕ засчитывает — резолюция
по METAR, проверено 2026-09-23; как сигнал не используются).
"""

# Чистые данные, без импортов: этот файл читает и дашборд (в его
# контейнере нет requests и остальных зависимостей коллектора).


def _c(icao, tz, unit, slug, nws5=False):
    iem = icao[1:] if icao.startswith("K") else icao
    return {"icao": icao, "iem": iem, "tz": tz, "unit": unit, "poly_slug": slug, "nws5": nws5}


# Точные координаты станций (aviationweather.gov stationinfo, 2026-09-23) —
# для прогнозных моделей (weather_edge.py/weather_multimodel.py).
COORDS = {
    'nyc': (40.7795, -73.8803),
    'toronto': (43.679, -79.629),
    'london': (51.505, 0.055),
    'paris': (48.967, 2.428),
    'madrid': (40.466, -3.555),
    'beijing': (40.082, 116.603),
    'atlanta': (33.6297, -84.4422),
    'miami': (25.7881, -80.3169),
    'los_angeles': (33.9382, -118.3866),
    'chicago': (41.9602, -87.9316),
    'dallas': (32.8384, -96.8358),
    'san_francisco': (37.6196, -122.3656),
    'houston': (29.6458, -95.2821),
    'denver': (39.713, -104.758),
    'seattle': (47.4447, -122.3144),
    'austin': (30.1831, -97.6806),
    'wellington': (-41.331, 174.806),
    'sao_paulo': (-23.432, -46.469),
    'panama_city': (8.967, -79.555),
    'tokyo': (35.553, 139.781),
    'shanghai': (31.146, 121.8),
    'helsinki': (60.327, 24.957),
    'mexico_city': (19.436, -99.072),
    'buenos_aires': (-34.822, -58.536),
    'seoul': (37.469, 126.451),
    'munich': (48.348, 11.813),
    'shenzhen': (22.639, 113.803),
    'manila': (14.507, 121.004),
    'warsaw': (52.163, 20.961),
    'busan': (35.179, 128.938),
    'qingdao': (36.362, 120.087),
    'guangzhou': (23.392, 113.307),
    'singapore': (1.368, 103.982),
    'milan': (45.631, 8.728),
    'amsterdam': (52.315, 4.79),
    'lucknow': (26.761, 80.889),
    'chongqing': (29.718, 106.639),
    'chengdu': (30.576, 103.95),
    'kuala_lumpur': (2.747, 101.714),
    'jeddah': (21.685, 39.166),
    'karachi': (24.902, 67.139),
    'cape_town': (-33.965, 18.602),
    'ankara': (40.128, 32.995),
    'moscow': (55.592, 37.261),
    'tel_aviv': (32.011, 34.887),
    'istanbul': (41.262, 28.74),
    'wuhan': (30.783, 114.205),
    'zhengzhou': (34.52, 113.834),
}


def _with_coords():
    for k, c in OBS_CITIES.items():
        c["lat"], c["lon"] = COORDS[k]


OBS_CITIES = {
    # 6 городов прогнозных моделей
    "nyc": _c("KLGA", "America/New_York", "fahrenheit", "nyc", True),
    "toronto": _c("CYYZ", "America/Toronto", "celsius", "toronto"),
    "london": _c("EGLC", "Europe/London", "celsius", "london"),
    "paris": _c("LFPB", "Europe/Paris", "celsius", "paris"),
    "madrid": _c("LEMD", "Europe/Madrid", "celsius", "madrid"),
    "beijing": _c("ZBAA", "Asia/Shanghai", "celsius", "beijing"),
    # только для стратегии по замерам (2026-09-23)
    "atlanta": _c("KATL", "America/New_York", "fahrenheit", "atlanta", True),
    "miami": _c("KMIA", "America/New_York", "fahrenheit", "miami", True),
    "los_angeles": _c("KLAX", "America/Los_Angeles", "fahrenheit", "los-angeles", True),
    "chicago": _c("KORD", "America/Chicago", "fahrenheit", "chicago", True),
    "dallas": _c("KDAL", "America/Chicago", "fahrenheit", "dallas", True),
    "san_francisco": _c("KSFO", "America/Los_Angeles", "fahrenheit", "san-francisco", True),
    "houston": _c("KHOU", "America/Chicago", "fahrenheit", "houston", True),
    "denver": _c("KBKF", "America/Denver", "fahrenheit", "denver", True),
    "seattle": _c("KSEA", "America/Los_Angeles", "fahrenheit", "seattle", True),
    "austin": _c("KAUS", "America/Chicago", "fahrenheit", "austin", True),
    "wellington": _c("NZWN", "Pacific/Auckland", "celsius", "wellington"),
    "sao_paulo": _c("SBGR", "America/Sao_Paulo", "celsius", "sao-paulo"),
    "panama_city": _c("MPMG", "America/Panama", "celsius", "panama-city"),
    "tokyo": _c("RJTT", "Asia/Tokyo", "celsius", "tokyo"),
    "shanghai": _c("ZSPD", "Asia/Shanghai", "celsius", "shanghai"),
    "helsinki": _c("EFHK", "Europe/Helsinki", "celsius", "helsinki"),
    "mexico_city": _c("MMMX", "America/Mexico_City", "celsius", "mexico-city"),
    "buenos_aires": _c("SAEZ", "America/Argentina/Buenos_Aires", "celsius", "buenos-aires"),
    "seoul": _c("RKSI", "Asia/Seoul", "celsius", "seoul"),
    "munich": _c("EDDM", "Europe/Berlin", "celsius", "munich"),
    # ещё 22 города с меньшим объёмом торгов ($2-23 тыс. в день), 2026-09-23,
    # решение Alex. Все — METAR через NOAA. Не подходят (своя метеослужба,
    # не METAR): Гонконг (HKO), Тайбэй, Цзинань.
    "shenzhen": _c("ZGSZ", "Asia/Shanghai", "celsius", "shenzhen"),
    "manila": _c("RPLL", "Asia/Manila", "celsius", "manila"),
    "warsaw": _c("EPWA", "Europe/Warsaw", "celsius", "warsaw"),
    "busan": _c("RKPK", "Asia/Seoul", "celsius", "busan"),
    "qingdao": _c("ZSQD", "Asia/Shanghai", "celsius", "qingdao"),
    "guangzhou": _c("ZGGG", "Asia/Shanghai", "celsius", "guangzhou"),
    "singapore": _c("WSSS", "Asia/Singapore", "celsius", "singapore"),
    "milan": _c("LIMC", "Europe/Rome", "celsius", "milan"),
    "amsterdam": _c("EHAM", "Europe/Amsterdam", "celsius", "amsterdam"),
    "lucknow": _c("VILK", "Asia/Kolkata", "celsius", "lucknow"),
    "chongqing": _c("ZUCK", "Asia/Shanghai", "celsius", "chongqing"),
    "chengdu": _c("ZUUU", "Asia/Shanghai", "celsius", "chengdu"),
    "kuala_lumpur": _c("WMKK", "Asia/Kuala_Lumpur", "celsius", "kuala-lumpur"),
    "jeddah": _c("OEJN", "Asia/Riyadh", "celsius", "jeddah"),
    "karachi": _c("OPKC", "Asia/Karachi", "celsius", "karachi"),
    "cape_town": _c("FACT", "Africa/Johannesburg", "celsius", "cape-town"),
    "ankara": _c("LTAC", "Europe/Istanbul", "celsius", "ankara"),
    "moscow": _c("UUWW", "Europe/Moscow", "celsius", "moscow"),
    "tel_aviv": _c("LLBG", "Asia/Jerusalem", "celsius", "tel-aviv"),
    "istanbul": _c("LTFM", "Europe/Istanbul", "celsius", "istanbul"),
    "wuhan": _c("ZHHH", "Asia/Shanghai", "celsius", "wuhan"),
    "zhengzhou": _c("ZHCC", "Asia/Shanghai", "celsius", "zhengzhou"),
}

_with_coords()
