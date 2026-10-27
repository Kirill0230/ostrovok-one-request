"""Сборка корзины и проверка стыковок — обычным кодом, без LLM.

Проверяет цепочку «вылет → прилёт → паспортный контроль → трансфер → заселение → ... →
выезд → трансфер → вылет обратно», совпадение дат отеля и билетов, бюджет, питомцев и детей.
"""
from datetime import datetime, timedelta
from typing import List, Optional, Tuple

from .catalog import CITIES, ORIGINS, MEALS, day_label, dt_label, hours, range_label, rub, travellers_label
from .models import CartItem, Issue, StateView, TimelineEvent, TripState
from .tools import get_flight, get_hotel, search_transfers, transfer_offer

AIRPORT_ARRIVAL_MIN = 60  # паспортный контроль + багаж
AIRPORT_BEFORE_DEP_MIN = 150  # быть в аэропорту за 2,5 часа до международного вылета


def current_flight(s: TripState) -> Optional[dict]:
    return get_flight(s.flight_id, s.travellers) if s.flight_id else None


def hotel_check_in(s: TripState) -> Optional[datetime]:
    return s.hotel_check_in or s.date_from


def current_hotel(s: TripState) -> Optional[dict]:
    if not s.hotel_id or not s.date_from:
        return None
    return get_hotel(s.hotel_id, hotel_check_in(s), s.nights, s.travellers)


def current_transfer(s: TripState, hotel: Optional[dict], flight: Optional[dict]) -> Optional[dict]:
    if not s.transfer_id or not hotel or not flight:
        return None
    _, city_id, kind = s.transfer_id.split("~")
    return transfer_offer(kind, city_id, hotel["airport_km"], flight["out"]["arr"], s.travellers)


def default_transfer_id(s: TripState, hotel: dict, flight: dict) -> str:
    return search_transfers(s.destination_id, hotel, flight["out"]["arr"], s.travellers)[0]["id"]


def build_cart(s: TripState) -> Tuple[List[CartItem], Optional[dict], Optional[dict], Optional[dict]]:
    hotel, flight = current_hotel(s), current_flight(s)
    transfer = current_transfer(s, hotel, flight)
    items: List[CartItem] = []
    if flight:
        o = flight["out"]
        items.append(CartItem(
            id=flight["id"], kind="flight",
            title=f"Перелёт {o['from']} ⇄ {o['to']} · {flight['airline']}",
            subtitle=("прямой" if not flight["via"] else f"через {flight['via']}") + (" · багаж включён" if flight["baggage"] else " · без багажа"),
            price=flight["total"]))
    if hotel:
        items.append(CartItem(
            id=hotel["id"], kind="hotel", title=f"{hotel['name']} {'★' * hotel['stars']}",
            subtitle=f"{range_label(hotel['check_in_date'], hotel['check_out_date'])} · {hotel['nights']} ноч. · {MEALS[hotel['meal']]}"
                     + (" · бесплатная отмена" if hotel["free_cancel"] else " · невозвратный"),
            price=hotel["total"]))
    if transfer:
        items.append(CartItem(
            id=transfer["id"], kind="transfer", title=f"{transfer['emoji']} {transfer['name']}",
            subtitle=f"аэропорт ⇄ отель · ~{transfer['duration_min']} мин" + (" · ночной тариф" if transfer["night"] else ""),
            price=transfer["price"]))
    items.extend(s.extras)
    return items, hotel, flight, transfer


def validate(s: TripState, hotel: Optional[dict], flight: Optional[dict], transfer: Optional[dict],
             total: int) -> Tuple[List[Issue], List[TimelineEvent]]:
    issues: List[Issue] = []
    tl: List[TimelineEvent] = []
    c = CITIES.get(s.destination_id or "")
    origin = ORIGINS.get(s.origin, ORIGINS["moscow"])

    if s.budget and total > s.budget:
        issues.append(Issue(code="over_budget", severity="warn",
                            message=f"Сумма {rub(total)} больше бюджета {rub(s.budget)} на {rub(total - s.budget)}",
                            fix="Уложи в бюджет"))
    if hotel and s.travellers.pets and not hotel["pets_allowed"]:
        issues.append(Issue(code="pets_not_allowed", severity="error",
                            message="В выбранном отеле нельзя с питомцем", fix="Подбери отель, где можно с собакой"))
    if hotel and s.travellers.children and not hotel["kids_club"] and c and "beach" in c["types"]:
        issues.append(Issue(code="no_kids_club", severity="info", message="В отеле нет детского клуба"))
    if c and not c["visa_free"]:
        issues.append(Issue(code="visa", severity="info", message=f"Виза: {c['visa']}. Оформите заранее."))

    if not flight:
        return issues, tl

    out_dep = datetime.fromisoformat(flight["out"]["dep"])
    out_arr = datetime.fromisoformat(flight["out"]["arr"])
    back_dep = datetime.fromisoformat(flight["back"]["dep"])
    back_arr = datetime.fromisoformat(flight["back"]["arr"])
    tl.append(TimelineEvent(at=out_dep.isoformat(), icon="🛫", label=f"Вылет {origin['from']} ({flight['out']['from']})",
                            detail=f"{dt_label(out_dep)} · {flight['airline']}" + (f" · пересадка через {flight['via']}" if flight["via"] else "")))
    arr_status = "warn" if (out_arr.hour >= 23 or out_arr.hour < 6) else "ok"
    tl.append(TimelineEvent(at=out_arr.isoformat(), icon="🛬", label=f"Прилёт {c['name_to'] if c else ''} ({flight['out']['to']})",
                            detail=f"{dt_label(out_arr)} местного · в пути {hours(flight['out']['duration_h'])}", status=arr_status))
    if arr_status == "warn":
        issues.append(Issue(code="night_arrival", severity="info", message="Ночной прилёт — трансфер по ночному тарифу"))

    if not hotel:
        return issues, tl

    ci_h, ci_m = map(int, hotel["check_in"].split(":"))
    ci_dt = datetime.combine(hotel["check_in_date"], datetime.min.time()).replace(hour=ci_h, minute=ci_m)
    co_dt = datetime.combine(hotel["check_out_date"], datetime.min.time()).replace(hour=12)
    tr_min = transfer["duration_min"] if transfer else 60
    at_hotel = out_arr + timedelta(minutes=AIRPORT_ARRIVAL_MIN + tr_min)

    tl.append(TimelineEvent(at=(out_arr + timedelta(minutes=AIRPORT_ARRIVAL_MIN)).isoformat(), icon=transfer["emoji"] if transfer else "🚕",
                            label=transfer["name"] if transfer else "Трансфер не выбран",
                            detail=f"после паспортного контроля и багажа (~1 ч), в дороге ~{tr_min} мин",
                            status="ok" if transfer else "warn"))
    if not transfer:
        issues.append(Issue(code="no_transfer", severity="warn", message="Не выбран трансфер из аэропорта", fix="Добавь трансфер"))

    has_early = any(e.id == "early_checkin" for e in s.extras)
    if out_arr.date() != hotel["check_in_date"]:
        if out_arr.date() > hotel["check_in_date"]:
            msg = f"Прилёт {day_label(out_arr.date())}, а заселение {day_label(hotel['check_in_date'])} — первая ночь пропадёт"
        else:
            msg = f"Прилёт {day_label(out_arr.date())}, а отель только с {day_label(hotel['check_in_date'])} — негде ночевать"
        issues.append(Issue(code="checkin_date_mismatch", severity="error", message=msg, fix="Почини стыковку дат"))
        hotel_status = "error"
    elif at_hotel < ci_dt and not has_early:
        wait = (ci_dt - at_hotel).total_seconds() / 3600
        hotel_status = "warn" if wait > 2 else "ok"
        if wait > 2:
            issues.append(Issue(code="checkin_wait", severity="warn",
                                message=f"В отеле будете в {at_hotel:%H:%M}, заселение с {hotel['check_in']} — ждать {hours(wait)}",
                                fix="Добавь ранний заезд"))
    else:
        hotel_status = "ok"
    tl.append(TimelineEvent(at=max(at_hotel, ci_dt if not has_early else at_hotel).isoformat(), icon="🏨",
                            label=f"Заселение в {hotel['name']}",
                            detail=f"доезд ~{at_hotel:%H:%M}, заселение с {'раннего заезда' if has_early else hotel['check_in']}",
                            status=hotel_status))

    leave_hotel = back_dep - timedelta(minutes=AIRPORT_BEFORE_DEP_MIN + tr_min)
    tl.append(TimelineEvent(at=min(co_dt, leave_hotel).isoformat(), icon="🧳", label="Выезд из отеля",
                            detail=f"{dt_label(co_dt)} · {hotel['nights']} ноч."))
    if back_dep.date() != hotel["check_out_date"]:
        issues.append(Issue(code="checkout_date_mismatch", severity="error",
                            message=f"Выезд из отеля {day_label(hotel['check_out_date'])}, а обратный рейс {day_label(back_dep.date())}",
                            fix="Почини стыковку дат"))
        back_status = "error"
    elif leave_hotel < co_dt - timedelta(hours=10):
        back_status = "ok"
    elif leave_hotel - co_dt > timedelta(hours=4) and not any(e.id == "late_checkout" for e in s.extras):
        issues.append(Issue(code="checkout_gap", severity="info",
                            message=f"Между выездом (12:00) и дорогой в аэропорт {hours((leave_hotel - co_dt).total_seconds() / 3600)} — можно взять поздний выезд",
                            fix="Добавь поздний выезд"))
        back_status = "ok"
    else:
        back_status = "ok"
    tl.append(TimelineEvent(at=leave_hotel.isoformat(), icon="🚕", label="Трансфер в аэропорт",
                            detail=f"выехать в {leave_hotel:%H:%M}, чтобы быть в аэропорту за 2,5 ч"))
    tl.append(TimelineEvent(at=back_dep.isoformat(), icon="🛫", label=f"Обратный вылет ({flight['back']['from']})",
                            detail=dt_label(back_dep), status=back_status))
    tl.append(TimelineEvent(at=back_arr.isoformat(), icon="🏠", label=f"Дома, {origin['name']}",
                            detail=f"{dt_label(back_arr)} · в пути {hours(flight['back']['duration_h'])}"))
    tl.sort(key=lambda e: e.at)
    return issues, tl


def build_view(s: TripState) -> StateView:
    items, hotel, flight, transfer = build_cart(s)
    total = sum(i.price for i in items)
    issues, tl = validate(s, hotel, flight, transfer, total)
    c = CITIES.get(s.destination_id or "")
    date_label = None
    if s.date_from:
        start = hotel["check_in_date"] if hotel else s.date_from
        date_label = f"{range_label(start, start + timedelta(days=s.nights))} · {s.nights} ноч."
    blocking = any(i.severity == "error" for i in issues)
    return StateView(
        trip=s,
        origin_name=ORIGINS.get(s.origin, ORIGINS["moscow"])["name"],
        destination_name=f"{c['flag']} {c['name']}" if c else None,
        date_label=date_label,
        travellers_label=travellers_label(s.travellers.adults, s.travellers.children, s.travellers.pets),
        cart=items, total=total, issues=issues, timeline=tl,
        can_checkout=bool(hotel and flight and transfer) and not blocking and s.stage != "booked",
    )
