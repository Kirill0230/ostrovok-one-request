import math
import random
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

from .catalog import CITIES, MEALS, MEAL_RANK, MONTHS_PREP, ORIGINS, TYPE_LABELS, hours, rub
from .models import Prefs, Priorities, Travellers


def flight_hours(c: dict, origin: str) -> float:
    o = ORIGINS.get(origin, ORIGINS["moscow"])
    shift = o["east_shift"] if c["tz"] >= 5 else o["west_shift"]
    return max(1.5, c["flight_hours"] + shift)


def pax_flight(t: Travellers) -> float:
    kids = sum(0.1 if a < 2 else 0.75 for a in (t.child_ages + [7] * (t.children - len(t.child_ages))))
    return t.adults + kids


def room_factor(t: Travellers) -> float:
    rooms = max(1, math.ceil(t.adults / 2))
    f = 0.85 if t.adults == 1 else float(rooms)
    return f + 0.2 * t.children


def season_k(c: dict, d: date) -> float:
    if (d.month == 12 and d.day >= 25) or (d.month == 1 and d.day <= 8):
        return 1.35
    return 1.15 if d.month in c["best_months"] else 0.85


def climate(c: dict, month: int) -> Dict[str, Optional[int]]:
    i = month - 1
    return {"air": c["climate"]["air"][i], "sea": c["climate"]["sea"][i], "rain": c["climate"]["rain"][i]}


def estimate_trip(c: dict, origin: str, d: date, nights: int, t: Travellers) -> Dict[str, int]:
    o = ORIGINS.get(origin, ORIGINS["moscow"])
    flights = c["flight_one_way"] * 2 * o["price_k"] * season_k(c, d) * pax_flight(t)
    hotel = c["hotel_night"] * season_k(c, d) * room_factor(t) * nights
    transfer = 2 * (800 + 45 * c["airport_km"]) * c["cost_k"] * (1.6 if t.adults + t.children > 3 else 1)
    pets = 2 * 6000 + 1200 * nights if t.pets else 0
    total = flights + hotel + transfer + pets
    return {"flights": int(flights), "hotel": int(hotel), "transfer": int(transfer), "total": int(round(total, -3))}


def _norm(values: List[float], v: float, invert: bool = False) -> float:
    lo, hi = min(values), max(values)
    x = 0.5 if hi == lo else (v - lo) / (hi - lo)
    return 1 - x if invert else x


def suggest_destinations(origin: str, d: date, nights: int, t: Travellers, prefs: Prefs,
                         priorities: Priorities, budget: Optional[int], budget_level: Optional[str],
                         exclude: List[str], limit: int = 3) -> Dict:
    month = d.month
    pool = [c for c in CITIES.values() if c["id"] not in exclude]
    funnel = [f"{len(CITIES)} направлений в каталоге"]
    if prefs.candidates:
        pool = [c for c in pool if c["id"] in prefs.candidates] or pool
        funnel.append(f"{len(pool)} — по названию страны/города")

    level_cap = {"low": 1, "mid": 3, "high": 4}.get(budget_level or "", None)
    if prefs.max_price_level:
        level_cap = min(level_cap or 4, prefs.max_price_level)

    est = {c["id"]: estimate_trip(c, origin, d, nights, t) for c in pool}

    filters: List[Tuple[str, callable, float]] = []
    if prefs.max_flight_hours:
        filters.append((f"перелёт ≤ {hours(prefs.max_flight_hours)}", lambda c: flight_hours(c, origin) <= prefs.max_flight_hours, 2))
    if prefs.min_temp:
        filters.append((f"тепло от +{prefs.min_temp}°", lambda c: climate(c, month)["air"] >= prefs.min_temp, 2))
    if prefs.max_temp:
        filters.append((f"не жарче +{prefs.max_temp}°", lambda c: climate(c, month)["air"] <= prefs.max_temp, 2))
    if "beach" in prefs.trip_types:
        filters.append(("купальный сезон", lambda c: "beach" in c["types"] and (climate(c, month)["sea"] or 0) >= 23, 3))
        filters.append(("не сезон дождей", lambda c: climate(c, month)["rain"] < 15, 2))
    if prefs.no_visa:
        filters.append(("без визы", lambda c: c["visa_free"], 2))
    if prefs.direct_only:
        filters.append(("прямой рейс", lambda c: c["direct"], 1))
    if budget:
        filters.append((f"бюджет {rub(budget)}", lambda c: est[c["id"]]["total"] <= budget * 1.05, 3))
    elif level_cap:
        filters.append(("уровень цен", lambda c: c["price_level"] <= level_cap, 1))
    if t.pets:
        filters.append(("удобно с питомцем", lambda c: flight_hours(c, origin) <= 6, 1))

    violations = {c["id"]: [(n, w) for n, f, w in filters if not f(c)] for c in pool}
    strict = [c for c in pool if not violations[c["id"]]]
    for n, f, _ in filters:
        funnel.append(f"{n}: {sum(1 for c in pool if f(c))}")
    funnel.append(f"подходят полностью: {len(strict)}")

    fh = [flight_hours(c, origin) for c in pool]
    totals = [est[c["id"]]["total"] for c in pool]
    kids = t.children > 0
    w_price = priorities.price + (1 if budget_level == "low" else 0)
    w_flight = priorities.flight_time + (1.5 if kids else 0) + (1 if t.pets else 0)

    scored = []
    for c in pool:
        cl = climate(c, month)
        s_season = 1.0 if month in c["best_months"] else 0.35
        s_rain = 1 - min(cl["rain"], 20) / 20
        s_types = (len(set(prefs.trip_types) & set(c["types"])) / len(prefs.trip_types)) if prefs.trip_types else 0.6
        s_price = _norm(totals, est[c["id"]]["total"], invert=True)
        s_flight = _norm(fh, flight_hours(c, origin), invert=True)
        s_kids = c["kids"] / 3 if kids else 0.5
        if budget_level == "high":
            s_price = 0.5 + 0.5 * c["price_level"] / 4
        score = (2.5 * s_season + 1.2 * s_rain + 2.5 * s_types + w_price * s_price + w_flight * s_flight
                 + (2 if kids else 0) * s_kids)
        scored.append((score, c))
    scored.sort(key=lambda x: (sum(w for _, w in violations[x[1]["id"]]), -x[0]))
    max_score = max((sc for sc, _ in scored), default=1)
    shown = scored[:limit]
    relaxed = list(dict.fromkeys(n for _, c in shown for n, _ in violations[c["id"]]))
    totals = [est[c["id"]]["total"] for _, c in shown] or [0]
    fh = [flight_hours(c, origin) for _, c in shown] or [0]

    options = []
    cheapest = min(totals) if totals else 0
    fastest = min(fh) if fh else 0
    for score, c in shown:
        cl = climate(c, month)
        e = est[c["id"]]
        h = flight_hours(c, origin)
        why, tradeoffs, risks = [], [], []
        if violations[c["id"]]:
            tradeoffs.append("Не проходит по условию: " + ", ".join(n for n, _ in violations[c["id"]]))
        mp = MONTHS_PREP[month - 1]
        if cl["sea"]:
            why.append(f"В {mp}: воздух +{cl['air']}°, море +{cl['sea']}°")
        else:
            why.append(f"В {mp}: днём около +{cl['air']}°")
        why.append(f"Лететь {hours(h)}" + (", прямой рейс" if c["direct"] else ", с пересадкой"))
        if budget and e["total"] <= budget:
            why.append(f"≈ {rub(e['total'])} — укладываемся в бюджет {rub(budget)}")
        else:
            why.append(f"≈ {rub(e['total'])} на всех за {nights} ноч.")
        matched = [TYPE_LABELS[x] for x in prefs.trip_types if x in c["types"]]
        if matched:
            why.append("Подходит под запрос: " + ", ".join(matched))
        if kids and c["kids"] >= 3:
            why.append("Отлично с детьми: семейные отели, анимация")
        if month in c["best_months"]:
            why.append("Сейчас лучший сезон")

        if h - fastest >= 2:
            tradeoffs.append(f"Перелёт на {hours(h - fastest)} дольше самого близкого варианта")
        if e["total"] - cheapest >= 10000:
            tradeoffs.append(f"На {rub(e['total'] - cheapest)} дороже самого бюджетного варианта")
        if cl["sea"] and cl["sea"] < 23:
            tradeoffs.append(f"Море +{cl['sea']}° — прохладно для купания")
        if prefs.trip_types and not matched:
            tradeoffs.append("Не совсем тот тип отдыха, что вы просили")

        if cl["rain"] >= 10:
            risks.append(f"Около {cl['rain']} дождливых дней в месяц")
        if month not in c["best_months"]:
            risks.append("Не самый лучший сезон")
        if not c["visa_free"]:
            risks.append(f"Виза: {c['visa']}")
        if not c["direct"]:
            risks.append("Нет прямых рейсов — пересадка")
        if kids and h > 7:
            risks.append("Длинный перелёт для ребёнка")
        if t.pets:
            risks.append("Питомцу нужны ветпаспорт, чип и справка (форма 1) — проверьте правила въезда")
        if budget and e["total"] > budget:
            risks.append(f"Может выйти за бюджет на {rub(e['total'] - budget)}")

        options.append({
            "city": c, "estimate": e, "climate": cl, "flight_hours": h,
            "score": round(100 * score / max_score), "why": why, "tradeoffs": tradeoffs, "risks": risks,
        })
    return {"options": options, "funnel": funnel, "relaxed": relaxed}


_NAME_WORDS = ["Azure", "Coral", "Palm", "Lotus", "Sunset", "Amber", "Jade", "Saffron", "Marina",
               "Orchid", "Silk", "Cedar", "Pearl", "Emerald", "Breeze", "Horizon", "Lumen", "Terra"]
_BEACH_TPL = [("{} Family Resort & Spa", 5), ("{} Beach Resort", 5), ("{} Bay Hotel", 4), ("{} Garden Hotel", 3),
              ("{} Lagoon Villas", 5), ("{} Sea View", 4), ("{} Budget Inn", 2)]
_CITY_TPL = [("{} Grand Hotel", 5), ("{} Boutique Hotel", 4), ("{} Old Town Residence", 4), ("{} Design Hotel", 4),
             ("{} City Inn", 3), ("{} Apartments", 3), ("{} Hostel & Rooms", 2)]
_STAR_K = {2: 0.45, 3: 0.7, 4: 1.0, 5: 1.6}
_MEAL_K = {"RO": 0.85, "BB": 1.0, "HB": 1.2, "AI": 1.5}
_AMENITIES = ["бассейн", "Wi-Fi", "спа", "фитнес", "парковка", "кухня", "детская площадка", "ресторан", "бар на крыше"]


def _hotel_base(city_id: str) -> List[dict]:
    c = CITIES[city_id]
    rng = random.Random(f"hotels-{city_id}")
    beach = "beach" in c["types"]
    words = rng.sample(_NAME_WORDS, 7)
    hotels = []
    for i, (tpl, stars) in enumerate(_BEACH_TPL if beach else _CITY_TPL):
        if city_id == "maldives":
            stars = max(stars, 4)
        if city_id == "antalya":
            meal = "AI" if stars >= 4 else rng.choice(["HB", "BB"])
        elif beach:
            meal = rng.choice(["BB", "BB", "HB", "AI"] if stars >= 4 else ["RO", "BB"])
        else:
            meal = rng.choice(["BB", "BB", "RO"])
        free_cancel = rng.random() < 0.6
        rating = round(min(9.7, 6.6 + stars * 0.55 + rng.uniform(-0.6, 0.6)), 1)
        hotels.append({
            "id": f"{city_id}-h{i}",
            "city_id": city_id,
            "name": tpl.format(words[i]),
            "stars": stars,
            "rating": rating,
            "reviews": rng.randint(80, 2400),
            "meal": meal,
            "free_cancel": free_cancel,
            "cancel_days": rng.choice([1, 3, 7]) if free_cancel else 0,
            "beach_m": (rng.choice([50, 120, 250, 400, 700, 1200]) if beach else None),
            "center_km": round(rng.uniform(0.3, 2.5) if not beach else rng.uniform(1, 12), 1),
            "airport_km": max(3, int(c["airport_km"] * rng.uniform(0.7, 1.3))),
            "kids_club": beach and stars >= 4 and rng.random() < 0.75,
            "pets_allowed": i in (2, 5) or rng.random() < 0.15,
            "check_in": rng.choice(["14:00", "14:00", "15:00"]),
            "check_out": "12:00",
            "amenities": rng.sample(_AMENITIES, 4),
            "base_night": c["hotel_night"] * _STAR_K[stars] * _MEAL_K[meal] * (1.0 if free_cancel else 0.88) * rng.uniform(0.9, 1.12),
            "emoji": "🏨" if not beach else ("🏝️" if stars >= 5 else "🏖️"),
        })
    return hotels


def hotel_offer(hotel: dict, check_in: date, nights: int, t: Travellers) -> dict:
    c = CITIES[hotel["city_id"]]
    jitter = random.Random(f"{hotel['id']}-{check_in}").uniform(0.96, 1.05)
    night = hotel["base_night"] * season_k(c, check_in) * room_factor(t) * jitter
    pet_fee = 1200 * nights if t.pets else 0
    total = int(round(night * nights + pet_fee, -2))
    return {**hotel, "check_in_date": check_in, "check_out_date": check_in + timedelta(days=nights),
            "nights": nights, "night_price": int(round(night, -2)), "pet_fee": pet_fee, "total": total,
            "early_checkin_price": int(round(night * 0.5, -2))}


def get_hotel(hotel_id: str, check_in: date, nights: int, t: Travellers) -> Optional[dict]:
    city_id = hotel_id.rsplit("-h", 1)[0]
    if city_id not in CITIES:
        return None
    h = next((h for h in _hotel_base(city_id) if h["id"] == hotel_id), None)
    return hotel_offer(h, check_in, nights, t) if h else None


def search_hotels(city_id: str, check_in: date, nights: int, t: Travellers, prefs: Prefs,
                  priorities: Priorities, exclude: List[str], limit: int = 4, budget_left: Optional[int] = None) -> Dict:
    c = CITIES[city_id]
    offers = [hotel_offer(h, check_in, nights, t) for h in _hotel_base(city_id) if h["id"] not in exclude]
    filters = []
    if budget_left:
        filters.append((f"в бюджете (≤ {rub(budget_left)} на отель)", lambda h: h["total"] <= budget_left))
    if t.pets:
        filters.append(("можно с питомцем", lambda h: h["pets_allowed"]))
    if prefs.meal_min:
        filters.append((f"питание ≥ {MEALS[prefs.meal_min].lower()}", lambda h: MEAL_RANK[h["meal"]] >= MEAL_RANK[prefs.meal_min]))
    if prefs.free_cancel:
        filters.append(("бесплатная отмена", lambda h: h["free_cancel"]))
    if prefs.min_stars:
        filters.append((f"от {prefs.min_stars}★", lambda h: h["stars"] >= prefs.min_stars))
    if prefs.min_rating:
        filters.append((f"рейтинг от {prefs.min_rating}", lambda h: h["rating"] >= prefs.min_rating))
    if prefs.max_beach_m and "beach" in c["types"]:
        filters.append((f"до моря ≤ {prefs.max_beach_m} м", lambda h: (h["beach_m"] or 0) <= prefs.max_beach_m))
    if prefs.kids_club:
        filters.append(("детский клуб", lambda h: h["kids_club"]))
    if prefs.hotel_max_night:
        filters.append((f"до {rub(prefs.hotel_max_night)}/ночь", lambda h: h["night_price"] <= prefs.hotel_max_night))

    funnel = [f"{len(offers)} отелей {c['name_in']}"]
    active, relaxed = list(filters), []
    while True:
        res = [h for h in offers if all(f(h) for _, f in active)]
        if res or not active:
            break
        relaxed.append(active.pop()[0])
    funnel += [n for n, _ in active] + [f"осталось {len(res)}"]
    res = res or offers

    kids = t.children > 0
    prices = [h["total"] for h in res]
    loc = [(h["beach_m"] / 1000 if h["beach_m"] is not None else h["center_km"]) for h in res]
    w = {
        "price": priorities.price, "rating": priorities.rating,
        "meals": priorities.meals + (1 if kids else 0), "flex": priorities.flexibility + (1 if kids else 0),
        "loc": priorities.location, "kids": 2 if kids else 0,
    }
    scored = []
    for h, l in zip(res, loc):
        s = (w["price"] * _norm(prices, h["total"], invert=True)
             + w["rating"] * (h["rating"] - 6) / 4
             + w["meals"] * MEAL_RANK[h["meal"]] / 3
             + w["flex"] * (1 if h["free_cancel"] else 0)
             + w["loc"] * _norm(loc, l, invert=True)
             + w["kids"] * (1 if h["kids_club"] else 0))
        scored.append((s, h))
    scored.sort(key=lambda x: -x[0])
    top = scored[0][0] if scored else 1

    cheapest = min(prices)
    best_rating = max(h["rating"] for h in res)
    options = []
    for s, h in scored[:limit]:
        why, tradeoffs, risks = [], [], []
        if h["total"] == cheapest:
            why.append("Самый выгодный по цене среди подходящих")
        if h["rating"] >= 8.8:
            why.append(f"Высокий рейтинг гостей — {h['rating']}")
        if h["meal"] in ("AI", "HB"):
            why.append(f"Питание: {MEALS[h['meal']].lower()} — меньше трат на месте")
        if h["free_cancel"]:
            why.append(f"Бесплатная отмена за {h['cancel_days']} дн. до заезда")
        if h["beach_m"] is not None and h["beach_m"] <= 150:
            why.append(f"Первая линия — {h['beach_m']} м до моря")
        if h["beach_m"] is None and h["center_km"] <= 1:
            why.append(f"В центре — {h['center_km']} км")
        if kids and h["kids_club"]:
            why.append("Детский клуб и анимация")
        if t.pets and h["pets_allowed"]:
            why.append("Можно с питомцем")
        if not why:
            why.append("Сбалансированный вариант по цене и качеству")

        if h["total"] - cheapest >= 3000:
            tradeoffs.append(f"Дороже самого дешёвого варианта на {rub(h['total'] - cheapest)}")
        if best_rating - h["rating"] >= 0.5:
            tradeoffs.append(f"Рейтинг {h['rating']} — ниже лучшего ({best_rating})")
        if h["meal"] in ("RO", "BB"):
            tradeoffs.append("Обеды и ужины — за свой счёт")
        if h["beach_m"] and h["beach_m"] >= 400:
            tradeoffs.append(f"До моря {h['beach_m']} м — не первая линия")

        if not h["free_cancel"]:
            risks.append("Невозвратный тариф: при отмене деньги не вернут")
        if kids and not h["kids_club"]:
            risks.append("Нет детского клуба")
        if h["check_in"] == "15:00":
            risks.append("Заселение только с 15:00")
        if h["rating"] < 7.8:
            risks.append("Встречаются жалобы в отзывах")

        options.append({"hotel": h, "score": round(100 * s / top), "why": why[:4], "tradeoffs": tradeoffs, "risks": risks})
    return {"options": options, "funnel": funnel, "relaxed": relaxed}


_SLOTS = [("06:40", 1.0), ("10:15", 1.05), ("14:30", 1.08), ("19:50", 1.0), ("23:55", 0.88)]
_HUBS = [("Emirates", "Дубай"), ("Turkish Airlines", "Стамбул"), ("Qatar Airways", "Доху"), ("China Eastern", "Шанхай")]
_DIRECT = ["Аэрофлот", "S7 Airlines", "Уральские авиалинии", "Azur Air", "Победа"]
_LOWCOST = {"Победа"}


def _flight_id(origin: str, city_id: str, d: date, nights: int, slot: int) -> str:
    return f"FL~{origin}~{city_id}~{d.isoformat()}~{nights}~{slot}"


def get_flight(flight_id: str, t: Travellers) -> Optional[dict]:
    try:
        _, origin, city_id, d, nights, slot = flight_id.split("~")
        return _make_flight(origin, city_id, date.fromisoformat(d), int(nights), int(slot), t)
    except (ValueError, KeyError):
        return None


def _make_flight(origin: str, city_id: str, d: date, nights: int, slot: int, t: Travellers) -> dict:
    c = CITIES[city_id]
    o = ORIGINS.get(origin, ORIGINS["moscow"])
    rng = random.Random(f"{origin}-{city_id}-{d}-{nights}-{slot}")
    base_h = flight_hours(c, origin)
    connection = (not c["direct"]) or slot == 2
    short = base_h <= 5
    if connection:
        airline, via = rng.choice(_HUBS)
        dur = base_h + (round(rng.uniform(2.0, 4.5) * 12) / 12 if c["direct"] else 0)
    else:
        airline = rng.choice(_DIRECT if short else [a for a in _DIRECT if a != "Победа"])
        via = None
        dur = base_h
    baggage = airline not in _LOWCOST
    dep_time, slot_k = _SLOTS[slot]
    hh, mm = map(int, dep_time.split(":"))
    dep = datetime(d.year, d.month, d.day, hh, mm)
    tz_diff = c["tz"] - o["tz"]
    arr = dep + timedelta(hours=dur + tz_diff)

    ret_day = arr.date() + timedelta(days=nights)
    r_slot = rng.choice([0, 1, 2, 3])
    rh, rm = map(int, _SLOTS[r_slot][0].split(":"))
    rdur = dur + round(rng.uniform(-0.4, 0.5) * 12) / 12
    rdep = datetime(ret_day.year, ret_day.month, ret_day.day, rh, rm)
    rarr = rdep + timedelta(hours=rdur - tz_diff)

    per_adult = c["flight_one_way"] * 2 * o["price_k"] * season_k(c, d) * slot_k * (0.8 if connection and c["direct"] else 1) \
        * (0.82 if not baggage else 1) * rng.uniform(0.9, 1.15)
    pet_fee = 2 * 6000 if t.pets else 0
    total = int(round(per_adult * pax_flight(t) + pet_fee, -2))
    return {
        "id": _flight_id(origin, city_id, d, nights, slot),
        "airline": airline, "via": via, "baggage": baggage, "slot": slot,
        "out": {"from": o["airport"], "to": c["airport"], "dep": dep.isoformat(), "arr": arr.isoformat(), "duration_h": round(dur, 2)},
        "back": {"from": c["airport"], "to": o["airport"], "dep": rdep.isoformat(), "arr": rarr.isoformat(), "duration_h": round(rdur, 2)},
        "per_adult": int(round(per_adult, -2)), "pet_fee": pet_fee, "total": total,
    }


def _night(dt_iso: str) -> bool:
    h = datetime.fromisoformat(dt_iso).hour
    return h >= 23 or h < 6


def search_flights(origin: str, city_id: str, d: date, nights: int, t: Travellers, prefs: Prefs,
                   priorities: Priorities, exclude: List[str], limit: int = 4) -> Dict:
    flights = [_make_flight(origin, city_id, d, nights, s, t) for s in range(len(_SLOTS))]
    flights = [f for f in flights if f["id"] not in exclude]
    filters = []
    if prefs.direct_only:
        filters.append(("без пересадок", lambda f: f["via"] is None))
    if prefs.daytime_flights:
        filters.append(("без ночных рейсов", lambda f: not any(_night(x) for x in (f["out"]["dep"], f["out"]["arr"], f["back"]["dep"]))))
    if prefs.baggage:
        filters.append(("с багажом", lambda f: f["baggage"]))
    if prefs.flight_max:
        filters.append((f"до {rub(prefs.flight_max)}", lambda f: f["total"] <= prefs.flight_max))
    funnel = [f"{len(flights)} вариантов перелёта"]
    active, relaxed = list(filters), []
    while True:
        res = [f for f in flights if all(fn(f) for _, fn in active)]
        if res or not active:
            break
        relaxed.append(active.pop()[0])
    funnel += [n for n, _ in active] + [f"осталось {len(res)}"]
    res = res or flights

    kids = t.children > 0
    prices = [f["total"] for f in res]
    durs = [f["out"]["duration_h"] + f["back"]["duration_h"] for f in res]
    scored = []
    for f, du in zip(res, durs):
        nights_cnt = sum(_night(x) for x in (f["out"]["dep"], f["out"]["arr"], f["back"]["dep"], f["back"]["arr"]))
        s = (priorities.price * _norm(prices, f["total"], invert=True)
             + (priorities.flight_time + (1.5 if kids else 0)) * _norm(durs, du, invert=True)
             + (1.5 if kids else 0.7) * (1 - nights_cnt / 4)
             + 0.5 * (1 if f["baggage"] else 0))
        scored.append((s, f))
    scored.sort(key=lambda x: -x[0])
    top = scored[0][0] if scored else 1
    cheapest, fastest = min(prices), min(durs)

    options = []
    for s, f in scored[:limit]:
        du = f["out"]["duration_h"] + f["back"]["duration_h"]
        why, tradeoffs, risks = [], [], []
        if f["via"] is None:
            why.append("Прямой рейс")
        if f["total"] == cheapest:
            why.append("Самый дешёвый вариант")
        if du == fastest:
            why.append("Минимальное время в пути")
        if not _night(f["out"]["dep"]) and not _night(f["out"]["arr"]):
            why.append("Удобное время вылета и прилёта")
        if f["baggage"]:
            why.append("Багаж включён")
        if f["total"] - cheapest >= 3000:
            tradeoffs.append(f"Дороже самого дешёвого на {rub(f['total'] - cheapest)}")
        if du - fastest >= 1.5:
            tradeoffs.append(f"В пути на {hours(du - fastest)} дольше (туда-обратно)")
        if not f["baggage"]:
            tradeoffs.append("Без багажа — только ручная кладь")
        if f["via"]:
            risks.append(f"Пересадка через {f['via']} — риск не успеть на стыковку")
        if _night(f["out"]["dep"]) or _night(f["out"]["arr"]):
            risks.append("Ночной вылет или прилёт" + (" — тяжело с ребёнком" if kids else ""))
        if datetime.fromisoformat(f["out"]["arr"]).date() > d:
            risks.append("Прилёт на следующий день — заселение сдвинется")
        options.append({"flight": f, "score": round(100 * s / top), "why": why[:4] or ["Сбалансированный вариант"],
                        "tradeoffs": tradeoffs, "risks": risks})
    return {"options": options, "funnel": funnel, "relaxed": relaxed}


_TRANSFER_TYPES = {
    "shuttle": {"name": "Групповой шаттл", "emoji": "🚌", "k": 0.35, "speed": 30, "extra_min": 30, "max_pax": 50},
    "taxi": {"name": "Такси (стандарт)", "emoji": "🚕", "k": 1.0, "speed": 45, "extra_min": 5, "max_pax": 3},
    "minivan": {"name": "Индивидуальный минивэн", "emoji": "🚐", "k": 1.5, "speed": 45, "extra_min": 5, "max_pax": 7},
    "speedboat": {"name": "Скоростной катер", "emoji": "🚤", "k": 2.0, "speed": 50, "extra_min": 15, "max_pax": 10},
}


def transfer_offer(kind: str, city_id: str, km: int, arrival_iso: str, t: Travellers) -> dict:
    c = CITIES[city_id]
    tt = _TRANSFER_TYPES[kind]
    pax = t.adults + t.children
    night = _night(arrival_iso)
    one_way = (800 + 45 * km) * c["cost_k"] * tt["k"] * (pax if kind == "shuttle" else 1) * (1.3 if night and kind != "shuttle" else 1)
    if kind == "taxi" and pax > 3:
        one_way *= 2
    duration = int(km / tt["speed"] * 60 + tt["extra_min"])
    return {"id": f"TR~{city_id}~{kind}", "kind": kind, "name": tt["name"], "emoji": tt["emoji"], "km": km,
            "duration_min": duration, "night": night, "child_seat": kind == "minivan",
            "price": int(round(one_way * 2, -2))}


def search_transfers(city_id: str, hotel: dict, arrival_iso: str, t: Travellers) -> List[dict]:
    kinds = ["speedboat"] if city_id == "maldives" else ["taxi", "minivan", "shuttle"]
    opts = [transfer_offer(k, city_id, hotel["airport_km"], arrival_iso, t) for k in kinds]
    night = _night(arrival_iso)
    if night:
        opts = [o for o in opts if o["kind"] != "shuttle"] or opts

    def rank(o):
        if t.children or t.pets or t.adults + t.children > 3:
            return 0 if o["kind"] == "minivan" else 1
        return 0 if o["kind"] == "taxi" else 1
    opts.sort(key=rank)
    return opts
