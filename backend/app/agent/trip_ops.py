"""Операции над TripState: применить разобранный запрос, сбросить зависимое, синхронизировать даты, трансфер.

Каждая функция меняет состояние на месте и пишет в ctx, что сделала.
"""
from datetime import datetime, timedelta
from typing import Optional, Set

from ..catalog import CITIES, MEALS, MEAL_RANK, ORIGINS, day_label, dt_label, range_label, rub, travellers_label
from ..models import Travellers, TripState
from ..store import Session
from ..tools import search_transfers
from ..validator import current_flight, current_hotel
from . import nlu
from .context import Ctx

LEVELS = ["destination", "hotel", "flight", "transfer"]


def clear(s: TripState, level: str):
    """Сбросить уровень плана и всё, что от него зависит (направление → отель → перелёт → трансфер)."""
    for lvl in LEVELS[LEVELS.index(level):]:
        if lvl == "destination":
            s.destination_id = None
        elif lvl == "hotel":
            s.hotel_id = None
            s.hotel_check_in = None
            s.extras = [e for e in s.extras if e.id not in ("early_checkin", "late_checkout")]
        elif lvl == "flight":
            s.flight_id = None
        else:
            s.transfer_id = None


def apply_parse(sess: Session, p: nlu.ParseResult, ctx: Ctx) -> Set[str]:
    """Записать в состояние то, что понял разбор. Возвращает набор изменившихся групп полей."""
    s = sess.state
    changed: Set[str] = set()
    u = p.updates
    before_dates = (s.hotel_check_in or s.date_from, s.nights)
    if "origin" in u and u["origin"] != s.origin:
        ctx.changes.append(f"Город вылета: {ORIGINS[s.origin]['name']} → {ORIGINS[u['origin']]['name']}")
        s.origin = u["origin"]
        changed.add("origin")
    if "origin" in u:
        s.assumptions.pop("origin", None)
    if "date_from" in u and u["date_from"] != s.date_from:
        s.date_from = u["date_from"]
        changed.add("dates")
    if "date_from" in u:
        s.assumptions.pop("dates", None)
    if "nights" in u:
        s.assumptions.pop("nights", None)
        if u["nights"] != s.nights:
            s.nights = u["nights"]
            changed.add("dates")
    if p.shift_days and s.date_from:
        s.date_from += timedelta(days=p.shift_days)
        if s.hotel_check_in:
            s.hotel_check_in += timedelta(days=p.shift_days)
        changed.add("dates")
    if "dates" in changed and before_dates[0]:
        start = s.hotel_check_in or s.date_from
        ctx.changes.append(f"Даты: {range_label(before_dates[0], before_dates[0] + timedelta(days=before_dates[1]))} → "
                           f"{range_label(start, start + timedelta(days=s.nights))}")
    if "budget" in u and u["budget"] != s.budget:
        if s.budget:
            ctx.changes.append(f"Бюджет: {rub(s.budget)} → {rub(u['budget'])}" if u["budget"] else "Бюджет снят")
        s.budget = u["budget"]
        changed.add("budget")
    if "budget_level" in u and u["budget_level"] != s.budget_level:
        s.budget_level = u["budget_level"]
        changed.add("prefs")
    if p.travellers:
        old = travellers_label(s.travellers.adults, s.travellers.children, s.travellers.pets)
        data = s.travellers.model_dump()
        data.update(p.travellers)
        if "children" in p.travellers and "child_ages" not in p.travellers:
            data["child_ages"] = data["child_ages"][: data["children"]]
        new_t = Travellers(**data)
        if new_t != s.travellers:
            s.travellers = new_t
            changed.add("travellers")
            ctx.changes.append(f"Едут: {old} → {travellers_label(new_t.adults, new_t.children, new_t.pets)}")
        s.assumptions.pop("travellers", None)
    for k, v in p.prefs.items():
        if k == "exclude":
            s.excluded["destination"] = list(dict.fromkeys(s.excluded["destination"] + v))
            if s.destination_id in v:
                clear(s, "destination")
                changed.add("destination")
            continue
        if getattr(s.prefs, k) != v:
            setattr(s.prefs, k, v)
            changed.add("prefs")
    for tp in p.add_types:
        if tp not in s.prefs.trip_types:
            s.prefs.trip_types.append(tp)
            changed.add("prefs")
    for k, v in p.priorities.items():
        setattr(s.priorities, k, v)
    if p.candidates:
        s.prefs.candidates = p.candidates
        if s.destination_id and s.destination_id not in p.candidates:
            clear(s, "destination")
        changed.add("prefs")
    if p.destination and p.destination != s.destination_id:
        if s.destination_id:
            ctx.changes.append(f"Направление: {CITIES[s.destination_id]['name']} → {CITIES[p.destination]['name']}")
        clear(s, "destination")
        s.destination_id = p.destination
        s.prefs.candidates = []
        if p.destination in s.excluded["destination"]:
            s.excluded["destination"].remove(p.destination)
        changed.add("destination")
    if changed:
        ctx.step("update_trip", "изменено: " + ", ".join(sorted(changed)))
        if s.stage != "start":
            sess.metrics.replans += 1
    return changed


def set_transfer(s: TripState, kind: Optional[str], ctx: Ctx):
    """Выбрать трансфер нужного типа (или рекомендованный, если kind=None)."""
    h, f = current_hotel(s), current_flight(s)
    if not (h and f):
        return
    opts = search_transfers(s.destination_id, h, f["out"]["arr"], s.travellers)
    chosen = next((o for o in opts if o["kind"] == kind), opts[0])
    if chosen["id"] != s.transfer_id:
        s.transfer_id = chosen["id"]
        ctx.changes.append(f"Трансфер: {chosen['name']} — {rub(chosen['price'])}, ~{chosen['duration_min']} мин")
    ctx.step("search_transfers", ", ".join(f"{o['name']} {rub(o['price'])}" for o in opts))


def sync_hotel_dates(s: TripState, ctx: Ctx):
    """Заселение — в день прилёта: если рейс прилетает на следующий день, сдвигаем отель."""
    f = current_flight(s)
    if not f:
        s.hotel_check_in = None
        return
    arr = datetime.fromisoformat(f["out"]["arr"])
    cur = s.hotel_check_in or s.date_from
    if s.hotel_id and arr.date() != cur:
        ctx.step("validate_trip", f"прилёт {day_label(arr.date())}, а заселение {day_label(cur)} → исправляю")
        ctx.changes.append(f"Рейс прилетает {dt_label(arr)} — сдвинул заселение в отель на {day_label(arr.date())}, "
                           "чтобы не платить за пустую ночь")
    s.hotel_check_in = arr.date()


def hotel_violation(s: TripState, h: dict) -> Optional[str]:
    """Чем выбранный отель не подходит под текущие условия (None — подходит)."""
    p = s.prefs
    if s.travellers.pets and not h["pets_allowed"]:
        return "нельзя с питомцем"
    if p.meal_min and MEAL_RANK[h["meal"]] < MEAL_RANK[p.meal_min]:
        return f"питание {MEALS[h['meal']].lower()}"
    if p.free_cancel and not h["free_cancel"]:
        return "невозвратный тариф"
    if p.min_stars and h["stars"] < p.min_stars:
        return f"только {h['stars']}★"
    if p.kids_club and not h["kids_club"]:
        return "нет детского клуба"
    if p.max_beach_m and h["beach_m"] and h["beach_m"] > p.max_beach_m:
        return f"до моря {h['beach_m']} м"
    return None


def flight_violation(s: TripState, f: dict) -> Optional[str]:
    if s.prefs.direct_only and f["via"]:
        return "с пересадкой"
    if s.prefs.baggage and not f["baggage"]:
        return "без багажа"
    return None


def hotel_loss(old: dict, new: dict) -> str:
    """Чем хуже новый отель по сравнению со старым — для объяснения «чем пожертвовали»."""
    lost = []
    if new["stars"] < old["stars"]:
        lost.append(f"{old['stars']}★ → {new['stars']}★")
    if new["rating"] < old["rating"] - 0.2:
        lost.append(f"рейтинг {old['rating']} → {new['rating']}")
    if MEAL_RANK[new["meal"]] < MEAL_RANK[old["meal"]]:
        lost.append(f"питание {MEALS[old['meal']].lower()} → {MEALS[new['meal']].lower()}")
    if old["free_cancel"] and not new["free_cancel"]:
        lost.append("нет бесплатной отмены")
    if old["kids_club"] and not new["kids_club"]:
        lost.append("нет детского клуба")
    if (new["beach_m"] or 0) > (old["beach_m"] or 0) + 100:
        lost.append(f"до моря {new['beach_m']} м вместо {old['beach_m']} м")
    return ", ".join(lost) or "почти ничем"


def city_in(city_id: str) -> str:
    """«в Анталье» → «В Анталье» для начала предложения."""
    name_in = CITIES[city_id]["name_in"]
    return name_in[:1].upper() + name_in[1:]
