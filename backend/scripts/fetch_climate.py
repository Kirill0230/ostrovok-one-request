import json
import sys
import time
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CITIES_PATH = ROOT / "data" / "cities.json"
OUT_PATH = ROOT / "data" / "climate.json"

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"
MARINE_URL = "https://marine-api.open-meteo.com/v1/marine"
AIR_PERIOD = ("2021-01-01", "2025-12-31")
SEA_PERIOD = ("2023-01-01", "2025-12-31")
RAIN_THRESHOLDS = (1.0, 2.5, 5.0)
RAIN_DAY_MM = 2.5
PAUSE_S = 20


def _get(url: str, params: dict, retries: int = 4) -> dict:
    full = f"{url}?{urllib.parse.urlencode(params)}"
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(full, timeout=120) as r:
                return json.loads(r.read())
        except Exception as e:
            if attempt == retries - 1:
                raise RuntimeError(f"Open-Meteo не ответил: {e}") from e
            time.sleep(5 * (attempt + 1))
    raise AssertionError


def air_and_rain(lat: float, lon: float):
    d = _get(ARCHIVE_URL, {"latitude": lat, "longitude": lon, "start_date": AIR_PERIOD[0], "end_date": AIR_PERIOD[1],
                           "daily": "temperature_2m_max,precipitation_sum", "timezone": "auto"})["daily"]
    temps, months_seen, precip = defaultdict(list), defaultdict(set), defaultdict(float)
    rain_days = {th: defaultdict(int) for th in RAIN_THRESHOLDS}
    for day, t, p in zip(d["time"], d["temperature_2m_max"], d["precipitation_sum"]):
        m = int(day[5:7])
        months_seen[m].add(day[:7])
        if t is not None:
            temps[m].append(t)
        if p is not None:
            precip[m] += p
            for th in RAIN_THRESHOLDS:
                if p >= th:
                    rain_days[th][m] += 1
    per_month = lambda counter: [round(counter[m] / len(months_seen[m])) for m in range(1, 13)]
    air = [round(sum(temps[m]) / len(temps[m])) for m in range(1, 13)]
    return {"air": air, "rain": per_month(rain_days[RAIN_DAY_MM]), "precip_mm": per_month(precip),
            "rain_by_threshold": {f"{th:g}": per_month(rain_days[th]) for th in RAIN_THRESHOLDS}}


def sea(lat: float, lon: float):
    h = _get(MARINE_URL, {"latitude": lat, "longitude": lon, "start_date": SEA_PERIOD[0], "end_date": SEA_PERIOD[1],
                          "hourly": "sea_surface_temperature", "timezone": "auto"})["hourly"]
    vals = defaultdict(list)
    for t, v in zip(h["time"], h["sea_surface_temperature"]):
        if v is not None:
            vals[int(t[5:7])].append(v)
    if any(not vals[m] for m in range(1, 13)):
        raise RuntimeError(f"нет данных о море в точке {lat},{lon} — сдвиньте sea_lat/sea_lon дальше от берега")
    return [round(sum(vals[m]) / len(vals[m])) for m in range(1, 13)]


def main():
    force = "--force" in sys.argv
    cities = json.loads(CITIES_PATH.read_text(encoding="utf-8"))["cities"]
    prev = json.loads(OUT_PATH.read_text(encoding="utf-8")) if OUT_PATH.exists() and not force else {}
    out = {
        "source": "Open-Meteo.com — Historical Weather API (ERA5) и Marine API",
        "license": "CC BY 4.0, https://open-meteo.com/",
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "method": {
            "air": f"средняя дневная максимальная температура воздуха, {AIR_PERIOD[0][:4]}–{AIR_PERIOD[1][:4]}",
            "rain": f"среднее число дней с осадками ≥ {RAIN_DAY_MM:g} мм, {AIR_PERIOD[0][:4]}–{AIR_PERIOD[1][:4]}",
            "sea": f"средняя температура поверхности моря, {SEA_PERIOD[0][:4]}–{SEA_PERIOD[1][:4]}",
        },
        "cities": prev.get("cities", {}),
    }
    todo = [c for c in cities if force or c["id"] not in out["cities"]]
    for i, c in enumerate(todo):
        g = c["geo"]
        row = air_and_rain(g["lat"], g["lon"])
        row["sea"] = sea(g["sea_lat"], g["sea_lon"]) if "sea_lat" in g else None
        out["cities"][c["id"]] = row
        OUT_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{c['name']:12} воздух {row['air']}  море {row['sea']}  дожди {row['rain']}", flush=True)
        if i < len(todo) - 1:
            time.sleep(PAUSE_S)
    print(f"\nГотово: {OUT_PATH} ({len(out['cities'])} из {len(cities)} направлений, {date.today()})")


if __name__ == "__main__":
    main()
