"""HTTP API. Каждый ответ агента — AgentReply: {text, cards, deck_kind, quick_replies, steps, changes, ui_action, state}.

Все ручки, кроме health/privacy/catalog/auth, требуют Authorization: Bearer <token> (security/auth.py).
Сессии и брони видит только их владелец.

Сервер асинхронный: пока агент ждёт LLM, поток и соединение с базой никто не держит.
"""
import asyncio
import hmac
import logging
import random
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Awaitable, Callable, Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import analytics, config, db
from .agent import get_agent
from .catalog import CITIES, CLIMATE_META, ORIGINS, VIDEOS_DIR, media_urls
from .models import ActionRequest, AgentReply, ChatRequest, CheckoutRequest, FeedbackRequest, TelegramAuthRequest
from .security import ratelimit
from .security.auth import (Identity, TelegramAuthError, current_user, issue_token, new_anonymous_id,
                            verify_telegram_init_data)
from .security.crypto import pseudonymize
from .security.http import SecurityMiddleware
from .state_migrations import StateTooNew
from .store import (SESSION_TTL_DAYS, Consent, Session, SessionConflict, SessionNotFound, all_metrics, booking_history,
                    bookings_stats, erase_user_data, export_user_data, funnel, load_session, new_session,
                    recent_messages, run_maintenance, save_session)
from .validator import build_view

log = logging.getLogger(__name__)
MAINTENANCE_CHECK_S = 300  # как часто экземпляр проверяет, не пора ли обслуживание (запустит только один)
SAVE_ATTEMPTS = 3


@asynccontextmanager
async def lifespan(_: FastAPI):
    config.validate()  # в продакшне без секретов не стартуем
    await db.pool()  # подключение + миграции при старте, а не на первом запросе

    async def maintenance_loop():
        while True:
            try:
                await run_maintenance()
            except Exception:
                log.exception("Обслуживание не удалось")
            await asyncio.sleep(MAINTENANCE_CHECK_S + random.uniform(0, 30))

    task = asyncio.create_task(maintenance_loop())
    yield
    task.cancel()
    await ratelimit.close()
    await db.close()


app = FastAPI(title="О!дин запрос — API", version="0.4.0", lifespan=lifespan,
              docs_url=None if config.PROD else "/docs", redoc_url=None,
              openapi_url=None if config.PROD else "/openapi.json")
app.add_middleware(SecurityMiddleware)
app.add_middleware(CORSMiddleware, allow_origins=config.CORS_ORIGINS, allow_credentials=False,
                   allow_methods=["GET", "POST", "DELETE"], allow_headers=["Authorization", "Content-Type"])
agent = get_agent()

NOT_FOUND = "Сессия не найдена — создайте новую через POST /api/session"


async def _handle(user: Identity, session_id: str, user_text: Optional[str], event: str,
                  fn: Callable[[Session], Awaitable[AgentReply]], **track) -> AgentReply:
    """Прочитать сессию → агент без открытой транзакции → сохранить с проверкой версии.

    Если параллельный запрос успел изменить сессию (двойной свайп, двойной клик «Оплатить»), сохранение
    отклоняется, и запрос повторяется на свежем состоянии — так ни одно изменение не теряется.
    """
    for attempt in range(SAVE_ATTEMPTS):
        try:
            sess = await load_session(user.user_id, session_id)
        except SessionNotFound:
            raise HTTPException(404, NOT_FOUND)
        except StateTooNew:
            # сессию записала более новая версия кода (откат деплоя) — не трогаем её, чтобы не потерять данные
            raise HTTPException(409, "Сессия сохранена более новой версией сервиса")
        stage_before = sess.state.stage
        if user_text:
            sess.log("user", user_text)
        reply = await fn(sess)
        sess.log("agent", reply.text)
        analytics.track_reply(sess, event, stage_before, reply, **track)
        try:
            await save_session(sess)
            return reply
        except SessionConflict:
            await asyncio.sleep(random.uniform(0.01, 0.05) * (attempt + 1))
    raise HTTPException(409, "Сессию одновременно меняют несколько запросов — повторите действие")


async def agent_user(user: Identity = Depends(current_user)) -> Identity:
    """Пользователь + лимит на обращения к агенту (с LLM каждое обращение стоит денег)."""
    await ratelimit.limit_agent_call(user)
    return user


# ---------------------------------------------------------------- публичное

@app.get("/api/health")
async def health():
    return {"ok": True, "agent": agent.name, "videos": sum(1 for c in CITIES.values() if media_urls(c)["video_url"]),
            "climate": {"source": CLIMATE_META.get("source", "примерные значения из cities.json"),
                        "cities_from_open_meteo": sum(1 for c in CITIES.values() if c.get("climate_source") == "open-meteo")}}


@app.get("/api/privacy")
async def privacy():
    return {"policy_version": config.POLICY_VERSION, "policy_url": "/privacy.html",
            "retention": {"sessions_days": SESSION_TTL_DAYS,
                          "booking_contacts_days_after_checkout": config.BOOKING_PII_RETENTION_DAYS}}


@app.get("/api/catalog")
async def catalog():
    return {"origins": {k: v["name"] for k, v in ORIGINS.items()},
            "destinations": [{"id": c["id"], "name": c["name"], "country": c["country"], "flag": c["flag"],
                              **media_urls(c)} for c in CITIES.values()]}


# ---------------------------------------------------------------- вход

def _token_reply(user_id: str, kind: str) -> dict:
    return {"token": issue_token(user_id, kind), "user_id": user_id, "kind": kind,
            "policy_version": config.POLICY_VERSION}


@app.post("/api/auth/anonymous")
async def auth_anonymous(request: Request):
    await ratelimit.limit_auth(request)
    return _token_reply(new_anonymous_id(), "anon")


@app.post("/api/auth/telegram")
async def auth_telegram(req: TelegramAuthRequest, request: Request):
    await ratelimit.limit_auth(request)
    try:
        tg_user = verify_telegram_init_data(req.init_data, config.TELEGRAM_BOT_TOKEN, config.TELEGRAM_AUTH_MAX_AGE_S)
    except TelegramAuthError as e:
        raise HTTPException(401, f"Вход через Telegram не прошёл проверку: {e}")
    return _token_reply(f"tg-{int(tg_user['id'])}", "telegram")


# ---------------------------------------------------------------- агент

@app.post("/api/session")
async def create_session(user: Identity = Depends(agent_user)):
    await ratelimit.limit_new_session(user)
    sess = new_session(user.user_id)
    reply = await agent.start(sess)
    sess.log("agent", reply.text)
    analytics.track_reply(sess, "session_started", "start", reply)
    await save_session(sess)
    return {"session_id": sess.id, "reply": reply}


@app.get("/api/session/{session_id}")
async def session_state(session_id: str, user: Identity = Depends(current_user)):
    try:
        sess = await load_session(user.user_id, session_id)
    except SessionNotFound:
        raise HTTPException(404, NOT_FOUND)
    return {"session_id": sess.id, "history": await recent_messages(sess.id, user.user_id), "deck": sess.deck,
            "state": build_view(sess.state)}


@app.post("/api/chat", response_model=AgentReply)
async def chat(req: ChatRequest, user: Identity = Depends(agent_user)):
    return await _handle(user, req.session_id, req.message, "message", lambda sess: agent.message(sess, req.message))


@app.post("/api/feedback", response_model=AgentReply)
async def feedback(req: FeedbackRequest, user: Identity = Depends(agent_user)):
    label = f"[{req.action} {req.kind} {req.card_id}{' · ' + req.reason if req.reason else ''}]"
    return await _handle(user, req.session_id, label, "feedback", lambda sess: agent.feedback(sess, req), req=req)


@app.post("/api/action", response_model=AgentReply)
async def action(req: ActionRequest, user: Identity = Depends(agent_user)):
    return await _handle(user, req.session_id, f"[action {req.type}]", "action", lambda sess: agent.action(sess, req),
                         action_type=req.type)


@app.post("/api/checkout", response_model=AgentReply)
async def checkout(req: CheckoutRequest, request: Request, user: Identity = Depends(agent_user)):
    if not req.consent or req.policy_version != config.POLICY_VERSION:
        raise HTTPException(400, "Для оформления нужно согласие на обработку персональных данных "
                                 f"по текущей политике (версия {config.POLICY_VERSION})")
    ip_hash = pseudonymize(ratelimit.client_ip(request))

    async def run(sess: Session) -> AgentReply:
        sess.pending_consents.append(Consent(purpose="booking", policy_version=req.policy_version, granted=True,
                                             ip_hash=ip_hash))
        return await agent.checkout(sess, req)
    return await _handle(user, req.session_id, None, "checkout", run)


@app.get("/api/bookings/{booking_id}")
async def booking(booking_id: str, user: Identity = Depends(current_user)):
    """Своя бронь со всей историей изменений."""
    b = await booking_history(booking_id, user.user_id)
    if not b:
        raise HTTPException(404, "Бронь не найдена")
    return b


# ---------------------------------------------------------------- мои данные (152-ФЗ)

@app.get("/api/me/export")
async def my_data(user: Identity = Depends(current_user)):
    """Всё, что сервис хранит о пользователе."""
    await ratelimit.hit(f"export:{user.user_id}", 10, 3600)
    return await export_user_data(user.user_id)


@app.delete("/api/me")
async def delete_my_data(user: Identity = Depends(current_user)):
    """Удалить данные пользователя (брони остаются обезличенной записью о сделке)."""
    return {"deleted": True, **(await erase_user_data(user.user_id))}


# ---------------------------------------------------------------- служебное

async def admin(x_admin_token: Optional[str] = Header(default=None)) -> None:
    if not config.ADMIN_TOKEN:
        if config.PROD:
            raise HTTPException(403, "Закрыто")
        return  # в разработке открыто
    if not x_admin_token or not hmac.compare_digest(x_admin_token, config.ADMIN_TOKEN):
        raise HTTPException(403, "Нужен X-Admin-Token")


@app.get("/api/metrics", dependencies=[Depends(admin)])
async def metrics():
    """Метрики качества по всем сессиям в базе (только агрегаты, без персональных данных)."""
    ms = [m for m in await all_metrics() if m.turns]
    ttfr = [m.first_reco_at - m.started_at for m in ms if m.first_reco_at]
    n = len(ms) or 1
    return {
        "sessions": len(ms),
        "bookings": await bookings_stats(),
        "booked_rate": round(sum(m.booked for m in ms) / n, 2),
        "avg_clarifications": round(sum(m.clarifications for m in ms) / n, 2),
        "avg_time_to_first_reco_s": round(sum(ttfr) / len(ttfr), 2) if ttfr else None,
        "avg_replans": round(sum(m.replans for m in ms) / n, 2),
        "connection_errors_seen": sum(m.connection_errors_seen for m in ms),
    }


@app.get("/api/analytics/funnel", dependencies=[Depends(admin)])
async def analytics_funnel(days: int = 30):
    """Воронка, причины дизлайков и популярность направлений по обезличенным событиям."""
    return await funnel(max(1, min(days, 365)))


VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/videos", StaticFiles(directory=VIDEOS_DIR), name="videos")

# собранный фронт (npm run build) раздаём с того же сервера — для демо и Telegram Mini App
DIST = (Path(__file__).resolve().parents[2] / "frontend" / "dist").resolve()
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    async def spa(path: str):
        f = (DIST / path).resolve()
        if path and f.is_file() and f.is_relative_to(DIST):  # без выхода за пределы dist (../)
            return FileResponse(f)
        return FileResponse(DIST / "index.html")
