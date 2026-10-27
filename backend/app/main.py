"""HTTP API. Каждый ответ — AgentReply: {text, cards, deck_kind, quick_replies, steps, changes, ui_action, state}."""
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .agent import get_agent
from .catalog import CITIES, ORIGINS, VIDEOS_DIR, media_urls
from .models import (ActionRequest, AgentReply, ChatRequest, CheckoutRequest, FeedbackRequest, NewSessionRequest)
from .store import SESSIONS, Session, get_session, new_session
from .validator import build_view

app = FastAPI(title="О!дин запрос — API", version="0.1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
agent = get_agent()


def _session(session_id: str) -> Session:
    s = get_session(session_id)
    if not s:
        raise HTTPException(404, "Сессия не найдена — создайте новую через POST /api/session")
    return s


def _log(sess: Session, role: str, text: str):
    sess.history.append({"role": role, "text": text, "at": time.time()})


@app.get("/api/health")
def health():
    return {"ok": True, "agent": agent.name, "videos": sum(1 for c in CITIES.values() if media_urls(c)["video_url"])}


@app.post("/api/session")
def create_session(req: NewSessionRequest):
    sess = new_session(req.user_id)
    reply = agent.start(sess)
    _log(sess, "agent", reply.text)
    return {"session_id": sess.id, "reply": reply}


@app.get("/api/session/{session_id}")
def session_state(session_id: str):
    sess = _session(session_id)
    return {"session_id": sess.id, "history": sess.history, "deck": sess.deck, "state": build_view(sess.state)}


@app.post("/api/chat", response_model=AgentReply)
def chat(req: ChatRequest):
    sess = _session(req.session_id)
    _log(sess, "user", req.message)
    reply = agent.message(sess, req.message)
    _log(sess, "agent", reply.text)
    return reply


@app.post("/api/feedback", response_model=AgentReply)
def feedback(req: FeedbackRequest):
    sess = _session(req.session_id)
    _log(sess, "user", f"[{req.action} {req.kind} {req.card_id}{' · ' + req.reason if req.reason else ''}]")
    reply = agent.feedback(sess, req)
    _log(sess, "agent", reply.text)
    return reply


@app.post("/api/action", response_model=AgentReply)
def action(req: ActionRequest):
    sess = _session(req.session_id)
    _log(sess, "user", f"[action {req.type} {req.payload}]")
    reply = agent.action(sess, req)
    _log(sess, "agent", reply.text)
    return reply


@app.post("/api/checkout", response_model=AgentReply)
def checkout(req: CheckoutRequest):
    sess = _session(req.session_id)
    reply = agent.checkout(sess, req)
    _log(sess, "agent", reply.text)
    return reply


@app.get("/api/catalog")
def catalog():
    return {"origins": {k: v["name"] for k, v in ORIGINS.items()},
            "destinations": [{"id": c["id"], "name": c["name"], "country": c["country"], "flag": c["flag"],
                              **media_urls(c)} for c in CITIES.values()]}


@app.get("/api/metrics")
def metrics():
    """Метрики качества по всем сессиям с момента запуска."""
    ss = [s for s in SESSIONS.values() if s.metrics.turns]
    ttfr = [s.metrics.first_reco_at - s.metrics.started_at for s in ss if s.metrics.first_reco_at]
    n = len(ss) or 1
    return {
        "sessions": len(ss),
        "booked_rate": round(sum(s.metrics.booked for s in ss) / n, 2),
        "avg_clarifications": round(sum(s.metrics.clarifications for s in ss) / n, 2),
        "avg_time_to_first_reco_s": round(sum(ttfr) / len(ttfr), 2) if ttfr else None,
        "avg_replans": round(sum(s.metrics.replans for s in ss) / n, 2),
        "connection_errors_seen": sum(s.metrics.connection_errors_seen for s in ss),
    }


VIDEOS_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/videos", StaticFiles(directory=VIDEOS_DIR), name="videos")

# собранный фронт (npm run build) раздаём с того же сервера — удобно для демо и Telegram Mini App
DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        f = DIST / path
        return FileResponse(f if path and f.is_file() else DIST / "index.html")
