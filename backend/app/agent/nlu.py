"""Разбор запроса на правилах (заглушка до подключения LLM).

LLM-агент заменит этот модуль: ему нужно выдавать тот же ParseResult (или сразу
вызывать update_trip с теми же полями). Здесь — регулярки по типовым формулировкам,
чтобы сквозной сценарий работал без модели.
"""
import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Dict, List, Optional

from ..catalog import CITIES, MONTHS_GEN, MONTHS_PREP, ORIGINS, TYPE_LABELS

NUM_WORDS = {"один": 1, "одну": 1, "одного": 1, "два": 2, "две": 2, "двух": 2, "три": 3, "трех": 3, "четыре": 4,
             "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10, "пару": 2, "неделю": 7,
             "двое": 2, "трое": 3, "четверо": 4, "двумя": 2, "тремя": 3}

MONTH_STEMS = [("январ", 1), ("феврал", 2), ("март", 3), ("апрел", 4), ("ма[йяе]", 5), ("июн", 6), ("июл", 7),
               ("август", 8), ("сентябр", 9), ("октябр", 10), ("ноябр", 11), ("декабр", 12)]


@dataclass
class ParseResult:
    updates: Dict = field(default_factory=dict)      # origin, date_from, nights, budget, budget_level
    travellers: Dict = field(default_factory=dict)
    prefs: Dict = field(default_factory=dict)
    priorities: Dict = field(default_factory=dict)
    add_types: List[str] = field(default_factory=list)
    destination: Optional[str] = None
    candidates: List[str] = field(default_factory=list)
    intents: List[str] = field(default_factory=list)
    shift_days: int = 0
    transfer_kind: Optional[str] = None
    unknown_place: Optional[str] = None
    understood: List[str] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not (self.updates or self.travellers or self.prefs or self.priorities or self.add_types
                    or self.destination or self.candidates or self.intents or self.shift_days or self.unknown_place)


def _num(s: str) -> Optional[int]:
    if s is None:
        return None
    s = s.strip()
    if s.isdigit():
        return int(s)
    return NUM_WORDS.get(s)


def _month_date(month: int, today: date, day: Optional[int] = None) -> date:
    year = today.year if month >= today.month else today.year + 1
    if day:
        try:
            d = date(year, month, day)
        except ValueError:
            d = date(year, month, 1)
        return d if d >= today else date(year + 1, month, day)
    if month == today.month:
        return today + timedelta(days=7)
    d = date(year, month, 5)
    return d + timedelta(days=(5 - d.weekday()) % 7)  # ближайшая суббота с 5-го числа


def _month_in(text: str) -> Optional[int]:
    for stem, m in MONTH_STEMS:
        if re.search(rf"\b{stem}\w*", text):
            return m
    return None


def parse(raw: str, today: date) -> ParseResult:
    t = " " + raw.lower().replace("ё", "е") + " "
    r = ParseResult()

    # ---------- сброс / служебные намерения
    if re.search(r"\b(заново|сначала|сброс|начать с нуля|новая поездка)\b", t):
        r.intents.append("reset")
        return r
    if re.search(r"\b(оформ|оплат|бронируй|забронир|покупа|беру|купить)", t):
        r.intents.append("checkout")
    if re.search(r"ранн\w* заезд", t):
        r.intents.append("early_checkin")
    if re.search(r"поздн\w* выезд", t):
        r.intents.append("late_checkout")
    if re.search(r"напомина|напомни", t):
        r.intents.append("reminders")
    if re.search(r"почини|стыковк", t):
        r.intents.append("fix_dates")
    if re.search(r"уложи\w* в бюджет", t):
        r.intents.append("fit_budget")
    if re.search(r"трансфер|минивэн|минивен|шаттл|такси", t):
        r.intents.append("transfer")
        for kind, rx in (("minivan", r"минив"), ("shuttle", r"шаттл|автобус"), ("taxi", r"такси")):
            if re.search(rx, t):
                r.transfer_kind = kind
    if re.search(r"\bпочему\b|объясни", t):
        r.intents.append("why")
    if re.search(r"не знаю|удиви|куда угодно|куда-нибудь|без разницы куда|предложи", t):
        r.intents.append("surprise")
    if re.search(r"^\s*(привет|здравствуй|добрый|хай|hi|hello)\W*\s*$", t):
        r.intents.append("greeting")
    if re.search(r"друг\w* (направлени|стран|город|мест)|не хочу туда|не туда|поменяй направление", t):
        r.intents.append("other_destination")
    if re.search(r"друг\w* отел|поменяй отель|не нравится отель", t):
        r.intents.append("other_hotel")
    if re.search(r"друг\w* (рейс|билет|перел)|поменяй (рейс|билет)", t):
        r.intents.append("other_flight")

    # ---------- сдвиг дат
    m = re.search(r"на (\d+|\w+) (дн\w*|недел\w*) (позже|раньше)|(позже|раньше) на (\d+|\w+) (дн\w*|недел\w*)", t)
    if m:
        n_s, unit, direction = (m.group(1), m.group(2), m.group(3)) if m.group(1) else (m.group(5), m.group(6), m.group(4))
        n = _num(n_s) or 1
        if unit.startswith("недел"):
            n = 7 * (n if n_s != "неделю" else 1)
        r.shift_days = n if direction == "позже" else -n
        r.understood.append(f"сдвиг дат на {r.shift_days:+d} дн.")
    elif re.search(r"на день (позже|раньше)", t):
        r.shift_days = 1 if "позже" in t else -1

    # ---------- даты
    m = re.search(r"с (\d{1,2})(?:\s*(\w+))? по (\d{1,2}) (\w+)", t)
    if m and _month_in(m.group(4)):
        mon2 = _month_in(m.group(4))
        mon1 = _month_in(m.group(2) or "") or mon2
        d1 = _month_date(mon1, today, int(m.group(1)))
        d2 = _month_date(mon2, today, int(m.group(3)))
        if d2 < d1:
            d2 = date(d1.year + (1 if mon2 < mon1 else 0), mon2, int(m.group(3)))
        r.updates["date_from"] = d1
        r.updates["nights"] = max(1, (d2 - d1).days)
        r.understood.append(f"даты {d1.day} {MONTHS_GEN[d1.month-1]} – {d2.day} {MONTHS_GEN[d2.month-1]}")
    else:
        m = re.search(r"\b(\d{1,2})\.(\d{1,2})(?:\.\d{2,4})?\b", t)
        m2 = re.search(r"\b(\d{1,2}) (январ|феврал|март|апрел|ма[яй]|июн|июл|август|сентябр|октябр|ноябр|декабр)\w*", t)
        if m and 1 <= int(m.group(2)) <= 12:
            d = _month_date(int(m.group(2)), today, int(m.group(1)))
            r.updates["date_from"] = d
            r.understood.append(f"с {d.day} {MONTHS_GEN[d.month-1]}")
        elif m2:
            d = _month_date(_month_in(m2.group(2)), today, int(m2.group(1)))
            r.updates["date_from"] = d
            r.understood.append(f"с {d.day} {MONTHS_GEN[d.month-1]}")
        elif re.search(r"нов\w* год", t):
            d = date(today.year if today.month < 12 or today.day < 28 else today.year + 1, 12, 29)
            r.updates["date_from"] = d
            r.understood.append("на Новый год")
        elif re.search(r"майски", t):
            r.updates["date_from"] = _month_date(5, today, 1)
            r.understood.append("на майские")
        elif re.search(r"(на )?(этих |ближайши\w* )?выходн", t):
            d = today + timedelta(days=(4 - today.weekday()) % 7 or 7)
            r.updates["date_from"] = d
            r.updates.setdefault("nights", 2)
            r.understood.append("на выходные")
        elif re.search(r"через (\d+|\w+) недел", t) or "через неделю" in t:
            mm = re.search(r"через (\d+|\w+) недел", t)
            n = (_num(mm.group(1)) if mm and mm.group(1) != "неделю" else 1) or 1
            r.updates["date_from"] = today + timedelta(days=7 * n)
            r.understood.append(f"через {n} нед.")
        elif "через месяц" in t or "в следующем месяце" in t:
            r.updates["date_from"] = today + timedelta(days=30)
            r.understood.append("через месяц")
        elif re.search(r"\b(завтра|послезавтра|на днях|срочно|горящ)", t):
            r.updates["date_from"] = today + timedelta(days=2)
            r.understood.append("в ближайшие дни")
        else:
            mon = _month_in(t)
            season = {"летом": 7, "осенью": 10, "зимой": 1, "весной": 4}
            if mon is None:
                mon = next((v for k, v in season.items() if k in t), None)
            if mon:
                d = _month_date(mon, today)
                r.updates["date_from"] = d
                r.understood.append(f"в {MONTHS_PREP[mon - 1]}")

    # ---------- длительность
    m = re.search(r"на (\d+|\w+) (ноч\w*|дн\w*|недел\w*)(?! (позже|раньше))", t)
    if m and _num(m.group(1)):
        n = _num(m.group(1))
        unit = m.group(2)
        if m.group(1) == "неделю":
            nights = 7
        elif unit.startswith("недел"):
            nights = 7 * n
        elif unit.startswith("ноч"):
            nights = n
        else:
            nights = max(1, n - 1)
        if not r.shift_days:
            r.updates["nights"] = min(nights, 30)
            r.understood.append(f"{r.updates['nights']} ноч.")
    elif re.search(r"на неделю(?! (позже|раньше))", t):
        r.updates["nights"] = 7
    elif re.search(r"на (две|2) недели", t):
        r.updates["nights"] = 14
    elif re.search(r"на (целый )?месяц", t):
        r.updates["nights"] = 28
        r.understood.append("на месяц")

    # ---------- откуда
    m = re.search(r"\bиз (\w+)", t)
    if m:
        for oid, o in ORIGINS.items():
            if any(m.group(1).startswith(st) for st in o["stems"]):
                r.updates["origin"] = oid
                r.understood.append(o["from"])

    # ---------- бюджет
    m = re.search(r"(\d+[\d\s]*[.,]?\d*)\s*(млн|миллион)", t)
    if m:
        r.updates["budget"] = int(float(m.group(1).replace(" ", "").replace(",", ".")) * 1_000_000)
    else:
        m = re.search(r"(\d[\d\s]*)\s*(тыс\w*|т\.?р\.?|тр|к|k)(?=\W)", t) or re.search(r"(\d[\d\s]{3,})\s*(₽|руб|р\b|рублей)?", t)
        if m:
            val = int(re.sub(r"\s", "", m.group(1)))
            if m.group(2) and m.group(2)[0] in "тkк":
                val *= 1000
            if val >= 10000:
                r.updates["budget"] = val
    if "budget" in r.updates:
        r.understood.append(f"бюджет до {r.updates['budget']:,} ₽".replace(",", " "))
    if re.search(r"недорог|бюджетн|дешев|эконом|подешевле|не дорого|скромн", t) and "budget" not in r.updates:
        if re.search(r"(бюджет|подешевле|дешевле) (меньше|поменьше|ниже)|дешевле|подешевле|урежь|сократи бюджет", t):
            r.intents.append("reduce_budget")
        else:
            r.updates["budget_level"] = "low"
            r.understood.append("недорого")
    if re.search(r"бюджет (меньше|поменьше|ниже)|меньше денег|урежь", t) and "reduce_budget" not in r.intents:
        r.intents.append("reduce_budget")
    if re.search(r"премиум|роскош|шикарн|люкс|дорог\w* отдых|не экономим|деньги не проблема", t):
        r.updates["budget_level"] = "high"
        r.understood.append("премиум")
    if re.search(r"можно дороже|бюджет больше|получше|подороже", t):
        r.intents.append("increase_budget")

    # ---------- кто едет
    kids = None
    m = re.search(r"(\d+|двумя|тремя|двое|трое) (дет|ребен)", t)
    if m:
        kids = _num(m.group(1))
    elif re.search(r"с детьми|детей", t):
        kids = 2
    elif re.search(r"с ребенком|с ребенк|с сыном|с дочк|с дочерью|с малыш|ребенок|с ребёнком", t):
        kids = 1
    if kids:
        r.travellers["children"] = kids
        ages = [int(a) for a in re.findall(r"(\d{1,2})\s*(?:лет|года|год|мес)", t) if int(a) < 18]
        if ages:
            r.travellers["child_ages"] = ages[:kids]
        r.understood.append(f"дети: {kids}")
    m = re.search(r"(\d+) взросл", t)
    adults = None
    if m:
        adults = int(m.group(1))
    elif re.search(r"\b(один|одна|соло|в одиночку|сам\b|сама\b)", t):
        adults = 1
    elif re.search(r"\b(вдвоем|с женой|с мужем|с девушкой|с парнем|парой|с подругой|с другом|медовый)", t):
        adults = 2
    elif re.search(r"\bвтроем\b", t):
        adults = 3 - (kids or 0)
    elif re.search(r"\bвчетвером\b", t):
        adults = 4 - (kids or 0)
    elif re.search(r"с друзьями|компани", t):
        adults = 4
    elif kids and re.search(r"с женой|с мужем|семь", t):
        adults = 2
    if re.search(r"с родителями|с мамой|с папой", t):
        adults = (adults or 1) + (2 if "родител" in t else 1)
    if adults:
        r.travellers["adults"] = max(1, adults)
        r.understood.append(f"взрослых: {adults}")
    if re.search(r"с собак|с псом|с кот|с кошк|с питомц|с щенк|с собачк", t):
        r.travellers["pets"] = True
        r.understood.append("с питомцем")
    if re.search(r"без (собак|питомц|кот)", t):
        r.travellers["pets"] = False

    # ---------- тип отдыха и ограничения
    types = {
        "beach": r"мор[еяю]|пляж|океан|купат|загора|остров|побережь",
        "city": r"город|экскурси|музе|архитектур|погулять|достопримеч|истори",
        "food": r"\bед[аыу]\b|гастро|вин[оа]|кухн|покушать|рестора",
        "party": r"тусов|вечерин|ночн\w* клуб|ночн\w* жизн|потусить",
        "relax": r"спокойн|тих|отдохнуть|релакс|уедин|расслаб|перезагруз",
        "nature": r"природ|гор[ыа]|поход|треккинг|водопад|джунгл",
        "shopping": r"шопинг|магазин|покупк",
    }
    for k, rx in types.items():
        if re.search(rx, t):
            r.add_types.append(k)
    if r.add_types:
        r.understood.append(", ".join(TYPE_LABELS[k] for k in r.add_types))
    if re.search(r"тепл|жар[ак]|солнц|лето зимой", t):
        r.prefs["min_temp"] = 25
        r.understood.append("тепло")
    if re.search(r"не жарк|без жары|не жара|прохладн", t):
        r.prefs["max_temp"] = 30
        r.prefs.pop("min_temp", None)
    m = re.search(r"(?:лететь|перел\w*|полет\w*) (?:не больше|до|не дольше|максимум) (\d+)", t) or \
        re.search(r"(?:не больше|до|не дольше|максимум) (\d+) час\w* (?:лететь|перел|в пути|полета)", t)
    if m:
        r.prefs["max_flight_hours"] = float(m.group(1))
        r.understood.append(f"перелёт ≤ {m.group(1)} ч")
    elif re.search(r"без долг\w* перел|недалеко|не далеко|поближе|ближе|коротк\w* перел|не долго лететь|недолго лететь|близко", t):
        r.prefs["max_flight_hours"] = 5.0
        r.understood.append("без долгих перелётов")
    if re.search(r"без пересад|прям\w* рейс", t):
        r.prefs["direct_only"] = True
    if re.search(r"без виз|безвиз", t):
        r.prefs["no_visa"] = True
        r.understood.append("без визы")
    if re.search(r"вс[её] включено|all inclusive|ультра", t):
        r.prefs["meal_min"] = "AI"
        r.understood.append("всё включено")
        if "city" not in r.add_types and "beach" not in r.add_types:
            r.add_types.append("beach")  # «всё включено» — это почти всегда пляжный отель
    elif re.search(r"полупансион", t):
        r.prefs["meal_min"] = "HB"
    elif re.search(r"с завтрак", t):
        r.prefs["meal_min"] = "BB"
    if re.search(r"бесплатн\w* отмен|можно отменить|с отменой|возвратн", t):
        r.prefs["free_cancel"] = True
        r.understood.append("бесплатная отмена")
    if re.search(r"перв\w* лини|у моря|рядом с пляжем|на берегу", t):
        r.prefs["max_beach_m"] = 200
    if re.search(r"детск\w* клуб|анимаци", t):
        r.prefs["kids_club"] = True
    m = re.search(r"(\d)\s*(?:звезд|\*|★)", t)
    if m:
        r.prefs["min_stars"] = int(m.group(1))
    elif re.search(r"пятерк|пятизвезд", t):
        r.prefs["min_stars"] = 5
    if re.search(r"с багаж", t):
        r.prefs["baggage"] = True
    if re.search(r"без ночн|не ночью|днем лететь|дневн\w* рейс", t):
        r.prefs["daytime_flights"] = True

    # ---------- направление
    words = re.findall(r"[а-яa-z\-]+", t)
    found_city = [cid for cid, c in CITIES.items() if any(w.startswith(st) for w in words for st in c["stems"])]
    found_country = [cid for cid, c in CITIES.items() if any(w.startswith(st) for w in words for st in c["country_stems"])]
    negated = re.search(r"\bне (в|на) (\w+)", t)
    if negated:
        neg_ids = [cid for cid, c in CITIES.items() if any(negated.group(2).startswith(st) for st in c["stems"] + c["country_stems"])]
        found_city = [x for x in found_city if x not in neg_ids]
        found_country = [x for x in found_country if x not in neg_ids]
        if neg_ids:
            r.prefs["exclude"] = neg_ids
    if not found_city and not found_country:
        # «хочу в Сочи»: место с большой буквы, которого нет в каталоге
        for m in re.finditer(r"\b(?:в|во|на)\s+([А-ЯЁ][а-яё\-]+)", raw):
            word = m.group(1).lower()
            if not _month_in(word) and word not in ("море", "майские"):
                r.unknown_place = m.group(1)
                r.understood.append(f"«{m.group(1)}» (нет в каталоге)")
                break
    if len(found_city) == 1 and not re.search(r"\bили\b", t):
        r.destination = found_city[0]
        r.understood.append(CITIES[found_city[0]]["name"])
    elif found_city or found_country:
        r.candidates = list(dict.fromkeys(found_city + found_country))
        r.understood.append("направления: " + ", ".join(CITIES[c]["name"] for c in r.candidates))

    # ---------- приоритеты
    if re.search(r"главное (цена|дешев)|подешевле", t):
        r.priorities["price"] = 3
    if re.search(r"с ребен|с детьми|с малыш", t):
        r.priorities["flight_time"] = 2
        r.priorities["flexibility"] = 2
    if re.search(r"главное (качеств|комфорт|сервис)|комфортн", t):
        r.priorities["rating"] = 3
    return r
