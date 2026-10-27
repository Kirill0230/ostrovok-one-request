"""Лайк и дизлайк карточки.

Лайк фиксирует выбор. Дизлайк исключает вариант и превращает причину в новое условие или вес.
Чтобы добавить причину: впишите правило в таблицу ниже и кнопку в agent/cards.py (*_REASONS).
Правило получает (state, card, city) и возвращает фразу «что учёл» или None.
"""
from typing import Callable, Dict, Optional

from ..catalog import CITIES, MEALS, hours, rub
from ..models import Card, FeedbackRequest, TripState
from ..store import Session
from ..tools import climate, flight_hours
from ..validator import current_flight, current_hotel
from .context import Ctx, today
from .replan import replan
from .trip_ops import apply_parse, clear, sync_hotel_dates

Rule = Callable[[TripState, Optional[Card], Optional[dict]], Optional[str]]


def like(sess: Session, req: FeedbackRequest, ctx: Ctx):
    s = sess.state
    if req.kind == "destination" and req.card_id in CITIES:
        if s.destination_id != req.card_id:
            clear(s, "destination")
            s.destination_id = req.card_id
        ctx.step("update_trip", f"направление = {CITIES[req.card_id]['name']}")
        ctx.head.append(f"Отлично, {CITIES[req.card_id]['name']}! ❤️")
    elif req.kind == "hotel":
        s.hotel_id = req.card_id
        h = current_hotel(s)
        ctx.step("update_trip", f"отель = {h['name'] if h else req.card_id}")
        ctx.head.append(f"Берём {h['name']}." if h else "Отель выбран.")
    elif req.kind == "flight":
        s.flight_id = req.card_id
        f = current_flight(s)
        ctx.step("update_trip", f"перелёт = {f['airline']}")
        sync_hotel_dates(s, ctx)


# ---------------------------------------------------------------- правила дизлайка: направления

def _month(s: TripState) -> int:
    return (s.date_from or today()).month


def _dest_expensive(s, card, c):
    if not c:
        return None
    s.prefs.max_price_level = max(1, c["price_level"] - 1)
    if card and s.budget is None:
        s.budget = int(round(card.price * 0.85, -3))
    s.priorities.price = 3
    return f"ищу дешевле — до {rub(s.budget)}" if s.budget else "ищу дешевле"


def _dest_far(s, card, c):
    if not c:
        return None
    s.prefs.max_flight_hours = max(3.0, flight_hours(c, s.origin) - 1.5)
    return f"перелёт не дольше {hours(s.prefs.max_flight_hours)}"


def _dest_hot(s, card, c):
    if not c:
        return None
    s.prefs.max_temp = climate(c, _month(s))["air"] - 3
    s.prefs.min_temp = None
    return f"не жарче +{s.prefs.max_temp}°"


def _dest_cold(s, card, c):
    if not c:
        return None
    s.prefs.min_temp = climate(c, _month(s))["air"] + 3
    return f"теплее +{s.prefs.min_temp}°"


def _dest_want(kind: str, instead_of: str, label: str) -> Rule:
    def rule(s, card, c):
        s.prefs.trip_types = [t for t in s.prefs.trip_types if t != instead_of] + [kind]
        return label
    return rule


def _dest_visa(s, card, c):
    s.prefs.no_visa = True
    return "только без визы"


# ---------------------------------------------------------------- отели

def _data(card: Optional[Card]) -> dict:
    return card.data if card else {}


def _hotel_expensive(s, card, c):
    d = _data(card)
    if not d.get("night_price"):
        return None
    s.prefs.hotel_max_night = int(d["night_price"] * 0.85)
    s.priorities.price = 3
    return f"до {rub(s.prefs.hotel_max_night)} за ночь"


def _hotel_location(s, card, c):
    d = _data(card)
    s.priorities.location = 3
    if d.get("beach_m"):
        s.prefs.max_beach_m = max(100, d["beach_m"] // 2)
        return f"до моря не дальше {s.prefs.max_beach_m} м"
    return "ближе к центру"


def _hotel_meals(s, card, c):
    d = _data(card)
    if not d.get("meal"):
        return None
    order = ["RO", "BB", "HB", "AI"]
    s.prefs.meal_min = order[min(3, order.index(d["meal"]) + 1)]
    return f"питание от «{MEALS[s.prefs.meal_min].lower()}»"


def _hotel_rating(s, card, c):
    d = _data(card)
    if not d.get("rating"):
        return None
    s.prefs.min_rating = round(d["rating"] + 0.3, 1)
    return f"рейтинг от {s.prefs.min_rating}"


def _hotel_nonref(s, card, c):
    s.prefs.free_cancel = True
    return "только с бесплатной отменой"


def _hotel_stars(s, card, c):
    d = _data(card)
    if not d.get("stars"):
        return None
    s.prefs.min_stars = min(5, d["stars"] + 1)
    return f"от {s.prefs.min_stars}★"


# ---------------------------------------------------------------- перелёты

def _flight_expensive(s, card, c):
    d = _data(card)
    if not d.get("total"):
        return None
    s.prefs.flight_max = int(d["total"] * 0.92)
    s.priorities.price = 3
    return f"перелёт до {rub(s.prefs.flight_max)}"


def _set_pref(field: str, label: str) -> Rule:
    def rule(s, card, c):
        setattr(s.prefs, field, True)
        return label
    return rule


def _flight_long(s, card, c):
    s.priorities.flight_time = 3
    return "минимум времени в пути"


DISLIKE_RULES: Dict[str, Dict[str, Rule]] = {
    "destination": {
        "expensive": _dest_expensive, "far": _dest_far, "hot": _dest_hot, "cold": _dest_cold,
        "want_city": _dest_want("city", "beach", "город вместо пляжа"),
        "want_beach": _dest_want("beach", "city", "море вместо города"),
        "visa": _dest_visa,
    },
    "hotel": {
        "expensive": _hotel_expensive, "location": _hotel_location, "meals": _hotel_meals,
        "rating": _hotel_rating, "nonref": _hotel_nonref, "stars": _hotel_stars,
    },
    "flight": {
        "expensive": _flight_expensive, "time": _set_pref("daytime_flights", "без ночных вылетов и прилётов"),
        "connection": _set_pref("direct_only", "только прямые"), "baggage": _set_pref("baggage", "с багажом"),
        "long": _flight_long,
    },
}


def dislike(sess: Session, req: FeedbackRequest, ctx: Ctx, understand):
    """understand — хук агента для разбора комментария своими словами (правила или LLM)."""
    s = sess.state
    card = next((c for c in sess.deck if c.id == req.card_id), None)
    s.excluded[req.kind].append(req.card_id)
    rule = DISLIKE_RULES[req.kind].get(req.reason or "")
    learned = rule(s, card, CITIES.get(req.card_id)) if rule else None
    if req.kind == "destination":
        s.prefs.candidates = [x for x in s.prefs.candidates if x != req.card_id]

    if req.comment:
        p = understand(req.comment, s)
        p.destination = None if req.kind != "destination" else p.destination
        changed = apply_parse(sess, p, ctx)
        if changed:
            replan(sess, changed, ctx)
        if p.understood:
            learned = (learned + "; " if learned else "") + ", ".join(p.understood)
    ctx.step("update_trip", f"дизлайк {req.kind}: {req.reason or 'без причины'}" + (f" → {learned}" if learned else ""))
    ctx.head.append(f"Понял, учёл: {learned}." if learned else "Понял, убрал этот вариант.")
    sess.metrics.replans += 1
