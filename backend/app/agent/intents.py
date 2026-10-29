from typing import Set

from ..catalog import CITIES, rub
from ..models import CartItem
from ..store import Session
from ..validator import build_view, current_hotel
from . import nlu
from .context import Ctx
from .trip_ops import clear, set_transfer, sync_hotel_dates

EXTRAS = {"early_checkin": "Ранний заезд (с 08:00)", "late_checkout": "Поздний выезд (до 18:00)"}


def apply_intents(sess: Session, p: nlu.ParseResult, ctx: Ctx, changed: Set[str]):
    s = sess.state
    total = build_view(s).total
    if "reduce_budget" in p.intents:
        base = s.budget or total
        if base:
            s.budget = int(round(base * 0.85, -3))
            ctx.changes.append(f"Бюджет: {rub(base)} → {rub(s.budget)}")
            changed.add("budget")
        else:
            s.budget_level = "low"
            changed.add("prefs")
        s.priorities.price = 3
    if "increase_budget" in p.intents:
        if s.budget:
            s.budget = int(round(s.budget * 1.25, -3))
            changed.add("budget")
        s.budget_level = "high" if not s.budget else s.budget_level
        s.priorities.rating = 3
        s.priorities.price = 1
        changed.add("prefs")
    if "other_destination" in p.intents and s.destination_id:
        s.excluded["destination"].append(s.destination_id)
        ctx.changes.append(f"Убрал {CITIES[s.destination_id]['name']} из вариантов")
        clear(s, "destination")
    if "other_hotel" in p.intents and s.hotel_id:
        s.excluded["hotel"].append(s.hotel_id)
        clear(s, "hotel")
        ctx.changes.append("Ищу другой отель, перелёт подберу заново под даты")
    if "other_flight" in p.intents and s.flight_id:
        s.excluded["flight"].append(s.flight_id)
        clear(s, "flight")
    if "fit_budget" in p.intents:
        changed.add("budget")
    if "fix_dates" in p.intents:
        sync_hotel_dates(s, ctx)
    if "transfer" in p.intents and s.hotel_id and s.flight_id:
        set_transfer(s, p.transfer_kind, ctx)
    for extra, title in EXTRAS.items():
        if extra not in p.intents:
            continue
        h = current_hotel(s)
        if not h:
            ctx.notes.append("Сначала выберем отель — потом добавлю ранний заезд/поздний выезд.")
            continue
        if any(e.id == extra for e in s.extras):
            continue
        s.extras.append(CartItem(id=extra, kind="extra", title=title, subtitle=h["name"], price=h["early_checkin_price"]))
        ctx.changes.append(f"Добавил: {title.lower()} — {rub(h['early_checkin_price'])}")
