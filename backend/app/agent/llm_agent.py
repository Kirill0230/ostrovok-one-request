import asyncio
import json
import logging
import os
import re
import time
from datetime import date, timedelta
from typing import Any, Dict, Optional

from ..catalog import CITIES, MONTHS_GEN, ORIGINS, TYPE_LABELS, rub, travellers_label
from ..models import TripState
from . import nlu
from .context import today
from .rule_agent import RuleAgent

log = logging.getLogger(__name__)

TIMEOUT_S = float(os.getenv("GIGACHAT_TIMEOUT_S", "8"))
CONCURRENCY = int(os.getenv("GIGACHAT_CONCURRENCY", "1"))
PHRASE = os.getenv("GIGACHAT_PHRASE", "0").lower() in ("1", "true", "yes", "on")
BREAKER_FAILS = 3
BREAKER_PAUSE_S = 60

INTENTS = {
    "checkout": "хочет оформить/оплатить/забронировать",
    "early_checkin": "нужен ранний заезд", "late_checkout": "нужен поздний выезд",
    "reminders": "просит напоминания", "fix_dates": "просит починить стыковки дат",
    "fit_budget": "просит уложиться в бюджет", "transfer": "нужен трансфер/такси",
    "why": "спрашивает, почему выбран вариант", "surprise": "не знает куда, просит удивить",
    "greeting": "просто приветствие без запроса", "other_destination": "хочет другое направление",
    "other_hotel": "хочет другой отель", "other_flight": "хочет другой рейс",
    "reduce_budget": "хочет дешевле", "increase_budget": "готов потратить больше",
    "reset": "хочет начать поиск заново",
}
PREFS_BOOL = ("direct_only", "no_visa", "free_cancel", "kids_club", "baggage", "daytime_flights")
PRIORITY_KEYS = ("price", "flight_time", "flexibility", "meals", "location", "rating")


def _system_prompt(today_: date) -> str:
    catalog = "; ".join(f"{cid} — {c['name']} ({c['country']})" for cid, c in CITIES.items())
    origins = "; ".join(f"{oid} — {o['name']}" for oid, o in ORIGINS.items())
    intents = "; ".join(f"{k} — {v}" for k, v in INTENTS.items())
    return f"""Ты — модуль понимания запросов турагента. Сегодня {today_.isoformat()}.
Разбери сообщение пользователя и верни ТОЛЬКО JSON-объект, без пояснений и markdown.
Включай только то, что пользователь сказал явно или однозначно подразумевал. Не выдумывай.

Направления каталога (id — название): {catalog}.
Города вылета (id — название): {origins}.
Типы отдыха: beach, city, food, party, relax, nature, shopping.
Намерения: {intents}.

Формат (все поля необязательные, опускай пустые):
{{"origin": "id города вылета",
 "date_from": "YYYY-MM-DD — дата начала поездки, всегда в будущем; если назван только месяц — 5-е число",
 "nights": число ночей 1..30,
 "budget": общий бюджет в рублях целым числом,
 "budget_level": "low" | "high",
 "adults": число, "children": число, "child_ages": [возрасты], "pets": true/false,
 "trip_types": ["beach", ...],
 "destination": "id из каталога — только если назван ровно один город/курорт каталога",
 "candidates": ["id", ...] — если названа страна или несколько мест каталога,
 "exclude": ["id", ...] — куда пользователь НЕ хочет,
 "unknown_place": "место, которого нет в каталоге, как назвал пользователь",
 "min_temp": °C, "max_temp": °C, "max_flight_hours": часы,
 "meal_min": "BB" | "HB" | "AI", "max_beach_m": метры до пляжа, "min_stars": 1..5,
 "direct_only", "no_visa", "free_cancel", "kids_club", "baggage", "daytime_flights": true,
 "priorities": {{"price"|"flight_time"|"flexibility"|"meals"|"location"|"rating": 0..3}},
 "intents": ["id намерения", ...],
 "shift_days": сдвиг уже выбранных дат в днях (позже +, раньше −),
 "transfer_kind": "taxi" | "minivan" | "shuttle"}}

Пример: «хотим с мужем и сыном 5 лет погреться на море в декабре, тыщ 250, без пересадок» →
{{"adults": 2, "children": 1, "child_ages": [5], "trip_types": ["beach"], "min_temp": 25,
 "date_from": "{_month_start(12, today_).isoformat()}", "budget": 250000, "direct_only": true}}"""


def _month_start(month: int, today_: date) -> date:
    year = today_.year if month > today_.month else today_.year + 1
    return date(year, month, 5)


def _state_hint(s: TripState) -> str:
    parts = []
    if s.destination_id:
        parts.append(f"выбрано направление {CITIES[s.destination_id]['name']}")
    if s.date_from:
        parts.append(f"даты с {s.date_from.isoformat()} на {s.nights} ноч.")
    if s.budget:
        parts.append(f"бюджет {s.budget} ₽")
    parts.append(f"едут: {travellers_label(s.travellers.adults, s.travellers.children, s.travellers.pets)}")
    return "Текущий план поездки: " + "; ".join(parts) + "."


def _extract_json(text: str) -> Dict[str, Any]:
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("в ответе нет JSON")
    data = json.loads(text[start:end + 1])
    if not isinstance(data, dict):
        raise ValueError("JSON — не объект")
    return data


def _int(v: Any, lo: int, hi: int) -> Optional[int]:
    try:
        n = int(round(float(v)))
    except (TypeError, ValueError):
        return None
    return n if lo <= n <= hi else None


def _ids(v: Any, allowed) -> list:
    return [x for x in dict.fromkeys(v) if isinstance(x, str) and x in allowed] if isinstance(v, list) else []


def to_parse_result(d: Dict[str, Any], today_: date) -> nlu.ParseResult:
    r = nlu.ParseResult()
    if d.get("origin") in ORIGINS:
        r.updates["origin"] = d["origin"]
    if isinstance(d.get("date_from"), str):
        try:
            df = date.fromisoformat(d["date_from"][:10])
            if today_ <= df <= today_ + timedelta(days=400):
                r.updates["date_from"] = df
        except ValueError:
            pass
    if (n := _int(d.get("nights"), 1, 30)) is not None:
        r.updates["nights"] = n
    if (b := _int(d.get("budget"), 10_000, 50_000_000)) is not None:
        r.updates["budget"] = b
    if d.get("budget_level") in ("low", "high"):
        r.updates["budget_level"] = d["budget_level"]

    if (a := _int(d.get("adults"), 1, 9)) is not None:
        r.travellers["adults"] = a
    if (c := _int(d.get("children"), 0, 6)) is not None:
        r.travellers["children"] = c
        ages = [x for x in (_int(a, 0, 17) for a in d.get("child_ages") or [] if not isinstance(a, bool)) if x is not None]
        if ages and c:
            r.travellers["child_ages"] = ages[:c]
    if isinstance(d.get("pets"), bool):
        r.travellers["pets"] = d["pets"]

    r.add_types = _ids(d.get("trip_types"), TYPE_LABELS)
    if d.get("destination") in CITIES:
        r.destination = d["destination"]
    else:
        r.candidates = _ids(d.get("candidates"), CITIES)
        if len(r.candidates) == 1:
            r.destination, r.candidates = r.candidates[0], []
    if isinstance(d.get("unknown_place"), str) and not r.destination and not r.candidates:
        r.unknown_place = d["unknown_place"].strip()[:40] or None

    p = r.prefs
    if exclude := _ids(d.get("exclude"), CITIES):
        p["exclude"] = exclude
    if (t := _int(d.get("min_temp"), 0, 40)) is not None:
        p["min_temp"] = t
    if (t := _int(d.get("max_temp"), 10, 45)) is not None:
        p["max_temp"] = t
    if (h := _int(d.get("max_flight_hours"), 1, 24)) is not None:
        p["max_flight_hours"] = float(h)
    if d.get("meal_min") in ("BB", "HB", "AI"):
        p["meal_min"] = d["meal_min"]
    if (m := _int(d.get("max_beach_m"), 0, 5000)) is not None:
        p["max_beach_m"] = m
    if (st := _int(d.get("min_stars"), 1, 5)) is not None:
        p["min_stars"] = st
    for k in PREFS_BOOL:
        if d.get(k) is True:
            p[k] = True

    if isinstance(d.get("priorities"), dict):
        for k in PRIORITY_KEYS:
            if (w := _int(d["priorities"].get(k), 0, 3)) is not None:
                r.priorities[k] = w
    r.intents = _ids(d.get("intents"), INTENTS)
    if (sd := _int(d.get("shift_days"), -60, 60)) is not None:
        r.shift_days = sd
    if d.get("transfer_kind") in ("taxi", "minivan", "shuttle"):
        r.transfer_kind = d["transfer_kind"]
    return r


def merge(rules: nlu.ParseResult, llm: nlu.ParseResult) -> nlu.ParseResult:
    r, added = rules, []
    for k, v in llm.updates.items():
        if k not in r.updates and not (k == "budget_level" and "budget" in r.updates):
            r.updates[k] = v
            added.append(_label_update(k, v))
    new_trav = {k: v for k, v in llm.travellers.items() if k not in r.travellers}
    if new_trav:
        r.travellers.update(new_trav)
        if "children" in new_trav and new_trav["children"]:
            added.append(f"дети: {new_trav['children']}")
        if "adults" in new_trav:
            added.append(f"взрослых: {new_trav['adults']}")
        if new_trav.get("pets"):
            added.append("с питомцем")
    for k, v in llm.prefs.items():
        if k == "exclude":
            r.prefs["exclude"] = list(dict.fromkeys(r.prefs.get("exclude", []) + v))
        elif k not in r.prefs:
            r.prefs[k] = v
            if lbl := _label_pref(k, v):
                added.append(lbl)
    for k, v in llm.priorities.items():
        r.priorities.setdefault(k, v)
    types = [t for t in llm.add_types if t not in r.add_types]
    if types:
        r.add_types += types
        added.append(", ".join(TYPE_LABELS[t] for t in types))
    excluded = set(r.prefs.get("exclude", []))
    llm.destination = llm.destination if llm.destination not in excluded else None
    llm.candidates = [c for c in llm.candidates if c not in excluded]
    if not r.destination and not r.candidates:
        if llm.destination or llm.candidates:
            r.destination, r.candidates, r.unknown_place = llm.destination, llm.candidates, None
            r.understood = [u for u in r.understood if "нет в каталоге" not in u]
            added.append(CITIES[r.destination]["name"] if r.destination else
                         "направления: " + ", ".join(CITIES[c]["name"] for c in r.candidates))
        elif llm.unknown_place and not r.unknown_place:
            r.unknown_place = llm.unknown_place
            added.append(f"«{llm.unknown_place}» (нет в каталоге)")
    if r.destination in excluded:
        r.destination = None
    r.candidates = [c for c in r.candidates if c not in excluded]
    for i in llm.intents:
        if i not in r.intents:
            r.intents.append(i)
    if not r.shift_days and llm.shift_days:
        r.shift_days = llm.shift_days
        added.append(f"сдвиг дат на {llm.shift_days:+d} дн.")
    r.transfer_kind = r.transfer_kind or llm.transfer_kind
    r.understood += [a for a in added if a]
    return r


def _label_update(k: str, v: Any) -> str:
    if k == "date_from":
        return f"с {v.day} {MONTHS_GEN[v.month - 1]}"
    if k == "nights":
        return f"{v} ноч."
    if k == "origin":
        return ORIGINS[v]["from"]
    if k == "budget":
        return f"бюджет до {rub(v)}"
    return {"low": "недорого", "high": "премиум"}.get(v, "")


def _label_pref(k: str, v: Any) -> str:
    return {"min_temp": "тепло", "max_temp": "не жарко", "no_visa": "без визы", "free_cancel": "бесплатная отмена",
            "direct_only": "без пересадок", "kids_club": "детский клуб",
            "max_flight_hours": f"перелёт ≤ {v:g} ч" if isinstance(v, float) else "",
            "meal_min": {"AI": "всё включено", "HB": "полупансион", "BB": "с завтраком"}.get(v, ""),
            "min_stars": f"от {v}★"}.get(k, "")


def _numbers(text: str) -> list:
    return sorted(re.findall(r"\d+", text))


class LLMAgent(RuleAgent):
    name = "llm"

    def __init__(self):
        self._client = None
        self._sem = asyncio.Semaphore(CONCURRENCY)
        self._fails = 0
        self._paused_until = 0.0
        self.stats = {"ok": 0, "errors": 0, "fallbacks": 0, "phrase_rejected": 0, "avg_latency_s": None}
        self._lat_sum = 0.0

    def _giga(self):
        if self._client is None:
            if not (os.getenv("GIGACHAT_CREDENTIALS") or os.getenv("GIGACHAT_ACCESS_TOKEN")):
                raise RuntimeError("GIGACHAT_CREDENTIALS не задан")
            from gigachat import GigaChat
            self._client = GigaChat(model=os.getenv("GIGACHAT_MODEL") or "GigaChat-2")
        return self._client

    async def aclose(self):
        if self._client is not None:
            await self._client.aclose()

    async def _ask(self, system: str, user: str, temperature: float, max_tokens: int) -> Optional[str]:
        if time.monotonic() < self._paused_until:
            self.stats["fallbacks"] += 1
            return None
        from gigachat.models import Chat, Messages, MessagesRole
        started = time.monotonic()
        try:
            async with asyncio.timeout(TIMEOUT_S):
                async with self._sem:
                    resp = await self._giga().achat(Chat(
                        messages=[Messages(role=MessagesRole.SYSTEM, content=system),
                                  Messages(role=MessagesRole.USER, content=user)],
                        temperature=temperature, max_tokens=max_tokens))
            text = resp.choices[0].message.content
        except Exception as e:
            self._fail(type(e).__name__)
            return None
        self._fails = 0
        self.stats["ok"] += 1
        self._lat_sum += time.monotonic() - started
        self.stats["avg_latency_s"] = round(self._lat_sum / self.stats["ok"], 2)
        return text

    def _fail(self, why: str):
        self._fails += 1
        self.stats["errors"] += 1
        self.stats["fallbacks"] += 1
        log.warning("GigaChat недоступен (%s), отвечают правила", why)
        if self._fails >= BREAKER_FAILS:
            self._paused_until = time.monotonic() + BREAKER_PAUSE_S
            self._fails = 0
            log.warning("GigaChat выключен на %s с после %s ошибок подряд", BREAKER_PAUSE_S, BREAKER_FAILS)

    async def understand(self, text: str, s: TripState) -> nlu.ParseResult:
        rules = await super().understand(text, s)
        if "reset" in rules.intents:
            return rules
        t = today()
        answer = await self._ask(_system_prompt(t), f"{_state_hint(s)}\nСообщение: {text}", 0.0, 400)
        if answer is None:
            return rules
        try:
            llm = to_parse_result(_extract_json(answer), t)
        except (ValueError, TypeError, AttributeError) as e:
            self._fail(f"невалидный JSON: {type(e).__name__}")
            return rules
        return merge(rules, llm)

    async def phrase(self, draft: str, s: TripState) -> str:
        if not PHRASE or len(draft) < 40:
            return draft
        answer = await self._ask(
            "Ты — дружелюбный турагент. Перепиши сообщение живее и короче, на «вы», без эмодзи-спама. "
            "Сохрани ВСЕ числа, даты, суммы, названия отелей, городов и авиакомпаний ровно как в исходнике. "
            "Ничего не добавляй от себя. Верни только новый текст.",
            draft, 0.3, 600)
        if not answer:
            return draft
        answer = answer.strip()
        if _numbers(answer) != _numbers(draft) or len(answer) > 2 * len(draft):
            self.stats["phrase_rejected"] += 1
            return draft
        return answer
