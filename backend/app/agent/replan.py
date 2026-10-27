"""Пересборка плана: после изменения условий пересчитываем только то, что от них зависит."""
from datetime import datetime
from typing import Set

from ..catalog import CITIES, dt_label, hours, rub
from ..models import TripState
from ..store import Session
from ..tools import flight_hours, get_flight, search_flights, search_hotels
from ..validator import build_view, current_flight, current_hotel
from .context import Ctx
from .trip_ops import city_in, flight_violation, hotel_loss, hotel_violation, set_transfer, sync_hotel_dates


def replan(sess: Session, changed: Set[str], ctx: Ctx):
    s = sess.state
    if not s.destination_id:
        return
    if ("dates" in changed or "origin" in changed) and s.flight_id:
        _rebook_flight(s, ctx)
    elif "dates" in changed and not s.flight_id:
        s.hotel_check_in = None

    if changed & {"travellers", "prefs"}:
        _fix_hotel(s, ctx)
        _fix_flight(s, ctx)
        if s.transfer_id and (s.travellers.children or s.travellers.pets) and s.transfer_id.endswith("~taxi"):
            set_transfer(s, "minivan", ctx)
        _warn_destination(s, ctx)

    if changed & {"budget", "dates", "travellers"} and s.budget and (s.hotel_id or s.flight_id):
        if build_view(s).total > s.budget:
            fit_budget(s, ctx)


def _rebook_flight(s: TripState, ctx: Ctx):
    """Новые даты или город вылета — берём тот же «слот» рейса (то же время вылета) на новые даты."""
    slot = int(s.flight_id.split("~")[-1])
    old = get_flight(s.flight_id, s.travellers)
    new_id = f"FL~{s.origin}~{s.destination_id}~{s.date_from.isoformat()}~{s.nights}~{slot}"
    if new_id in s.excluded["flight"]:
        new_id = f"FL~{s.origin}~{s.destination_id}~{s.date_from.isoformat()}~{s.nights}~{(slot + 1) % 5}"
    s.flight_id = new_id
    f = get_flight(new_id, s.travellers)
    ctx.step("search_flights", f"пересобрал перелёт на новые даты тем же рейсом-слотом ({f['airline']}, {f['out']['dep'][11:16]})")
    delta = f["total"] - old["total"] if old else 0
    ctx.changes.append(f"Перелёт: {f['airline']}, вылет {dt_label(datetime.fromisoformat(f['out']['dep']))}"
                       + (f" (+{rub(delta)})" if delta > 0 else f" (−{rub(-delta)})" if delta < 0 else ""))
    sync_hotel_dates(s, ctx)


def _fix_hotel(s: TripState, ctx: Ctx):
    h = current_hotel(s)
    if not h:
        return
    why = hotel_violation(s, h)
    if not why:
        return
    res = search_hotels(s.destination_id, s.hotel_check_in or s.date_from, s.nights, s.travellers, s.prefs,
                        s.priorities, s.excluded["hotel"])
    new = res["options"][0]["hotel"] if res["options"] else None
    if new and not hotel_violation(s, new):
        s.hotel_id = new["id"]
        ctx.step("search_hotels", " → ".join(res["funnel"]))
        ctx.changes.append(f"Отель: {h['name']} ({why}) → {new['name']}, {rub(new['total'])}")
    else:
        ctx.notes.append(f"{city_in(s.destination_id)} не нашёл отель без компромисса ({why}). Можно сменить направление.")
        ctx.quick.append("Подбери другое направление")


def _fix_flight(s: TripState, ctx: Ctx):
    f = current_flight(s)
    if not f:
        return
    why = flight_violation(s, f)
    if not why:
        return
    res = search_flights(s.origin, s.destination_id, s.date_from, s.nights, s.travellers, s.prefs, s.priorities,
                         s.excluded["flight"])
    new = res["options"][0]["flight"]
    if not flight_violation(s, new):
        s.flight_id = new["id"]
        ctx.changes.append(f"Перелёт ({why}) → {new['airline']}, {'прямой' if not new['via'] else 'через ' + new['via']}")
        sync_hotel_dates(s, ctx)


def _warn_destination(s: TripState, ctx: Ctx):
    """Направление больше не подходит под новые жёсткие условия — не меняем молча, а спрашиваем."""
    c = CITIES[s.destination_id]
    fh = flight_hours(c, s.origin)
    warn = None
    if s.prefs.max_flight_hours and fh > s.prefs.max_flight_hours + 0.5:
        warn = f"лететь {hours(fh)}"
    elif s.prefs.no_visa and not c["visa_free"]:
        warn = "нужна виза"
    elif s.travellers.pets and fh > 6:
        warn = f"с питомцем лететь {hours(fh)} тяжело"
    if warn:
        ctx.notes.append(f"Учтите: {c['name']} — {warn}. Оставляем или подобрать другое направление?")
        ctx.quick.append("Подбери другое направление")


def fit_budget(s: TripState, ctx: Ctx):
    """Ищет комбинацию отель × перелёт в том же направлении, которая влезает в бюджет с наименьшей потерей качества."""
    view = build_view(s)
    fixed = sum(i.price for i in view.cart if i.kind in ("transfer", "extra"))
    h_cur, f_cur = current_hotel(s), current_flight(s)
    check_in = s.hotel_check_in or s.date_from
    hotels = search_hotels(s.destination_id, check_in, s.nights, s.travellers, s.prefs, s.priorities,
                           s.excluded["hotel"], limit=10)["options"] if h_cur else [None]
    flights = search_flights(s.origin, s.destination_id, s.date_from, s.nights, s.travellers, s.prefs, s.priorities,
                             s.excluded["flight"], limit=10)["options"] if f_cur else [None]
    best, min_total = None, None
    for ho in hotels:
        for fo in flights:
            tot = fixed + (ho["hotel"]["total"] if ho else 0) + (fo["flight"]["total"] if fo else 0)
            min_total = tot if min_total is None else min(min_total, tot)
            if tot <= s.budget:
                # бонус за сохранение уже выбранного — меняем как можно меньше
                sc = (ho["score"] if ho else 0) + (fo["score"] if fo else 0) \
                    + (15 if ho and h_cur and ho["hotel"]["id"] == h_cur["id"] else 0) \
                    + (15 if fo and f_cur and fo["flight"]["id"] == f_cur["id"] else 0)
                if best is None or sc > best[0]:
                    best = (sc, ho, fo, tot)
    ctx.step("fit_budget", f"перебрал {len(hotels) * len(flights)} комбинаций отель × перелёт под бюджет {rub(s.budget)}")
    if best:
        _, ho, fo, tot = best
        if ho and ho["hotel"]["id"] != h_cur["id"]:
            s.hotel_id = ho["hotel"]["id"]
            lost = hotel_loss(h_cur, ho["hotel"])
            ctx.changes.append(f"Отель: {h_cur['name']} → {ho['hotel']['name']} (−{rub(h_cur['total'] - ho['hotel']['total'])}). "
                               f"Чем пожертвовали: {lost.lower()}")
        if fo and fo["flight"]["id"] != f_cur["id"]:
            s.flight_id = fo["flight"]["id"]
            ctx.changes.append(f"Перелёт: → {fo['flight']['airline']} (−{rub(f_cur['total'] - fo['flight']['total'])})")
            sync_hotel_dates(s, ctx)
        ctx.changes.append(f"Итого {rub(tot)} — укладываемся в {rub(s.budget)}")
    else:
        nights = max(2, int(s.nights * s.budget / max(min_total, 1)))
        ctx.notes.append(f"{city_in(s.destination_id)} в {rub(s.budget)} не уложиться: минимум ~{rub(min_total)}. "
                         f"Можно сократить поездку до {nights} ноч. или выбрать направление дешевле.")
        ctx.quick += [f"Сократи до {nights} ночей", "Подбери другое направление"]
