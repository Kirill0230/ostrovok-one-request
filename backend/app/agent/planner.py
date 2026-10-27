"""Следующий шаг сценария: чего не хватает в плане — то и показываем.

Этапы идут по порядку: направление → отель → перелёт → корзина. Каждый этап — функция
в STAGES: (условие «этап ещё не пройден», функция-показ). Чтобы добавить этап (поезд, экскурсии),
допишите функцию и вставьте её в STAGES перед корзиной.
"""
import time
from datetime import timedelta
from typing import Callable, List, NamedTuple, Optional

from ..catalog import CITIES, ORIGINS, plural, range_label, rub
from ..models import AgentReply, Card, TripState
from ..store import Session
from ..tools import estimate_trip, search_flights, search_hotels, suggest_destinations
from ..validator import build_view, current_flight, current_hotel
from .cards import destination_card, flight_card, hotel_card
from .context import Ctx, today
from .trip_ops import set_transfer, sync_hotel_dates

ASSUMPTION_ORDER = ["origin", "travellers", "nights", "dates"]

DEFAULT_QUICK = {
    "destination": ["Хочу подешевле", "Поближе, без долгих перелётов", "Хочу город, а не пляж"],
    "hotel": ["Только всё включено", "С бесплатной отменой", "Поближе к морю"],
    "flight": ["Только прямые", "Без ночных рейсов", "Почему этот рейс?"],
}


class StageResult(NamedTuple):
    cards: List[Card]
    kind: Optional[str]
    main: str
    ui_action: Optional[str] = None


def _destinations(sess: Session, ctx: Ctx, auto_transfer: bool) -> StageResult:
    s = sess.state
    res = suggest_destinations(s.origin, s.date_from, s.nights, s.travellers, s.prefs, s.priorities, s.budget,
                               s.budget_level, s.excluded["destination"])
    ctx.step("suggest_destinations", " → ".join(res["funnel"]))
    ctx.step("get_climate", f"климат на {s.date_from.month:02d} месяц для {len(res['options'])} направлений")
    cards = [destination_card(o, s) for o in res["options"]]
    s.stage = "destination"
    if res["relaxed"]:
        ctx.notes.append("Не все варианты проходят по всем условиям — где пришлось уступить: "
                         + ", ".join(res["relaxed"]) + ". Это видно на карточках.")
    if cards:
        main = (f"Вот {len(cards)} {plural(len(cards), 'направление', 'направления', 'направлений')} под ваш запрос. "
                "Смотрите видео и свайпайте: ❤️ — едем, ✖️ — скажите, что не так.")
    else:
        main = "Все направления из каталога вы уже отклонили. Напишите «заново» или ослабьте условия."
        ctx.quick.append("Не знаю, удиви меня")
    return StageResult(cards, "destination", main)


def _hotels(sess: Session, ctx: Ctx, auto_transfer: bool) -> StageResult:
    s = sess.state
    c = CITIES[s.destination_id]
    budget_left = None
    if s.budget:
        # сколько остаётся на отель после перелёта и трансфера
        est = estimate_trip(c, s.origin, s.date_from, s.nights, s.travellers)
        f = current_flight(s)
        budget_left = s.budget - (f["total"] if f else est["flights"]) - est["transfer"]
        budget_left = max(budget_left, 0) or None
    res = search_hotels(s.destination_id, s.hotel_check_in or s.date_from, s.nights, s.travellers, s.prefs,
                        s.priorities, s.excluded["hotel"], budget_left=budget_left)
    ctx.step("search_hotels", " → ".join(res["funnel"]))
    s.stage = "hotel"
    if res["relaxed"]:
        ctx.notes.append("Строго под все условия отелей нет — ослабил: " + ", ".join(res["relaxed"]) + ".")
    crit = "цене, питанию, отмене, расположению и рейтингу" + (", с учётом ребёнка" if s.travellers.children else "") \
           + (" и только где можно с питомцем" if s.travellers.pets else "")
    main = f"{c['flag']} {c['name']}: подобрал отели на {s.nights} ноч. Сравнивал по {crit}. Лучший — первым."
    return StageResult([hotel_card(o, s) for o in res["options"]], "hotel", main)


def _flights(sess: Session, ctx: Ctx, auto_transfer: bool) -> StageResult:
    s = sess.state
    res = search_flights(s.origin, s.destination_id, s.date_from, s.nights, s.travellers, s.prefs, s.priorities,
                         s.excluded["flight"])
    ctx.step("search_flights", " → ".join(res["funnel"]))
    s.stage = "flight"
    if res["relaxed"]:
        ctx.notes.append("Под все условия рейсов нет — ослабил: " + ", ".join(res["relaxed"]) + ".")
    main = f"Перелёты {ORIGINS[s.origin]['from']} туда-обратно. Учитывал цену, время в пути, ночные рейсы и багаж."
    return StageResult([flight_card(o, s) for o in res["options"]], "flight", main)


def _review(sess: Session, ctx: Ctx, auto_transfer: bool) -> StageResult:
    s = sess.state
    sync_hotel_dates(s, ctx)
    if not s.transfer_id and auto_transfer:
        set_transfer(s, None, ctx)
    view = build_view(s)
    errors = [i for i in view.issues if i.severity == "error"]
    warns = [i for i in view.issues if i.severity == "warn"]
    ctx.step("validate_trip", f"проверил {len(view.timeline)} точек маршрута: "
                              f"{len(errors)} ошибок, {len(warns)} предупреждений")
    if errors:
        sess.metrics.connection_errors_seen += len(errors)
    s.stage = "review"
    c = CITIES[s.destination_id]
    budget_line = ""
    if s.budget:
        budget_line = (f" — в бюджете {rub(s.budget)} ✅" if view.total <= s.budget
                       else f" — выше бюджета на {rub(view.total - s.budget)} ⚠️")
    main = (f"Поездка собрана: {c['flag']} {c['name']}, {view.date_label}\n"
            f"Итого {rub(view.total)}{budget_line}.\n")
    if errors or warns:
        main += "\nПроверка стыковок:\n" + "\n".join(f"{'❌' if i.severity == 'error' else '⚠️'} {i.message}" for i in errors + warns)
        ctx.quick += [i.fix for i in errors + warns if i.fix]
    else:
        main += "Проверил стыковки: прилёт → трансфер → заселение → выезд → вылет — всё сходится ✅"
    ctx.quick += ["Оформить", "А если на 2 дня позже?", "Бюджет меньше"]
    return StageResult([], None, main, "open_cart")


# этап показывается, если условие истинно; последний (корзина) — когда всё выбрано
STAGES: List[tuple] = [
    (lambda s: not s.destination_id, _destinations),
    (lambda s: not s.hotel_id, _hotels),
    (lambda s: not s.flight_id, _flights),
    (lambda s: True, _review),
]


def _ensure_dates(s: TripState):
    if not s.date_from:
        d = today() + timedelta(days=14)
        d += timedelta(days=(5 - d.weekday()) % 7)  # ближайшая суббота через 2 недели
        s.date_from = d
        s.assumptions["dates"] = f"даты {range_label(d, d + timedelta(days=s.nights))}"
    if not s.destination_id and s.prefs.candidates and len(s.prefs.candidates) == 1:
        s.destination_id = s.prefs.candidates[0]


def next_step(sess: Session, ctx: Ctx, phrase: Callable[[str, TripState], str], auto_transfer: bool = True) -> AgentReply:
    s = sess.state
    _ensure_dates(s)
    show = next(fn for cond, fn in STAGES if cond(s))
    res = show(sess, ctx, auto_transfer)

    if res.cards and sess.metrics.first_reco_at is None:
        sess.metrics.first_reco_at = time.time()
    sess.deck = res.cards

    parts = list(ctx.head)
    if ctx.changes:
        parts.append("Что поменял в плане:\n" + "\n".join(f"• {x}" for x in ctx.changes))
    parts += ctx.notes
    if res.kind == "destination" and "assumptions" not in s.asked and s.assumptions:
        # порядок фиксированный: JSONB в Postgres не сохраняет порядок ключей
        keys = sorted(s.assumptions, key=lambda k: ASSUMPTION_ORDER.index(k) if k in ASSUMPTION_ORDER else 99)
        parts.append("Считаю, что " + ", ".join(s.assumptions[k] for k in keys) + " — поправьте, если не так.")
        s.asked.append("assumptions")
    parts.append(res.main)
    quick = ctx.quick or (DEFAULT_QUICK.get(res.kind, []) if res.kind else [])
    return AgentReply(text=phrase("\n\n".join(p for p in parts if p), s), cards=res.cards, deck_kind=res.kind,
                      quick_replies=list(dict.fromkeys(quick))[:5], steps=ctx.steps, changes=ctx.changes,
                      ui_action=res.ui_action, state=build_view(s))


def redeck(sess: Session, ctx: Ctx, text: str) -> AgentReply:
    """Не поняли сообщение — повторяем текущую колоду с подсказками."""
    return AgentReply(text=text, cards=sess.deck, deck_kind=sess.deck[0].kind if sess.deck else None,
                      quick_replies=["На 2 дня позже", "Бюджет меньше", "Едем с собакой"], steps=ctx.steps,
                      state=build_view(sess.state))


def explain_selected(s: TripState) -> str:
    """Ответ на «почему?»: объяснение по выбранным отелю и рейсу + текущие веса."""
    lines = []
    h = current_hotel(s)
    if h:
        res = search_hotels(s.destination_id, s.hotel_check_in or s.date_from, s.nights, s.travellers, s.prefs, s.priorities, [], limit=10)
        o = next((o for o in res["options"] if o["hotel"]["id"] == h["id"]), None)
        if o:
            lines.append(f"Отель {h['name']}: почему — {'; '.join(o['why']).lower()}"
                         + (f". Чем пожертвовали — {'; '.join(o['tradeoffs']).lower()}" if o["tradeoffs"] else "")
                         + (f". Риски — {'; '.join(o['risks']).lower()}" if o["risks"] else ""))
    f = current_flight(s)
    if f:
        res = search_flights(s.origin, s.destination_id, s.date_from, s.nights, s.travellers, s.prefs, s.priorities, [], limit=10)
        o = next((o for o in res["options"] if o["flight"]["id"] == f["id"]), None)
        if o:
            lines.append(f"Перелёт {f['airline']}: почему — {'; '.join(o['why']).lower()}"
                         + (f". Риски — {'; '.join(o['risks']).lower()}" if o["risks"] else ""))
    lines.append(f"Сейчас веса такие: цена {s.priorities.price}/3, время в пути {s.priorities.flight_time}/3, "
                 f"гибкость {s.priorities.flexibility}/3, питание {s.priorities.meals}/3 — их можно поменять в панели «Поездка».")
    return "\n".join(lines)
