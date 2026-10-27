"""Агент-заглушка на правилах: полный сквозной сценарий без LLM.

Цикл на каждое событие (сообщение / лайк / дизлайк / действие из UI):
  1. understand()  — разобрать сообщение в ParseResult (здесь регулярки, позже LLM);
  2. apply         — изменить TripState (только изменённые поля);
  3. replan        — пересчитать то, что зависит от изменённых полей, не трогая остальное;
  4. next_step     — решить, что показать: направления → отели → перелёты → корзина;
  5. validate      — проверить стыковки кодом и при необходимости починить.

Хуки для LLM-команды: understand() и phrase() — см. llm_agent.py.
"""
import os
import random
import string
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import List, Optional, Set

from ..catalog import CITIES, MEALS, MEAL_RANK, ORIGINS, day_label, dt_label, hours, plural, range_label, rub, travellers_label
from ..models import (ActionRequest, AgentReply, AgentStep, Booking, CartItem, CheckoutRequest, FeedbackRequest,
                      Prefs, Priorities, Travellers, TripState)
from ..store import Session, load_profile, save_profile
from ..tools import climate, estimate_trip, flight_hours, get_flight, search_flights, search_hotels, search_transfers, suggest_destinations
from ..validator import build_view, current_flight, current_hotel, default_transfer_id
from . import nlu
from .cards import destination_card, flight_card, hotel_card

DEFAULT_PRIORITIES = Priorities()


def today() -> date:
    fake = os.getenv("FAKE_TODAY")
    return date.fromisoformat(fake) if fake else date.today()


@dataclass
class Ctx:
    steps: List[AgentStep] = field(default_factory=list)
    changes: List[str] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)
    quick: List[str] = field(default_factory=list)
    head: List[str] = field(default_factory=list)

    def step(self, tool: str, summary: str):
        self.steps.append(AgentStep(tool=tool, summary=summary))


class RuleAgent:
    name = "rules"

    # ------------------------------------------------------------ хуки для LLM
    def understand(self, text: str, s: TripState) -> nlu.ParseResult:
        return nlu.parse(text, today())

    def phrase(self, draft: str, s: TripState) -> str:
        """Здесь LLM может переписать черновой ответ живым языком (факты не менять!)."""
        return draft

    # ------------------------------------------------------------ публичные события
    def start(self, sess: Session) -> AgentReply:
        s = sess.state
        s.assumptions = {"origin": "вылет из Москвы", "travellers": "2 взрослых", "nights": "7 ночей"}
        profile = load_profile(sess.user_id)
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
        return AgentReply(text=hello, quick_replies=[
            "К тёплому морю в ноябре, недорого, с ребёнком", "Город с вкусной едой на выходные",
            "Не знаю куда, удиви меня", "Мальдивы на медовый месяц, бюджет 600 тысяч"], state=build_view(s))

    def message(self, sess: Session, text: str) -> AgentReply:
        sess.metrics.turns += 1
        s = sess.state
        ctx = Ctx()
        p = self.understand(text, s)
        ctx.step("parse_request", ("понял: " + ", ".join(p.understood)) if p.understood else "явных параметров не нашёл")

        if "reset" in p.intents:
            keep_user = sess.user_id
            sess.state = TripState()
            sess.deck = []
            sess.user_id = keep_user
            r = self.start(sess)
            r.text = "Начинаем заново. " + r.text
            return r

        nothing = p.empty or (p.intents == ["greeting"])
        if s.stage == "start" and nothing:
            sess.metrics.clarifications += 1
            s.asked.append("open")
            return AgentReply(text="Расскажите в двух словах, какой отдых хочется: море или город, когда и с кем едете? "
                                   "Остальное подберу сам.",
                              quick_replies=["Море в ноябре вдвоём", "Город на выходные", "Не знаю куда, удиви меня"],
                              state=build_view(s), steps=ctx.steps)
        if nothing and s.stage != "start":
            return self._redeck(sess, ctx, "Не совсем понял. Можно лайкнуть вариант, написать причину дизлайка "
                                           "или поменять условия: «на 2 дня позже», «бюджет меньше», «едем с собакой».")

        if s.stage == "booked":
            return self._post_booking(sess, p, ctx)

        first = s.stage == "start"
        if p.understood and first:
            ctx.head.append("Понял: " + ", ".join(p.understood) + ".")
        if p.unknown_place:
            ctx.notes.append(f"«{p.unknown_place}» пока нет в моём каталоге (20 популярных направлений). "
                             "Подобрал похожие по сезону и условиям — если не то, скажите, что важно.")
        changed = self._apply(sess, p, ctx)
        if first:
            ctx.changes.clear()  # на первом сообщении менять ещё нечего — это не пересборка
        self._intents(sess, p, ctx, changed)
        if changed:
            self._replan(sess, changed, ctx)
        if "checkout" in p.intents:
            view = build_view(s)
            if view.can_checkout:
                return AgentReply(text=self.phrase("Проверил стыковки — всё сходится. Открываю оформление.", s),
                                  ui_action="open_checkout", state=view, steps=ctx.steps)
            ctx.notes.append("Оформить пока не получится: " + ("сначала соберём поездку целиком." if not s.flight_id else
                             "есть ошибки в стыковках — смотрите корзину."))
        if "why" in p.intents and s.hotel_id:
            ctx.notes.append(self._explain_selected(s))
        return self.next_step(sess, ctx)

    def feedback(self, sess: Session, req: FeedbackRequest) -> AgentReply:
        sess.metrics.turns += 1
        s = sess.state
        ctx = Ctx()
        if s.stage == "booked":
            return AgentReply(text="Поездка уже оформлена. Напишите «заново», чтобы собрать новую.", state=build_view(s))
        if req.action == "like":
            return self._like(sess, req, ctx)
        sess.metrics.dislikes += 1
        return self._dislike(sess, req, ctx)

    def action(self, sess: Session, req: ActionRequest) -> AgentReply:
        s = sess.state
        ctx = Ctx()
        pl = req.payload
        if req.type == "reset":
            return self.message(sess, "заново")
        if req.type == "set_priorities":
            for k, v in pl.items():
                if hasattr(s.priorities, k):
                    setattr(s.priorities, k, max(0, min(3, int(v))))
            ctx.step("update_trip", "новые веса: " + ", ".join(f"{k}={v}" for k, v in s.priorities.model_dump().items()))
            ctx.head.append("Учёл ваши приоритеты и пересортировал варианты.")
            return self.next_step(sess, ctx)
        if req.type == "set_params":
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
            if s.stage == "booked":
                return self._post_booking(sess, p, ctx)
            changed = self._apply(sess, p, ctx)
            if changed:
                self._replan(sess, changed, ctx)
            return self.next_step(sess, ctx)
        if req.type == "remove_item":
            kind = pl.get("kind")
            item_id = pl.get("id")
            if kind == "hotel":
                self._clear(s, "hotel")
                ctx.head.append("Убрал отель — подберу другой.")
            elif kind == "flight":
                self._clear(s, "flight")
                ctx.head.append("Убрал перелёт — вот другие варианты.")
            elif kind == "transfer":
                s.transfer_id = None
                ctx.head.append("Убрал трансфер. Без него придётся добираться самим — учту в проверке.")
            else:
                s.extras = [e for e in s.extras if e.id != item_id]
                ctx.head.append("Убрал из корзины.")
            return self.next_step(sess, ctx, auto_transfer=kind != "transfer")
        if req.type == "add_extra":
            p = nlu.ParseResult(intents=[pl.get("id") or ("transfer" if pl.get("transfer") else "")], transfer_kind=pl.get("transfer"))
            if s.stage == "booked":
                return self._post_booking(sess, p, ctx)
            self._intents(sess, p, ctx, set())
            return self.next_step(sess, ctx)
        return self.next_step(sess, ctx)

    def checkout(self, sess: Session, req: CheckoutRequest) -> AgentReply:
        s = sess.state
        view = build_view(s)
        if not view.can_checkout:
            errors = "; ".join(i.message for i in view.issues if i.severity == "error")
            return AgentReply(text="Не могу оформить: " + (errors or "поездка ещё не собрана."), state=view)
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
        s.booking = Booking(
            id="OST-" + "".join(random.choices(string.ascii_uppercase + string.digits, k=6)),
            created_at=datetime.now().isoformat(timespec="seconds"), total=view.total,
            contact_name=req.contact_name, contact_email=req.contact_email, reminders=reminders)
        s.stage = "booked"
        sess.metrics.booked = True
        sess.deck = []
        save_profile(sess.user_id, s)
        text = (f"Готово! Бронь {s.booking.id} оформлена (демо, деньги не списаны).\n\n"
                f"{c['flag']} {c['name']}, {build_view(s).date_label}, итого {rub(view.total)}.\n\n"
                "Что могу сделать дальше: ранний заезд, поздний выезд, сдвинуть даты, поменять трансфер, напоминания.")
        return AgentReply(text=self.phrase(text, s), ui_action="open_booking", state=build_view(s),
                          quick_replies=["Добавь ранний заезд", "А если на день позже?", "Напоминания", "Поменяй трансфер на минивэн"],
                          steps=[AgentStep(tool="checkout", summary=f"бронь {s.booking.id}, {rub(view.total)} (имитация оплаты)")])

    # ------------------------------------------------------------ применение изменений
    @staticmethod
    def _clear(s: TripState, level: str):
        order = ["destination", "hotel", "flight", "transfer"]
        for lvl in order[order.index(level):]:
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

    def _apply(self, sess: Session, p: nlu.ParseResult, ctx: Ctx) -> Set[str]:
        s = sess.state
        changed: Set[str] = set()
        u = p.updates
        before_dates = (s.hotel_check_in or s.date_from, s.nights)
        if "origin" in u and u["origin"] != s.origin:
            ctx.changes.append(f"Город вылета: {ORIGINS[s.origin]['name']} → {ORIGINS[u['origin']]['name']}")
            s.origin = u["origin"]
            changed.add("origin")
        s.assumptions.pop("origin", None) if "origin" in u else None
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
                    self._clear(s, "destination")
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
                self._clear(s, "destination")
            changed.add("prefs")
        if p.destination and p.destination != s.destination_id:
            if s.destination_id:
                ctx.changes.append(f"Направление: {CITIES[s.destination_id]['name']} → {CITIES[p.destination]['name']}")
            self._clear(s, "destination")
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

    def _intents(self, sess: Session, p: nlu.ParseResult, ctx: Ctx, changed: Set[str]):
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
            self._clear(s, "destination")
        if "other_hotel" in p.intents and s.hotel_id:
            s.excluded["hotel"].append(s.hotel_id)
            self._clear(s, "hotel")
            ctx.changes.append("Ищу другой отель, перелёт подберу заново под даты")
        if "other_flight" in p.intents and s.flight_id:
            s.excluded["flight"].append(s.flight_id)
            self._clear(s, "flight")
        if "fit_budget" in p.intents:
            changed.add("budget")
        if "fix_dates" in p.intents:
            self._sync_hotel_dates(s, ctx)
        if "transfer" in p.intents and s.hotel_id and s.flight_id:
            self._set_transfer(s, p.transfer_kind, ctx)
        for extra in ("early_checkin", "late_checkout"):
            if extra in p.intents:
                h = current_hotel(s)
                if not h:
                    ctx.notes.append("Сначала выберем отель — потом добавлю ранний заезд/поздний выезд.")
                    continue
                if any(e.id == extra for e in s.extras):
                    continue
                title = "Ранний заезд (с 08:00)" if extra == "early_checkin" else "Поздний выезд (до 18:00)"
                s.extras.append(CartItem(id=extra, kind="extra", title=title, subtitle=h["name"], price=h["early_checkin_price"]))
                ctx.changes.append(f"Добавил: {title.lower()} — {rub(h['early_checkin_price'])}")

    def _set_transfer(self, s: TripState, kind: Optional[str], ctx: Ctx):
        h, f = current_hotel(s), current_flight(s)
        if not (h and f):
            return
        opts = search_transfers(s.destination_id, h, f["out"]["arr"], s.travellers)
        chosen = next((o for o in opts if o["kind"] == kind), opts[0])
        if chosen["id"] != s.transfer_id:
            s.transfer_id = chosen["id"]
            ctx.changes.append(f"Трансфер: {chosen['name']} — {rub(chosen['price'])}, ~{chosen['duration_min']} мин")
        ctx.step("search_transfers", ", ".join(f"{o['name']} {rub(o['price'])}" for o in opts))

    def _sync_hotel_dates(self, s: TripState, ctx: Ctx):
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

    def _hotel_violation(self, s: TripState, h: dict) -> Optional[str]:
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

    def _flight_violation(self, s: TripState, f: dict) -> Optional[str]:
        if s.prefs.direct_only and f["via"]:
            return "с пересадкой"
        if s.prefs.baggage and not f["baggage"]:
            return "без багажа"
        return None

    def _replan(self, sess: Session, changed: Set[str], ctx: Ctx):
        s = sess.state
        if not s.destination_id:
            return
        c = CITIES[s.destination_id]
        if ("dates" in changed or "origin" in changed) and s.flight_id:
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
            self._sync_hotel_dates(s, ctx)
        elif "dates" in changed and not s.flight_id:
            s.hotel_check_in = None

        if changed & {"travellers", "prefs"}:
            h = current_hotel(s)
            if h:
                why = self._hotel_violation(s, h)
                if why:
                    res = search_hotels(s.destination_id, s.hotel_check_in or s.date_from, s.nights, s.travellers, s.prefs,
                                        s.priorities, s.excluded["hotel"])
                    new = res["options"][0]["hotel"] if res["options"] else None
                    if new and not self._hotel_violation(s, new):
                        s.hotel_id = new["id"]
                        ctx.step("search_hotels", " → ".join(res["funnel"]))
                        ctx.changes.append(f"Отель: {h['name']} ({why}) → {new['name']}, {rub(new['total'])}")
                    else:
                        ctx.notes.append(f"{c['name_in'][:1].upper() + CITIES[s.destination_id]['name_in'][1:]} не нашёл отель без компромисса ({why}). Можно сменить направление.")
                        ctx.quick.append("Подбери другое направление")
            f = current_flight(s)
            if f:
                why = self._flight_violation(s, f)
                if why:
                    res = search_flights(s.origin, s.destination_id, s.date_from, s.nights, s.travellers, s.prefs, s.priorities,
                                         s.excluded["flight"])
                    new = res["options"][0]["flight"]
                    if not self._flight_violation(s, new):
                        s.flight_id = new["id"]
                        ctx.changes.append(f"Перелёт ({why}) → {new['airline']}, {'прямой' if not new['via'] else 'через ' + new['via']}")
                        self._sync_hotel_dates(s, ctx)
            if s.transfer_id and (s.travellers.children or s.travellers.pets) and s.transfer_id.endswith("~taxi"):
                self._set_transfer(s, "minivan", ctx)
            # направление больше не подходит под новые жёсткие условия
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

        if changed & {"budget", "dates", "travellers"} and s.budget and (s.hotel_id or s.flight_id):
            if build_view(s).total > s.budget:
                self._fit_budget(s, ctx)

    def _fit_budget(self, s: TripState, ctx: Ctx):
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
                lost = self._hotel_loss(h_cur, ho["hotel"])
                ctx.changes.append(f"Отель: {h_cur['name']} → {ho['hotel']['name']} (−{rub(h_cur['total'] - ho['hotel']['total'])}). "
                                   f"Чем пожертвовали: {lost.lower()}")
            if fo and fo["flight"]["id"] != f_cur["id"]:
                s.flight_id = fo["flight"]["id"]
                ctx.changes.append(f"Перелёт: → {fo['flight']['airline']} (−{rub(f_cur['total'] - fo['flight']['total'])})")
                self._sync_hotel_dates(s, ctx)
            ctx.changes.append(f"Итого {rub(tot)} — укладываемся в {rub(s.budget)}")
        else:
            nights = max(2, int(s.nights * s.budget / max(min_total, 1)))
            ctx.notes.append(f"{CITIES[s.destination_id]['name_in'][:1].upper() + CITIES[s.destination_id]['name_in'][1:]} в {rub(s.budget)} не уложиться: минимум ~{rub(min_total)}. "
                             f"Можно сократить поездку до {nights} ноч. или выбрать направление дешевле.")
            ctx.quick += [f"Сократи до {nights} ночей", "Подбери другое направление"]

    @staticmethod
    def _hotel_loss(old: dict, new: dict) -> str:
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

    # ------------------------------------------------------------ лайк / дизлайк
    def _like(self, sess: Session, req: FeedbackRequest, ctx: Ctx) -> AgentReply:
        s = sess.state
        if req.kind == "destination" and req.card_id in CITIES:
            if s.destination_id != req.card_id:
                self._clear(s, "destination")
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
            self._sync_hotel_dates(s, ctx)
        return self.next_step(sess, ctx)

    def _dislike(self, sess: Session, req: FeedbackRequest, ctx: Ctx) -> AgentReply:
        s = sess.state
        card = next((c for c in sess.deck if c.id == req.card_id), None)
        r = req.reason
        learned = None
        if req.kind == "destination":
            s.excluded["destination"].append(req.card_id)
            c = CITIES.get(req.card_id)
            month = (s.date_from or today()).month
            if c and r == "expensive":
                s.prefs.max_price_level = max(1, c["price_level"] - 1)
                if card and s.budget is None:
                    s.budget = int(round(card.price * 0.85, -3))
                s.priorities.price = 3
                learned = f"ищу дешевле — до {rub(s.budget)}" if s.budget else "ищу дешевле"
            elif c and r == "far":
                s.prefs.max_flight_hours = max(3.0, flight_hours(c, s.origin) - 1.5)
                learned = f"перелёт не дольше {hours(s.prefs.max_flight_hours)}"
            elif c and r == "hot":
                s.prefs.max_temp = climate(c, month)["air"] - 3
                s.prefs.min_temp = None
                learned = f"не жарче +{s.prefs.max_temp}°"
            elif c and r == "cold":
                s.prefs.min_temp = climate(c, month)["air"] + 3
                learned = f"теплее +{s.prefs.min_temp}°"
            elif r == "want_city":
                s.prefs.trip_types = [t for t in s.prefs.trip_types if t != "beach"] + ["city"]
                learned = "город вместо пляжа"
            elif r == "want_beach":
                s.prefs.trip_types = [t for t in s.prefs.trip_types if t != "city"] + ["beach"]
                learned = "море вместо города"
            elif r == "visa":
                s.prefs.no_visa = True
                learned = "только без визы"
            else:
                learned = None
            s.prefs.candidates = [x for x in s.prefs.candidates if x != req.card_id]
        elif req.kind == "hotel":
            s.excluded["hotel"].append(req.card_id)
            d = card.data if card else {}
            if r == "expensive" and d.get("night_price"):
                s.prefs.hotel_max_night = int(d["night_price"] * 0.85)
                s.priorities.price = 3
                learned = f"до {rub(s.prefs.hotel_max_night)} за ночь"
            elif r == "location":
                if d.get("beach_m"):
                    s.prefs.max_beach_m = max(100, d["beach_m"] // 2)
                    learned = f"до моря не дальше {s.prefs.max_beach_m} м"
                else:
                    learned = "ближе к центру"
                s.priorities.location = 3
            elif r == "meals" and d.get("meal"):
                order = ["RO", "BB", "HB", "AI"]
                s.prefs.meal_min = order[min(3, order.index(d["meal"]) + 1)]
                learned = f"питание от «{MEALS[s.prefs.meal_min].lower()}»"
            elif r == "rating" and d.get("rating"):
                s.prefs.min_rating = round(d["rating"] + 0.3, 1)
                learned = f"рейтинг от {s.prefs.min_rating}"
            elif r == "nonref":
                s.prefs.free_cancel = True
                learned = "только с бесплатной отменой"
            elif r == "stars" and d.get("stars"):
                s.prefs.min_stars = min(5, d["stars"] + 1)
                learned = f"от {s.prefs.min_stars}★"
        elif req.kind == "flight":
            s.excluded["flight"].append(req.card_id)
            d = card.data if card else {}
            if r == "expensive" and d.get("total"):
                s.prefs.flight_max = int(d["total"] * 0.92)
                s.priorities.price = 3
                learned = f"перелёт до {rub(s.prefs.flight_max)}"
            elif r == "time":
                s.prefs.daytime_flights = True
                learned = "без ночных вылетов и прилётов"
            elif r == "connection":
                s.prefs.direct_only = True
                learned = "только прямые"
            elif r == "baggage":
                s.prefs.baggage = True
                learned = "с багажом"
            elif r == "long":
                s.priorities.flight_time = 3
                learned = "минимум времени в пути"

        if req.comment:
            p = self.understand(req.comment, s)
            p.destination = None if req.kind != "destination" else p.destination
            changed = self._apply(sess, p, ctx)
            if changed:
                self._replan(sess, changed, ctx)
            if p.understood:
                learned = (learned + "; " if learned else "") + ", ".join(p.understood)
        ctx.step("update_trip", f"дизлайк {req.kind}: {r or 'без причины'}" + (f" → {learned}" if learned else ""))
        ctx.head.append(f"Понял, учёл: {learned}." if learned else "Понял, убрал этот вариант.")
        sess.metrics.replans += 1
        return self.next_step(sess, ctx)

    # ------------------------------------------------------------ следующий шаг
    def _assumption_line(self, s: TripState) -> Optional[str]:
        if not s.assumptions:
            return None
        return "Считаю, что " + ", ".join(s.assumptions.values()) + " — поправьте, если не так."

    def next_step(self, sess: Session, ctx: Ctx, auto_transfer: bool = True) -> AgentReply:
        s = sess.state
        if not s.date_from:
            d = today() + timedelta(days=14)
            d += timedelta(days=(5 - d.weekday()) % 7)
            s.date_from = d
            s.assumptions["dates"] = f"даты {range_label(d, d + timedelta(days=s.nights))}"
        if not s.destination_id and s.prefs.candidates and len(s.prefs.candidates) == 1:
            s.destination_id = s.prefs.candidates[0]

        cards, kind, main, quick, ui = [], None, "", list(ctx.quick), None
        if not s.destination_id:
            res = suggest_destinations(s.origin, s.date_from, s.nights, s.travellers, s.prefs, s.priorities, s.budget,
                                       s.budget_level, s.excluded["destination"])
            ctx.step("suggest_destinations", " → ".join(res["funnel"]))
            ctx.step("get_climate", f"климат на {s.date_from.month:02d} месяц для {len(res['options'])} направлений")
            cards = [destination_card(o, s) for o in res["options"]]
            kind = "destination"
            s.stage = "destination"
            if res["relaxed"]:
                ctx.notes.append("Не все варианты проходят по всем условиям — где пришлось уступить: "
                                 + ", ".join(res["relaxed"]) + ". Это видно на карточках.")
            if cards:
                main = (f"Вот {len(cards)} {plural(len(cards), 'направление', 'направления', 'направлений')} под ваш запрос. Смотрите видео и свайпайте: ❤️ — едем, ✖️ — скажите, что не так.")
            else:
                main = "Все направления из каталога вы уже отклонили. Напишите «заново» или ослабьте условия."
            quick += ["Не знаю, удиви меня"] if not cards else []
        elif not s.hotel_id:
            c = CITIES[s.destination_id]
            budget_left = None
            if s.budget:
                est = estimate_trip(c, s.origin, s.date_from, s.nights, s.travellers)
                f = current_flight(s)
                budget_left = s.budget - (f["total"] if f else est["flights"]) - est["transfer"]
                budget_left = max(budget_left, 0) or None
            res = search_hotels(s.destination_id, s.hotel_check_in or s.date_from, s.nights, s.travellers, s.prefs,
                                s.priorities, s.excluded["hotel"], budget_left=budget_left)
            ctx.step("search_hotels", " → ".join(res["funnel"]))
            cards = [hotel_card(o, s) for o in res["options"]]
            kind = "hotel"
            s.stage = "hotel"
            if res["relaxed"]:
                ctx.notes.append("Строго под все условия отелей нет — ослабил: " + ", ".join(res["relaxed"]) + ".")
            crit = "цене, питанию, отмене, расположению и рейтингу" + (", с учётом ребёнка" if s.travellers.children else "") \
                   + (" и только где можно с питомцем" if s.travellers.pets else "")
            main = f"{c['flag']} {c['name']}: подобрал отели на {s.nights} ноч. Сравнивал по {crit}. Лучший — первым."
        elif not s.flight_id:
            res = search_flights(s.origin, s.destination_id, s.date_from, s.nights, s.travellers, s.prefs, s.priorities,
                                 s.excluded["flight"])
            ctx.step("search_flights", " → ".join(res["funnel"]))
            cards = [flight_card(o, s) for o in res["options"]]
            kind = "flight"
            s.stage = "flight"
            if res["relaxed"]:
                ctx.notes.append("Под все условия рейсов нет — ослабил: " + ", ".join(res["relaxed"]) + ".")
            main = f"Перелёты {ORIGINS[s.origin]['from']} туда-обратно. Учитывал цену, время в пути, ночные рейсы и багаж."
        else:
            h, f = current_hotel(s), current_flight(s)
            self._sync_hotel_dates(s, ctx)
            if not s.transfer_id and auto_transfer:
                self._set_transfer(s, None, ctx)
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
                quick += [i.fix for i in errors + warns if i.fix]
            else:
                main += "Проверил стыковки: прилёт → трансфер → заселение → выезд → вылет — всё сходится ✅"
            quick += ["Оформить", "А если на 2 дня позже?", "Бюджет меньше"]
            ui = "open_cart"

        if cards and sess.metrics.first_reco_at is None:
            import time
            sess.metrics.first_reco_at = time.time()
        sess.deck = cards

        parts = list(ctx.head)
        if ctx.changes:
            parts.append("Что поменял в плане:\n" + "\n".join(f"• {x}" for x in ctx.changes))
        parts += ctx.notes
        if kind == "destination" and "assumptions" not in s.asked:
            line = self._assumption_line(s)
            if line:
                parts.append(line)
                s.asked.append("assumptions")
        parts.append(main)
        if kind and not quick:
            quick = {"destination": ["Хочу подешевле", "Поближе, без долгих перелётов", "Хочу город, а не пляж"],
                     "hotel": ["Только всё включено", "С бесплатной отменой", "Поближе к морю"],
                     "flight": ["Только прямые", "Без ночных рейсов", "Почему этот рейс?"]}[kind]
        return AgentReply(text=self.phrase("\n\n".join(p for p in parts if p), s), cards=cards, deck_kind=kind,
                          quick_replies=list(dict.fromkeys(quick))[:5], steps=ctx.steps, changes=ctx.changes,
                          ui_action=ui, state=build_view(s))

    def _redeck(self, sess: Session, ctx: Ctx, text: str) -> AgentReply:
        s = sess.state
        return AgentReply(text=text, cards=sess.deck, deck_kind=sess.deck[0].kind if sess.deck else None,
                          quick_replies=["На 2 дня позже", "Бюджет меньше", "Едем с собакой"], steps=ctx.steps,
                          state=build_view(s))

    def _explain_selected(self, s: TripState) -> str:
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

    # ------------------------------------------------------------ после покупки
    def _post_booking(self, sess: Session, p: nlu.ParseResult, ctx: Ctx) -> AgentReply:
        s = sess.state
        b = s.booking
        before = build_view(s).total
        if p.destination and p.destination != s.destination_id:
            return AgentReply(text="Поездка уже оплачена. Смена направления — это отмена брони; в прототипе проще начать "
                                   "новую поездку: напишите «заново».", state=build_view(s), quick_replies=["Заново"])
        if "reminders" in p.intents:
            return AgentReply(text="Напоминания по брони " + b.id + ":\n" + "\n".join(f"🔔 {r}" for r in b.reminders),
                              state=build_view(s), ui_action="open_booking")
        changed = self._apply(sess, p, ctx)
        self._intents(sess, p, ctx, changed)
        if changed:
            self._replan(sess, changed, ctx)
        view = build_view(s)
        diff = view.total - before
        if not ctx.changes:
            return AgentReply(text="Бронь " + b.id + " активна. Могу: ранний заезд, поздний выезд, сдвинуть даты, "
                                   "поменять трансфер, показать напоминания.", state=view,
                              quick_replies=["Добавь ранний заезд", "На день позже", "Напоминания"])
        b.total = view.total
        b.changes += ctx.changes
        money = (f"Доплата {rub(diff)}." if diff > 0 else f"Вернём {rub(-diff)}." if diff < 0 else "Без доплаты.")
        bad = [i for i in view.issues if i.severity == "error"]
        text = "Изменил оплаченную бронь " + b.id + ":\n" + "\n".join(f"• {x}" for x in ctx.changes) + f"\n\n{money} " \
               + ("Проверил стыковки — всё сходится ✅" if not bad else "⚠️ " + "; ".join(i.message for i in bad))
        return AgentReply(text=self.phrase(text, s), state=view, steps=ctx.steps, changes=ctx.changes, ui_action="open_booking")
