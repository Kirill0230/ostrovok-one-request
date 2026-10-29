import json
import os
import sys
import time
import uuid
from pathlib import Path

os.environ.setdefault("DB_SCHEMA", f"eval_{uuid.uuid4().hex[:8]}")
os.environ.setdefault("RATE_LIMIT_ENABLED", "0")

from fastapi.testclient import TestClient

from app import db
from app.main import app

QUERIES = json.loads((Path(__file__).parent / "queries.json").read_text(encoding="utf-8"))
DEFAULT_ANSWER = "море в ноябре вдвоём"


def run_one(c: TestClient, q: str) -> dict:
    auth = c.post("/api/auth/anonymous").json()
    c.headers["Authorization"] = f"Bearer {auth['token']}"
    sid = c.post("/api/session").json()["session_id"]
    t0 = time.perf_counter()
    r = c.post("/api/chat", json={"session_id": sid, "message": q}).json()
    clarifications = 0
    if not r["cards"] and r["state"]["trip"]["stage"] == "start":
        clarifications += 1
        r = c.post("/api/chat", json={"session_id": sid, "message": DEFAULT_ANSWER}).json()
    ttfr = time.perf_counter() - t0 if r["cards"] else None
    first = [cd["title"] for cd in r["cards"]]
    steps = 0
    while r["cards"] and steps < 6:
        card = r["cards"][0]
        r = c.post("/api/feedback", json={"session_id": sid, "card_id": card["id"], "kind": card["kind"], "action": "like"}).json()
        steps += 1
    reached_cart = r["state"]["trip"]["stage"] == "review"
    errors_before = [i["message"] for i in r["state"]["issues"] if i["severity"] == "error"]
    r2 = c.post("/api/chat", json={"session_id": sid, "message": "а если на 2 дня позже?"}).json()
    replan_ok = r2["state"]["trip"]["stage"] == "review" and bool(r2["changes"])
    errors_after = [i["message"] for i in r2["state"]["issues"] if i["severity"] == "error"]
    booked = False
    if r2["state"]["can_checkout"]:
        r3 = c.post("/api/checkout", json={"session_id": sid, "consent": True,
                                            "policy_version": auth["policy_version"]}).json()
        booked = r3["state"]["trip"]["stage"] == "booked"
    budget = r2["state"]["trip"]["budget"]
    return {
        "query": q, "first": first, "clarifications": clarifications, "ttfr_ms": round(ttfr * 1000) if ttfr else None,
        "reached_cart": reached_cart, "replan_ok": replan_ok, "booked": booked,
        "connection_errors": len(errors_before) + len(errors_after), "errors": errors_before + errors_after,
        "destination": r2["state"]["destination_name"], "total": r2["state"]["total"],
        "within_budget": (r2["state"]["total"] <= budget) if budget else None,
    }


def main():
    verbose = "--verbose" in sys.argv
    try:
        with TestClient(app) as c:
            rows = [run_one(c, q) for q in QUERIES]
    finally:
        db.drop_schema()
    n = len(rows)
    for r in rows:
        mark = "✅" if r["booked"] else "❌"
        print(f"{mark} {r['query'][:60]:60} → {r['destination'] or '—':18} {r['total']:>9,} ₽"
              f"  уточн:{r['clarifications']}  ошибок стыковки:{r['connection_errors']}"
              + (f"  бюджет:{'ок' if r['within_budget'] else 'превышен'}" if r["within_budget"] is not None else ""))
        if verbose:
            print("    первые варианты:", ", ".join(r["first"]))
            for e in r["errors"]:
                print("    ошибка:", e)
    ttfr = [r["ttfr_ms"] for r in rows if r["ttfr_ms"] is not None]
    budgeted = [r for r in rows if r["within_budget"] is not None]
    print("\n=== Метрики")
    print(f"запросов: {n}")
    print(f"доведено до оплаты: {sum(r['booked'] for r in rows)}/{n} ({100 * sum(r['booked'] for r in rows) // n}%)")
    print(f"дошли до корзины: {sum(r['reached_cart'] for r in rows)}/{n}")
    print(f"пересборка «на 2 дня позже» сработала: {sum(r['replan_ok'] for r in rows)}/{n}")
    print(f"среднее число уточнений: {sum(r['clarifications'] for r in rows) / n:.2f}")
    print(f"время до первой рекомендации (без LLM): {sum(ttfr) / len(ttfr):.0f} мс" if ttfr else "нет рекомендаций")
    print(f"ошибок в стыковках: {sum(r['connection_errors'] for r in rows)}")
    if budgeted:
        print(f"уложились в бюджет: {sum(r['within_budget'] for r in budgeted)}/{len(budgeted)}")


if __name__ == "__main__":
    main()
