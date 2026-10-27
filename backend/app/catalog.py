"""Загрузка каталога направлений и общие справочники/форматтеры."""
import json
import os
from datetime import date, datetime
from pathlib import Path
from typing import Dict, Optional

ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = Path(os.getenv("DATA_DIR", ROOT / "data"))
VIDEOS_DIR = Path(os.getenv("VIDEOS_DIR", DATA_DIR / "videos"))

_raw = json.loads((DATA_DIR / "cities.json").read_text(encoding="utf-8"))
CITIES: Dict[str, dict] = {c["id"]: c for c in _raw["cities"]}
ORIGINS: Dict[str, dict] = _raw["origins"]


def _load_climate() -> dict:
    """Климатические нормы Open-Meteo (data/climate.json, backend/scripts/fetch_climate.py).
    Если файла или города в нём нет — остаются запасные примерные значения из cities.json."""
    path = Path(os.getenv("CLIMATE_PATH", DATA_DIR / "climate.json"))
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    for cid, row in data.get("cities", {}).items():
        c = CITIES.get(cid)
        if c and len(row.get("air") or []) == 12 and len(row.get("rain") or []) == 12:
            sea = row.get("sea") or [None] * 12
            c["climate"] = {"air": row["air"], "sea": sea, "rain": row["rain"]}
            c["climate_source"] = "open-meteo"
    return {k: v for k, v in data.items() if k != "cities"}


CLIMATE_META = _load_climate()

TYPE_LABELS = {
    "beach": "пляж", "city": "город", "food": "еда", "party": "тусовки",
    "relax": "спокойный отдых", "nature": "природа", "shopping": "шопинг",
}
MEALS = {"RO": "Без питания", "BB": "Завтраки", "HB": "Полупансион", "AI": "Всё включено"}
MEAL_RANK = {"RO": 0, "BB": 1, "HB": 2, "AI": 3}

MONTHS_NOM = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август",
              "сентябрь", "октябрь", "ноябрь", "декабрь"]
MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня", "июля", "августа",
              "сентября", "октября", "ноября", "декабря"]
MONTHS_PREP = ["январе", "феврале", "марте", "апреле", "мае", "июне", "июле", "августе",
               "сентябре", "октябре", "ноябре", "декабре"]


def city(city_id: Optional[str]) -> Optional[dict]:
    return CITIES.get(city_id) if city_id else None


def media_urls(c: dict) -> Dict[str, Optional[str]]:
    """Ссылки на видео/постер, только если файл реально лежит в data/videos."""
    video = c.get("video")
    video_url = f"/videos/{video}" if video and (VIDEOS_DIR / video).exists() else None
    poster_url = None
    for ext in ("jpg", "jpeg", "png", "webp"):
        if (VIDEOS_DIR / f"{c['id']}.{ext}").exists():
            poster_url = f"/videos/{c['id']}.{ext}"
            break
    return {"video_url": video_url, "poster_url": poster_url}


def rub(n: float) -> str:
    return f"{int(round(n)):,}".replace(",", " ") + " ₽"


def hours(h: float) -> str:
    total = int(round(h * 60))
    hh, mm = divmod(total, 60)
    if mm == 0:
        return f"{hh} ч"
    if hh == 0:
        return f"{mm} мин"
    return f"{hh} ч {mm} мин"


def day_label(d: date) -> str:
    return f"{d.day} {MONTHS_GEN[d.month - 1]}"


def range_label(d1: date, d2: date) -> str:
    if d1.month == d2.month:
        return f"{d1.day}–{d2.day} {MONTHS_GEN[d1.month - 1]}"
    return f"{day_label(d1)} – {day_label(d2)}"


def dt_label(dt: datetime) -> str:
    return f"{dt.day} {MONTHS_GEN[dt.month - 1]}, {dt:%H:%M}"


def plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return one
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return few
    return many


def travellers_label(adults: int, children: int, pets: bool = False) -> str:
    parts = [f"{adults} {plural(adults, 'взрослый', 'взрослых', 'взрослых')}"]
    if children:
        parts.append(f"{children} {plural(children, 'ребёнок', 'ребёнка', 'детей')}")
    if pets:
        parts.append("питомец")
    return " + ".join(parts)
