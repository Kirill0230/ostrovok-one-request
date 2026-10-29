from datetime import datetime
from typing import List

from ..catalog import CITIES, MEALS, ORIGINS, dt_label, hours, rub
from ..models import Card, Fact, Media, Reason, TripState

DEST_REASONS = [Reason(id="expensive", label="💸 Дорого"), Reason(id="far", label="✈️ Далеко лететь"),
                Reason(id="hot", label="🥵 Слишком жарко"), Reason(id="cold", label="🥶 Холодно"),
                Reason(id="want_city", label="🏙 Хочу город"), Reason(id="want_beach", label="🏖 Хочу море"),
                Reason(id="visa", label="🛂 Не хочу визу"), Reason(id="been", label="🔁 Уже был(а)")]
HOTEL_REASONS = [Reason(id="expensive", label="💸 Дорого"), Reason(id="location", label="📍 Далеко от моря/центра"),
                 Reason(id="meals", label="🍽 Хочу питание получше"), Reason(id="rating", label="⭐ Слабый рейтинг"),
                 Reason(id="nonref", label="↩️ Нужна отмена"), Reason(id="stars", label="🏨 Хочу классом выше")]
FLIGHT_REASONS = [Reason(id="expensive", label="💸 Дорого"), Reason(id="time", label="🌙 Неудобное время"),
                  Reason(id="connection", label="🔀 Не хочу пересадку"), Reason(id="baggage", label="🧳 Нужен багаж"),
                  Reason(id="long", label="⏱ Долго в пути")]


def destination_card(opt: dict, s: TripState) -> Card:
    from ..catalog import media_urls
    c, cl, e = opt["city"], opt["climate"], opt["estimate"]
    urls = media_urls(c)
    weather = f"+{cl['air']}°" + (f" · море +{cl['sea']}°" if cl["sea"] else "")
    month = s.date_from.month if s.date_from else datetime.now().month
    badges = []
    if month in c["best_months"]:
        badges.append("Лучший сезон")
    if s.travellers.children and c["kids"] >= 3:
        badges.append("Для семей")
    badges += c["tags"][:2]
    return Card(
        id=c["id"], kind="destination", title=f"{c['name']}", subtitle=f"{c['flag']} {c['country']} · {c['pitch']}",
        media=Media(video_url=urls["video_url"], poster_url=urls["poster_url"], gradient=c["gradient"], emoji=c["emoji"],
                    ai_generated=bool(urls["video_url"]) and not urls["credit"], credit=urls["credit"]),
        facts=[Fact(icon="🌡", label=weather), Fact(icon="✈️", label=f"{hours(opt['flight_hours'])}" + ("" if c["direct"] else ", пересадка")),
               Fact(icon="☔", label=f"{cl['rain']} дн. дождя"), Fact(icon="🛂", label=c["visa"])],
        price=e["total"], price_note=f"≈ за всё на {s.nights} ноч. (перелёт, отель, трансфер)",
        badges=badges, why=opt["why"], tradeoffs=opt["tradeoffs"], risks=opt["risks"], score=opt["score"],
        compare={"Погода": weather, "Перелёт": hours(opt["flight_hours"]), "Цена ≈": rub(e["total"]),
                 "Дождь": f"{cl['rain']} дн.", "Виза": "нет" if c["visa_free"] else "нужна",
                 "Сезон": "лучший" if month in c["best_months"] else "так себе"},
        dislike_reasons=DEST_REASONS,
        data={"highlights": c["highlights"], "estimate": e, "city_id": c["id"]},
    )


def _city_media(c: dict, emoji: str) -> Media:
    from ..catalog import media_urls
    urls = media_urls(c)
    return Media(video_url=urls["video_url"], poster_url=urls["poster_url"], gradient=c["gradient"], emoji=emoji,
                 ai_generated=bool(urls["video_url"]) and not urls["credit"], credit=urls["credit"])


def hotel_card(opt: dict, s: TripState) -> Card:
    h = opt["hotel"]
    c = CITIES[h["city_id"]]
    loc = f"до моря {h['beach_m']} м" if h["beach_m"] is not None else f"центр {h['center_km']} км"
    facts = [Fact(icon="🍽", label=MEALS[h["meal"]]), Fact(icon="📍", label=loc),
             Fact(icon="↩️", label=f"отмена до {h['cancel_days']} дн." if h["free_cancel"] else "невозвратный"),
             Fact(icon="🕑", label=f"заезд с {h['check_in']}")]
    if h["kids_club"]:
        facts.append(Fact(icon="🧸", label="детский клуб"))
    if h["pets_allowed"]:
        facts.append(Fact(icon="🐾", label="можно с питомцем"))
    return Card(
        id=h["id"], kind="hotel", title=h["name"],
        subtitle=f"{'★' * h['stars']} · {h['rating']} ({h['reviews']} отзывов) · {c['name']}",
        media=_city_media(c, emoji=h["emoji"]),
        facts=facts, price=h["total"], price_note=f"за {h['nights']} ноч. · {rub(h['night_price'])}/ночь",
        badges=h["amenities"][:3], why=opt["why"], tradeoffs=opt["tradeoffs"], risks=opt["risks"], score=opt["score"],
        compare={"Цена": rub(h["total"]), "Звёзды": "★" * h["stars"], "Рейтинг": str(h["rating"]), "Питание": MEALS[h["meal"]],
                 "Расположение": loc, "Отмена": "бесплатно" if h["free_cancel"] else "нет", "Детский клуб": "да" if h["kids_club"] else "нет"},
        dislike_reasons=HOTEL_REASONS, data={"night_price": h["night_price"], "beach_m": h["beach_m"], "rating": h["rating"],
                                             "stars": h["stars"], "meal": h["meal"]},
    )


def flight_card(opt: dict, s: TripState) -> Card:
    f = opt["flight"]
    c = CITIES[s.destination_id]
    o, b = f["out"], f["back"]
    od, oa = datetime.fromisoformat(o["dep"]), datetime.fromisoformat(o["arr"])
    bd, ba = datetime.fromisoformat(b["dep"]), datetime.fromisoformat(b["arr"])
    route = "прямой" if not f["via"] else f"через {f['via']}"
    return Card(
        id=f["id"], kind="flight", title=f"{f['airline']} · {route}",
        subtitle=f"{o['from']} → {o['to']} → {b['to']}",
        media=Media(gradient=["#1e293b", c["gradient"][0]], emoji="✈️"),
        facts=[Fact(icon="🛫", label=f"Туда: {dt_label(od)} → {oa:%H:%M}" + (" (+1)" if oa.date() > od.date() else "")),
               Fact(icon="🛬", label=f"Обратно: {dt_label(bd)} → {ba:%H:%M}" + (" (+1)" if ba.date() > bd.date() else "")),
               Fact(icon="⏱", label=f"{hours(o['duration_h'])} / {hours(b['duration_h'])}"),
               Fact(icon="🧳", label="багаж включён" if f["baggage"] else "только ручная кладь")],
        price=f["total"], price_note=f"туда-обратно на всех · {rub(f['per_adult'])} за взрослого",
        why=opt["why"], tradeoffs=opt["tradeoffs"], risks=opt["risks"], score=opt["score"],
        compare={"Цена": rub(f["total"]), "Вылет": f"{od:%H:%M}", "Прилёт": f"{oa:%H:%M}", "В пути": hours(o["duration_h"]),
                 "Пересадка": f["via"] or "нет", "Багаж": "да" if f["baggage"] else "нет"},
        dislike_reasons=FLIGHT_REASONS, data={"total": f["total"], "slot": f["slot"]},
    )


def origin_name(s: TripState) -> str:
    return ORIGINS.get(s.origin, ORIGINS["moscow"])["name"]


def bullets(items: List[str]) -> str:
    return "\n".join(f"• {x}" for x in items)
