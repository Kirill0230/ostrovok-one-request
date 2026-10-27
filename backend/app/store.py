"""Хранилище сессий (в памяти) и профилей пользователей (JSON-файл — «память между сессиями»)."""
import json
import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .catalog import DATA_DIR
from .models import Card, TripState

PROFILES_PATH = DATA_DIR / "profiles.json"
_lock = threading.Lock()


@dataclass
class Metrics:
    started_at: float = field(default_factory=time.time)
    first_reco_at: Optional[float] = None
    clarifications: int = 0
    turns: int = 0
    replans: int = 0
    dislikes: int = 0
    connection_errors_seen: int = 0
    booked: bool = False


@dataclass
class Session:
    id: str
    user_id: Optional[str]
    state: TripState = field(default_factory=TripState)
    history: List[Dict[str, str]] = field(default_factory=list)
    deck: List[Card] = field(default_factory=list)
    metrics: Metrics = field(default_factory=Metrics)


SESSIONS: Dict[str, Session] = {}


def new_session(user_id: Optional[str]) -> Session:
    s = Session(id=uuid.uuid4().hex[:12], user_id=user_id)
    SESSIONS[s.id] = s
    return s


def get_session(session_id: str) -> Optional[Session]:
    return SESSIONS.get(session_id)


def load_profile(user_id: Optional[str]) -> Optional[dict]:
    if not user_id or not PROFILES_PATH.exists():
        return None
    try:
        return json.loads(PROFILES_PATH.read_text(encoding="utf-8")).get(user_id)
    except (OSError, json.JSONDecodeError):
        return None


def save_profile(user_id: Optional[str], state: TripState) -> None:
    if not user_id:
        return
    with _lock:
        data = {}
        if PROFILES_PATH.exists():
            try:
                data = json.loads(PROFILES_PATH.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                data = {}
        prev = data.get(user_id, {})
        trips = prev.get("trips", [])
        if state.booking and state.destination_id and state.booking.id not in [t.get("booking") for t in trips]:
            trips.append({"booking": state.booking.id, "destination": state.destination_id})
        data[user_id] = {
            "origin": state.origin,
            "travellers": state.travellers.model_dump(),
            "priorities": state.priorities.model_dump(),
            "trip_types": state.prefs.trip_types,
            "trips": trips[-10:],
        }
        PROFILES_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
