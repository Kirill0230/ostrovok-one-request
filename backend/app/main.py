"""HTTP API. Каждый ответ агента — AgentReply: {text, cards, deck_kind, quick_replies, steps, changes, ui_action, state}.

Все ручки, кроме health/privacy/catalog/auth, требуют Authorization: Bearer <token> (security/auth.py).
Сессии и брони видит только их владелец.
"""
import hmac
import logging
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Callable, Optional

import psycopg
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import config, db
from .agent import get_agent
from .catalog import CITIES, ORIGINS, VIDEOS_DIR, media_urls
from .models import ActionRequest, AgentReply, ChatRequest, CheckoutRequest, FeedbackRequest, TelegramAuthRequest
from .security import ratelimit
from .security.auth import (Identity, TelegramAuthError, current_user, issue_token, new_anonymous_id,
                            verify_telegram_init_data)
from .security.crypto import pseudonymize
from .security.http import SecurityMiddleware
from .state_migrations import StateTooNew
from .store import (SESSION_TTL_DAYS, Consent, Session, SessionNotFound, all_metrics, booking_history, bookings_stats,
                    encrypt_legacy_rows, erase_user_data, export_user_data, purge_expired, recent_messages,
                    session_scope)
from .validator import build_view

log = logging.getLogger(__name__)
PURGE_EVERY_S = 3600


@asynccontextmanager
async def lifespan(_: FastAPI):
    config.validate()  # в продакшне без секретов не стартуем
    db.pool()  # подключение + миграции при старте, а не на первом запросе
    encrypt_legacy_rows()
    stop = threading.Event()

    def purge_loop():
        while not stop.is_set():
            try:
                purge_expired()
            except Exception:
                log.exception("Фоновая очистка не удалась")
            stop.wait(PURGE_EVERY_S)

    threading.Thread(target=purge_loop, daemon=True, name="purge").start()
    yield
    stop.set()
    db.close()


app = FastAPI(title="О!дин запрос — API", version="0.3.0", lifespan=lifespan,
              docs_url=None if config.PROD else "/docs", redoc_url=None,
              openapi_url=None if config.PROD else "/openapi.json")
app.add_middleware(SecurityMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_credentials=False,
                   allow_methods=["GET", "POST", "DELETE"], allow_headers=["Authorization", "Content-Type"])
agent = get_agent()

NOT_FOUND = "Сессия не найдена — создайте новую через POST /api/session"


def _handle(user: Identity, session_id: str, user_text: Optional[str],
            fn: Callable[[Session], AgentReply]) -> AgentReply:
    """Сессия владельца под блокировкой → агент → сохранение одной транзакцией."""
    try:
        with session_scope(user.user_id, session_id) as sess:
            if user_text:
                sess.log("user", user_text)
            reply = fn(sess)
            sess.log("agent", reply.text)
        return reply
    except SessionNotFound:
        raise HTTPException(404, NOT_FOUND)
    except psycopg.errors.LockNotAvailable:
        raise HTTPException(409, "Предыдущий запрос по этой сессии ещё выполняется — повторите через секунду")
    except StateTooNew:
        # сессию записала более новая версия кода (откат деплоя) — не трогаем её, чтобы не потерять данные
        raise HTTPException(409, "Сессия сохранена более новой версией сервиса")


def agent_user(user: Identity = Depends(current_user)) -> Identity:
    """Пользователь + лимит на обращения к агенту (с LLM каждое обращение стоит денег)."""
    ratelimit.limit_agent_call(user)
    return user


# ---------------------------------------------------------------- публичное

@app.get("/api/health")
def health():
    return {"ok": True, "agent": agent.name, "videos": sum(1 for c in CITIES.values() if media_urls(c)["video_url"])}


@app.get("/api/privacy")
def privacy():
    return {"policy_version": config.POLICY_VERSION, "policy_url": "/privacy.html",
            "retention": {"sessions_days": SESSION_TTL_DAYS,
                          "booking_contacts_days_after_checkout": config.BOOKING_PII_RETENTION_DAYS}}


@app.get("/api/catalog")
def catalog():
    return {"origins": {k: v["name"] for k, v in ORIGINS.items()},
            "destinations": [{"id": c["id"], "name": c["name"], "country": c["country"], "flag": c["flag"],
                              **media_urls(c)} for c in CITIES.values()]}


# ---------------------------------------------------------------- вход

def _token_reply(user_id: str, kind: str) -> dict:
    return {"token": issue_token(user_id, kind), "user_id": user_id, "kind": kind,
            "policy_version": config.POLICY_VERSION}


@app.post("/api/auth/anonymous")
def auth_anonymous(request: Request):
    ratelimit.limit_auth(request)
    return _token_reply(new_anonymous_id(), "anon")


@app.post("/api/auth/telegram")
def auth_telegram(req: TelegramAuthRequest, request: Request):
    ratelimit.limit_auth(request)
    try:
        tg_user = verify_telegram_init_data(req.init_data, config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_AUTH_MAX_AGE_S)
    except TelegramAuthError as e:
        raise HTTPException(401, f"Вход через Telegram не прошёл проверку: {e}")
    return _token_reply(f"tg-{int(tg_user['id'])}", "telegram")


# ---------------------------------------------------------------- агент

@app.post("/api/session")
def create_session(user: Identity = Depends(agent_user)):
    ratelimit.limit_new_session(user)
    with session_scope(user.user_id) as sess:
        reply = agent.start(sess)
        sess.log("agent", reply.text)
    return {"session_id": sess.id, "reply": reply}


@app.get("/api/session/{session_id}")
def session_state(session_id: str, user: Identity = Depends(current_user)):
    try:
        with session_scope(user.user_id, session_id) as sess:
            pass
    except SessionNotFound:
        raise HTTPException(404, NOT_FOUND)
    return {"session_id": sess.id, "history": recent_messages(sess.id, user.user_id), "deck": sess.deck,
            "state": build_view(sess.state)}


@app.post("/api/chat", response_model=AgentReply)
def chat(req: ChatRequest, user: Identity = Depends(agent_user)):
    return _handle(user, req.session_id, req.message, lambda sess: agent.message(sess, req.message))


@app.post("/api/feedback", response_model=AgentReply)
def feedback(req: FeedbackRequest, user: Identity = Depends(agent_user)):
    label = f"[{req.action} {req.kind} {req.card_id}{' · ' + req.reason if req.reason else ''}]"
    return _handle(user, req.session_id, label, lambda sess: agent.feedback(sess, req))


@app.post("/api/action", response_model=AgentReply)
def action(req: ActionRequest, user: Identity = Depends(agent_user)):
    return _handle(user, req.session_id, f"[action {req.type}]", lambda sess: agent.action(sess, req))


@app.post("/api/checkout", response_model=AgentReply)
def checkout(req: CheckoutRequest, request: Request, user: Identity = Depends(agent_user)):
    if not req.consent or req.policy_version != config.POLICY_VERSION:
        raise HTTPException(400, "Для оформления нужно согласие на обработку персональных данных "
                                 f"по текущей политике (версия {config.POLICY_VERSION})")

    def run(sess: Session) -> AgentReply:
        sess.pending_consents.append(Consent(purpose="booking", policy_version=req.policy_version, granted=True,
                                             ip_hash=pseudonymize(ratelimit.client_ip(request))))
        return agent.checkout(sess, req)
    return _handle(user, req.session_id, None, run)


@app.get("/api/bookings/{booking_id}")
def booking(booking_id: str, user: Identity = Depends(current_user)):
    """Своя бронь со всей историей изменений."""
    b = booking_history(booking_id, user.user_id)
    if not b:
        raise HTTPException(404, "Бронь не найдена")
    return b


# ---------------------------------------------------------------- мои данные (152-ФЗ)

@app.get("/api/me/export")
def my_data(user: Identity = Depends(current_user)):
    """Всё, что сервис хранит о пользователе."""
    ratelimit.hit(f"export:{user.user_id}", 10, 3600)
    return export_user_data(user.user_id)


@app.delete("/api/me")
def delete_my_data(user: Identity = Depends(current_user)):
    """Удалить данные пользователя (брони остаются обезличенной записью о сделке)."""
    return {"deleted": True, **erase_user_data(user.user_id)}


# ---------------------------------------------------------------- служебное

def admin(x_admin_token: Optional[str] = Header(default=None)) -> None:
    if not config.ADMIN_TOKEN:
        if config.PROD:
            raise HTTPException(403, "Закрыто")
        return  # в разработке открыто
    if not x_admin_token or not hmac.compare_digest(x_admin_token, config.ADMIN_TOKEN):
        raise HTTPException(403, "Нужен X-Admin-Token")


@app.get("/api/metrics", dependencies=[Depends(admin)])
def metrics():
    """Метрики качества по всем сессиям в базе (только агрегаты, без персональных данных)."""
    ms = [m for m in all_metrics() if m.turns]
    ttfr = [m.first_reco_at - m.started_at for m in ms if m.first_reco_at]
    n = len(ms) or 1
    return {
        "sessions": len(ms),
        "bookings": bookings_stats(),
        "booked_rate": round(sum(m.booked for m in ms) / n, 2),
        "avg_clarifications": round(sum(m.clarifications for m in ms) / n, 2),
        "avg_time_to_first_reco_s": round(sum(ttfr) / len(ttfr), 2) if ttfr else None,
        "avg_replans": round(sum(m.replans for m in ms) / n, 2),
        "connection_errors_seen": sum(m.connection_errors_seen for m in ms),
    }


VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/videos", StaticFiles(directory=VIDEOS_DIR), name="videos")

# собранный фронт (npm run build) раздаём с того же сервера — для демо и Telegram Mini App
DIST = (Path(__file__).resolve().parents[2] / "frontend" / "dist").resolve()
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        f = (DIST / path).resolve()
        if path and f.is_file() and f.is_relative_to(DIST):  # без выхода за пределы dist (../)
            return FileResponse(f)
        return FileResponse(DIST / "index.html")
