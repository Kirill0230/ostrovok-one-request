"""Оформление (имитация оплаты) и работа после покупки: доп. услуги, смена дат с доплатой, напоминания."""
from datetime import datetime, timedelta
from typing import Callable, List

from ..catalog import CITIES, day_label, rub
from ..models import AgentReply, AgentStep, Booking, CheckoutRequest, TripState
from ..store import BookingEvent, Session, new_booking_id
from ..validator import build_view, current_flight, current_hotel
from . import nlu
from .context import Ctx
from .intents import apply_intents
from .replan import replan
from .trip_ops import apply_parse

Phrase = Callable[[str, TripState], str]


def _reminders(s: TripState) -> List[str]:
    h, f = current_hotel(s), current_flight(s)
    c = CITIES[s.destination_id]
    out_dep = datetime.fromisoformat(f["out"]["dep"])
    reminders = [
        f"{day_label(out_dep.date() - timedelta(days=7))}: проверить загранпаспорт (срок действия > 6 мес.)",
        f"{day_label(out_dep.date() - timedelta(days=1))}: онлайн-регистрация на рейс {f['airline']}",
        f"{day_label(out_dep.date())}, {(out_dep - timedelta(hours=3)):%H:%M}: выезжать в аэропорт {f['out']['from']}",
        "Водитель трансфера встретит в зоне прилёта с табличкой",
    ]
    if h["free_cancel"]:
        reminders.insert(0, f"Бесплатная отмена отеля до {day_label(h['check_in_date'] - timedelta(days=h['cancel_days']))}")
    if not c["visa_free"]:
        reminders.insert(0, f"Оформить визу: {c['visa']}")
    return reminders


def checkout(sess: Session, req: CheckoutRequest, phrase: Phrase) -> AgentReply:
    s = sess.state
    view = build_view(s)
    if s.stage == "booked" and s.booking:
        # повторный запрос (двойной клик «Оплатить») — второй брони не создаём
        return AgentReply(text=f"Бронь {s.booking.id} уже оформлена — повторно ничего не списываем.",
                          ui_action="open_booking", state=view)
    if not view.can_checkout:
        errors = "; ".join(i.message for i in view.issues if i.severity == "error")
        return AgentReply(text="Не могу оформить: " + (errors or "поездка ещё не собрана."), state=view)
    c = CITIES[s.destination_id]
    s.booking = Booking(
        id=new_booking_id(),
        created_at=datetime.now().isoformat(timespec="seconds"), total=view.total,
        reminders=_reminders(s))
    if req.contact_name or req.contact_email:
        sess.pending_contact = {"name": req.contact_name, "email": req.contact_email}
    s.stage = "booked"
    sess.metrics.booked = True
    sess.deck = []
    sess.profile_dirty = True
    sess.pending_booking_events.append(BookingEvent(
        type="created", amount_delta=view.total, description=f"Оформлено: {c['name']}, {view.date_label}"))
    text = (f"Готово! Бронь {s.booking.id} оформлена (демо, деньги не списаны).\n\n"
            f"{c['flag']} {c['name']}, {build_view(s).date_label}, итого {rub(view.total)}.\n\n"
            "Что могу сделать дальше: ранний заезд, поздний выезд, сдвинуть даты, поменять трансфер, напоминания.")
    return AgentReply(text=phrase(text, s), ui_action="open_booking", state=build_view(s),
                      quick_replies=["Добавь ранний заезд", "А если на день позже?", "Напоминания", "Поменяй трансфер на минивэн"],
                      steps=[AgentStep(tool="checkout", summary=f"бронь {s.booking.id}, {rub(view.total)} (имитация оплаты)")])


def post_booking(sess: Session, p: nlu.ParseResult, ctx: Ctx, phrase: Phrase) -> AgentReply:
    """Изменение уже оплаченной брони: пересобираем план и считаем доплату или возврат."""
    s = sess.state
    b = s.booking
    before = build_view(s).total
    if p.destination and p.destination != s.destination_id:
        return AgentReply(text="Поездка уже оплачена. Смена направления — это отмена брони; в прототипе проще начать "
                               "новую поездку: напишите «заново».", state=build_view(s), quick_replies=["Заново"])
    if "reminders" in p.intents:
        return AgentReply(text="Напоминания по брони " + b.id + ":\n" + "\n".join(f"🔔 {r}" for r in b.reminders),
                          state=build_view(s), ui_action="open_booking")
    changed = apply_parse(sess, p, ctx)
    apply_intents(sess, p, ctx, changed)
    if changed:
        replan(sess, changed, ctx)
    view = build_view(s)
    diff = view.total - before
    if not ctx.changes:
        return AgentReply(text="Бронь " + b.id + " активна. Могу: ранний заезд, поздний выезд, сдвинуть даты, "
                               "поменять трансфер, показать напоминания.", state=view,
                          quick_replies=["Добавь ранний заезд", "На день позже", "Напоминания"])
    b.total = view.total
    b.changes += ctx.changes
    sess.pending_booking_events.append(BookingEvent(type="changed", amount_delta=diff, description="; ".join(ctx.changes)))
    money = (f"Доплата {rub(diff)}." if diff > 0 else f"Вернём {rub(-diff)}." if diff < 0 else "Без доплаты.")
    bad = [i for i in view.issues if i.severity == "error"]
    text = "Изменил оплаченную бронь " + b.id + ":\n" + "\n".join(f"• {x}" for x in ctx.changes) + f"\n\n{money} " \
           + ("Проверил стыковки — всё сходится ✅" if not bad else "⚠️ " + "; ".join(i.message for i in bad))
    return AgentReply(text=phrase(text, s), state=view, steps=ctx.steps, changes=ctx.changes, ui_action="open_booking")
