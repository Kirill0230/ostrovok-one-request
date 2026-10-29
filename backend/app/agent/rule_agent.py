from datetime import date
from typing import Optional

from ..catalog import CITIES, ORIGINS, travellers_label
from ..models import ActionRequest, AgentReply, CheckoutRequest, FeedbackRequest, Priorities, Travellers, TripState
from ..store import Session, load_profile
from ..validator import build_view
from . import booking, feedback, nlu
from .context import Ctx, today
from .intents import apply_intents
from .planner import explain_selected, next_step, redeck
from .replan import replan
from .trip_ops import apply_parse, clear

START_QUICK = ["К тёплому морю в ноябре, недорого, с ребёнком", "Город с вкусной едой на выходные",
               "Не знаю куда, удиви меня", "Мальдивы на медовый месяц, бюджет 600 тысяч"]


class RuleAgent:
    name = "rules"

    async def understand(self, text: str, s: TripState) -> nlu.ParseResult:
        return nlu.parse(text, today())

    async def phrase(self, draft: str, s: TripState) -> str:
        return draft

    async def aclose(self):
        pass

    async def _finish(self, reply: AgentReply, sess: Session) -> AgentReply:
        reply.text = await self.phrase(reply.text, sess.state)
        return reply

    async def start(self, sess: Session) -> AgentReply:
        profile = await load_profile(sess.user_id)
        return await self._finish(self._start(sess, profile), sess)

    async def message(self, sess: Session, text: str) -> AgentReply:
        p = await self.understand(text, sess.state)
        if "reset" in p.intents:
            sess.metrics.turns += 1
            sess.state = TripState()
            sess.deck = []
            r = await self.start(sess)
            r.text = "Начинаем заново. " + r.text
            return r
        return await self._finish(self._message(sess, p), sess)

    async def feedback(self, sess: Session, req: FeedbackRequest) -> AgentReply:
        comment = await self.understand(req.comment, sess.state) if req.comment and req.action == "dislike" else None
        return await self._finish(self._feedback(sess, req, comment), sess)

    async def action(self, sess: Session, req: ActionRequest) -> AgentReply:
        if req.type == "reset":
            return await self.message(sess, "заново")
        return await self._finish(self._action(sess, req), sess)

    async def checkout(self, sess: Session, req: CheckoutRequest) -> AgentReply:
        return await self._finish(booking.checkout(sess, req), sess)

    def _start(self, sess: Session, profile: Optional[dict]) -> AgentReply:
        s = sess.state
        s.assumptions = {"origin": "вылет из Москвы", "travellers": "2 взрослых", "nights": "7 ночей"}
        hello = ("Привет! Я соберу поездку по одному сообщению: направление, отель, перелёт и трансфер — "
                 "и проверю, что всё стыкуется.\n\nНапишите, чего хочется, например: "
                 "«к тёплому морю в ноябре, недорого, с ребёнком».")
        if profile:
            s.origin = profile.get("origin", s.origin)
            s.travellers = Travellers(**profile.get("travellers", {}))
            s.priorities = Priorities(**profile.get("priorities", {}))
            s.assumptions["origin"] = ORIGINS[s.origin]["from"].replace("из", "вылет из")
            s.assumptions["travellers"] = travellers_label(s.travellers.adults, s.travellers.children, s.travellers.pets)
            past = [CITIES[t["destination"]]["name"] for t in profile.get("trips", []) if t.get("destination") in CITIES]
            hello = ("С возвращением! Помню ваши настройки: "
                     f"{s.assumptions['origin']}, {s.assumptions['travellers']}"
                     + (f"; в прошлый раз ездили: {', '.join(past[-2:])}" if past else "") + ".\n\nКуда хочется на этот раз?")
        return AgentReply(text=hello, quick_replies=START_QUICK, state=build_view(s))

    def _message(self, sess: Session, p: nlu.ParseResult) -> AgentReply:
        sess.metrics.turns += 1
        s = sess.state
        ctx = Ctx()
        ctx.step("parse_request", ("понял: " + ", ".join(p.understood)) if p.understood else "явных параметров не нашёл")

        nothing = p.empty or (p.intents == ["greeting"])
        if s.stage == "start" and nothing:
            sess.metrics.clarifications += 1
            s.asked.append("open")
            return AgentReply(text="Расскажите в двух словах, какой отдых хочется: море или город, когда и с кем едете? "
                                   "Остальное подберу сам.",
                              quick_replies=["Море в ноябре вдвоём", "Город на выходные", "Не знаю куда, удиви меня"],
                              state=build_view(s), steps=ctx.steps)
        if nothing:
            return redeck(sess, ctx, "Не совсем понял. Можно лайкнуть вариант, написать причину дизлайка "
                                     "или поменять условия: «на 2 дня позже», «бюджет меньше», «едем с собакой».")
        if s.stage == "booked":
            return booking.post_booking(sess, p, ctx)

        first = s.stage == "start"
        if p.understood and first:
            ctx.head.append("Понял: " + ", ".join(p.understood) + ".")
        if p.unknown_place:
            ctx.notes.append(f"«{p.unknown_place}» пока нет в моём каталоге (20 популярных направлений). "
                             "Подобрал похожие по сезону и условиям — если не то, скажите, что важно.")
        changed = apply_parse(sess, p, ctx)
        if first:
            ctx.changes.clear()
        apply_intents(sess, p, ctx, changed)
        if changed:
            replan(sess, changed, ctx)
        if "checkout" in p.intents:
            view = build_view(s)
            if view.can_checkout:
                return AgentReply(text="Проверил стыковки — всё сходится. Открываю оформление.",
                                  ui_action="open_checkout", state=view, steps=ctx.steps)
            ctx.notes.append("Оформить пока не получится: " + ("сначала соберём поездку целиком." if not s.flight_id else
                             "есть ошибки в стыковках — смотрите корзину."))
        if "why" in p.intents and s.hotel_id:
            ctx.notes.append(explain_selected(s))
        return next_step(sess, ctx)

    def _feedback(self, sess: Session, req: FeedbackRequest, comment: Optional[nlu.ParseResult]) -> AgentReply:
        sess.metrics.turns += 1
        ctx = Ctx()
        if sess.state.stage == "booked":
            return AgentReply(text="Поездка уже оформлена. Напишите «заново», чтобы собрать новую.",
                              state=build_view(sess.state))
        if req.action == "like":
            feedback.like(sess, req, ctx)
        else:
            sess.metrics.dislikes += 1
            feedback.dislike(sess, req, ctx, comment)
        return next_step(sess, ctx)

    def _action(self, sess: Session, req: ActionRequest) -> AgentReply:
        s = sess.state
        ctx = Ctx()
        pl = req.payload
        if req.type == "set_priorities":
            for k, v in pl.items():
                if hasattr(s.priorities, k):
                    setattr(s.priorities, k, max(0, min(3, int(v))))
            ctx.step("update_trip", "новые веса: " + ", ".join(f"{k}={v}" for k, v in s.priorities.model_dump().items()))
            ctx.head.append("Учёл ваши приоритеты и пересортировал варианты.")
            return next_step(sess, ctx)
        if req.type == "set_params":
            p = _params_to_parse(pl)
            if s.stage == "booked":
                return booking.post_booking(sess, p, ctx)
            changed = apply_parse(sess, p, ctx)
            if changed:
                replan(sess, changed, ctx)
            return next_step(sess, ctx)
        if req.type == "remove_item":
            kind = pl.get("kind")
            if kind == "hotel":
                clear(s, "hotel")
                ctx.head.append("Убрал отель — подберу другой.")
            elif kind == "flight":
                clear(s, "flight")
                ctx.head.append("Убрал перелёт — вот другие варианты.")
            elif kind == "transfer":
                s.transfer_id = None
                ctx.head.append("Убрал трансфер. Без него придётся добираться самим — учту в проверке.")
            else:
                s.extras = [e for e in s.extras if e.id != pl.get("id")]
                ctx.head.append("Убрал из корзины.")
            return next_step(sess, ctx, auto_transfer=kind != "transfer")
        if req.type == "add_extra":
            p = nlu.ParseResult(intents=[pl.get("id") or ("transfer" if pl.get("transfer") else "")],
                                transfer_kind=pl.get("transfer"))
            if s.stage == "booked":
                return booking.post_booking(sess, p, ctx)
            apply_intents(sess, p, ctx, set())
        return next_step(sess, ctx)


def _params_to_parse(pl: dict) -> nlu.ParseResult:
    p = nlu.ParseResult()
    if pl.get("origin"):
        p.updates["origin"] = pl["origin"]
    if pl.get("date_from"):
        p.updates["date_from"] = date.fromisoformat(pl["date_from"])
    if pl.get("nights"):
        p.updates["nights"] = int(pl["nights"])
    if "budget" in pl:
        p.updates["budget"] = int(pl["budget"]) if pl["budget"] else None
    for k in ("adults", "children"):
        if k in pl:
            p.travellers[k] = int(pl[k])
    if "pets" in pl:
        p.travellers["pets"] = bool(pl["pets"])
    return p
