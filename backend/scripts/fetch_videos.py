import json
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT_DIR = ROOT / "data" / "videos"
CREDITS = OUT_DIR / "credits.json"
API = "https://commons.wikimedia.org/w/api.php"
UA = "odin-hackathon/0.1 (student project)"
FREE = re.compile(r"^(CC0|Public domain|PD|CC BY(-SA)? [1-4]\.0)", re.I)
LENGTH_S = 8.0
VF = "crop='min(iw,ih*4/5)':'min(ih,iw*5/4)',scale=720:900:flags=lanczos,fps=24,setsar=1,format=yuv420p"

SOURCES = {
    "antalya": ("File:2025 Antalya Turkey.webm", 2, ""),
    "dubai": ("File:Dubai creek.webm", 0.5, "залив Дубай-Крик"),
    "phuket": ("File:James Bond Island P1120312.ogv", 1, "залив Пхангнга, экскурсия с Пхукета"),
    "pattaya": ("File:Pattaya Jomtien private beach at Birds and Bees resort in Pattaya Thailand by Don Ramey Logan.webm", 2, "пляж Джомтьен"),
    "sanya": ("File:三亞灣2020.webm", 1, "залив Санья"),
    "bali": ("File:Kelingking Beach.webm", 1, "пляж Келингкинг, Нуса-Пенида"),
    "maldives": ("File:Stingray in Maldives.webm", 4, "скат в лагуне"),
    "goa": ("File:Aguada beach, Goa.webm", 2, "пляж Агуада"),
    "batumi": ("File:Batumi2025ship-and-sunset.webm", 2, ""),
    "istanbul": ("File:View of Haydarpaşa railway station and the Bosphorus from Kadıköy Pier 20250818.webm", 1, "Босфор"),
    "bangkok": ("File:Bangkok Aerial Footage by Don Ramey Logan.webm", 2, ""),
    "tbilisi": ("File:Panorama of Tbilisi, Georgia - Jan. 2016.webm", 2, ""),
    "yerevan": ("File:Yerevan Republic Square Musical Fountains.webm", 2, "площадь Республики"),
    "seoul": ("File:View-of-Seoul-from63-Building-2019-7-27.webm", 2, ""),
    "tokyo": ("File:Shibuya crossing time-lapse 2019-01-05.webm", 2, "перекрёсток Сибуя"),
}


def _api(**params) -> dict:
    url = API + "?" + urllib.parse.urlencode({"action": "query", "format": "json", **params})
    with urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": UA}), timeout=60) as r:
        page = next(iter(json.loads(r.read())["query"]["pages"].values()))
    if "missing" in page:
        raise RuntimeError(f"нет на Commons: {params.get('titles')}")
    return page


def describe(title: str) -> dict:
    info = _api(titles=title, prop="imageinfo", iiprop="url|size|extmetadata",
                iiextmetadatafilter="LicenseShortName|Artist|LicenseUrl")["imageinfo"][0]
    meta = info["extmetadata"]
    lic = meta["LicenseShortName"]["value"]
    if not FREE.match(lic):
        raise RuntimeError(f"{title}: лицензия «{lic}» не подходит")
    derivs = _api(titles=title, prop="videoinfo", viprop="derivatives")["videoinfo"][0]["derivatives"]
    order = {"1080p.vp9.webm": 0, "720p.vp9.webm": 1, "480p.vp9.webm": 2}
    best = min(derivs, key=lambda d: order.get(d.get("transcodekey") or "", 9))
    author = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", meta.get("Artist", {}).get("value", ""))).strip()
    author = re.sub(r"\s*\((talk|обсуждение)[^)]*\)", "", author).strip()
    return {"src": best["src"], "bandwidth": int(best.get("bandwidth") or 4_000_000), "duration": float(info.get("duration") or 0), "license": lic,
            "license_url": meta.get("LicenseUrl", {}).get("value", ""), "author": author or "неизвестен",
            "source_url": info["descriptionurl"]}


def _download_head(url: str, n_bytes: int, dst: Path, attempts: int = 6) -> None:
    for i in range(attempts):
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Range": f"bytes=0-{n_bytes - 1}"})
        try:
            with urllib.request.urlopen(req, timeout=900) as r, open(dst, "wb") as f:
                while chunk := r.read(1 << 16):
                    f.write(chunk)
            return
        except urllib.error.HTTPError as e:
            if e.code != 429 or i == attempts - 1:
                raise
            time.sleep(60 * (i + 1))


def cut(cid: str, ffmpeg: str, tmp: Path) -> dict:
    title, start, place, *rest = SOURCES[cid]
    d = describe(title)
    length = min(rest[0] if rest else LENGTH_S, d["duration"] - start - 0.2)
    head = tmp / f"{cid}.webm"
    _download_head(d["src"], int(d["bandwidth"] / 8 * (start + length) * 1.4) + 300_000, head)
    out = OUT_DIR / f"{cid}.mp4"
    subprocess.run([ffmpeg, "-y", "-loglevel", "error", "-ss", str(start), "-i", str(head), "-t", f"{length:.2f}", "-an",
                    "-vf", VF, "-c:v", "libx264", "-preset", "slow", "-crf", "26", "-movflags", "+faststart", str(out)],
                   check=True, timeout=600)
    got = float(subprocess.run([ffmpeg, "-i", str(out)], capture_output=True, text=True).stderr
                .split("Duration: ")[1][:11].replace(":", " ").split()[2])
    if got < length - 1:
        raise RuntimeError(f"{cid}: получилось {got:.1f} с вместо {length:.1f} — увеличьте запас байтов")
    print(f"{cid:10} {out.stat().st_size / 1e6:.1f} МБ  {got:.1f} с  {d['license']:14} {d['author'][:40]}", flush=True)
    note = f"фрагмент {start}–{start + length:.0f} с, без звука" + (f"; в кадре: {place}" if place else "")
    return {"author": d["author"], "license": d["license"], "license_url": d["license_url"],
            "source_url": d["source_url"], "title": title.removeprefix("File:"), "note": note}


def main():
    import imageio_ffmpeg
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    todo = sys.argv[1:] or list(SOURCES)
    unknown = [c for c in todo if c not in SOURCES]
    if unknown:
        sys.exit(f"нет в SOURCES: {', '.join(unknown)}")
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    credits = json.loads(CREDITS.read_text(encoding="utf-8")) if CREDITS.exists() else {}
    videos = credits.get("videos", {})
    results, failed = {}, []
    with tempfile.TemporaryDirectory() as tmp:
        for cid in todo:
            try:
                results[cid] = cut(cid, ffmpeg, Path(tmp))
            except Exception as e:
                failed.append(cid)
                print(f"{cid:10} ОШИБКА: {e}", flush=True)
            time.sleep(3)
    videos.update(results)
    CREDITS.write_text(json.dumps({
        "about": "Клипы направлений из Wikimedia Commons под свободными лицензиями. Сгенерировано scripts/fetch_videos.py.",
        "updated_at": time.strftime("%Y-%m-%d"),
        "videos": dict(sorted(videos.items())),
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nГотово: {len(results)} клипов, подписи — {CREDITS}" + (f"; не вышло: {', '.join(failed)}" if failed else ""))


if __name__ == "__main__":
    main()
