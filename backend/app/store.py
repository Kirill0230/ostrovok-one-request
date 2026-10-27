"""Хранилище на Postgres: сессии, история чата, профили, брони, согласия.

Главные правила:
  • Любой запрос к сессии идёт через session_scope(session_id, user_id). Строка сессии блокируется
    (SELECT … FOR UPDATE) до конца запроса, и всё изменённое сохраняется одной транзакцией.
  • Сессию видит только её владелец: чужой session_id ведёт себя как несуществующий.
  • Персональные данные лежат только в зашифрованных колонках *_enc (security/crypto.py).

Агент в базу напрямую не пишет: он кладёт намерения в сессию (pending_*, profile_dirty),
а store сохраняет их при выходе из session_scope.
"""
import logging
import os
import secrets
import string
import time
import uuid
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from typing import Dict, Iterator, List, Optional

import psycopg
from psycopg.types.json import Jsonb

from . import config, db
from .models import STATE_VERSION, Card, TripState
from .security.crypto import decrypt_json, decrypt_str, encrypt_json, encrypt_str, pseudonymize
from .state_migrations import StateTooNew, upgrade

log = logging.getLogger(__name__)

SESSION_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "30"))
LOCK_TIMEOUT = os.getenv("SESSION_LOCK_TIMEOUT", "15s")


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
class BookingEvent:
    type: str  # created | changed | cancelled
    amount_delta: int
    description: str


@dataclass
class Consent:
    purpose: str
    policy_version: str
    granted: bool
    ip_hash: Optional[str] = None


@dataclass
class Session:
    id: str
    user_id: str
    state: TripState = field(default_factory=TripState)
    deck: List[Card] = field(default_factory=list)
    metrics: Metrics = field(default_factory=Metrics)
    # что сохранить при выходе из session_scope
    pending_messages: List[Dict[str, str]] = field(default_factory=list)
    pending_booking_events: List[BookingEvent] = field(default_factory=list)
    pending_contact: Optional[Dict[str, str]] = None  # контакты для брони — только в bookings, не в состоянии
    pending_consents: List[Consent] = field(default_factory=list)
    profile_dirty: bool = False

    def log(self, role: str, text: str) -> None:
        self.pending_messages.append({"role": role, "text": text})


class SessionNotFound(Exception):
    pass


def _metrics(raw: dict) -> Metrics:
    return Metrics(**{k: v for k, v in (raw or {}).items() if k in Metrics.__dataclass_fields__})


# ---------------------------------------------------------------- сессии

@contextmanager
def session_scope(user_id: str, session_id: Optional[str] = None) -> Iterator[Session]:
    """Открыть сессию пользователя под блокировкой (или создать новую) и сохранить при выходе."""
    with db.tx() as conn:
        conn.execute(f"SET LOCAL lock_timeout = '{LOCK_TIMEOUT}'")
        if session_id is None:
            sess = Session(id=uuid.uuid4().hex, user_id=user_id)
            _insert_session(conn, sess)
        else:
            row = conn.execute(
                "SELECT * FROM sessions WHERE id = %s AND user_id = %s AND expires_at > now() FOR UPDATE",
                (session_id, user_id)).fetchone()
            if not row:
                raise SessionNotFound(session_id)
            sess = _from_row(row)
        yield sess
        _save(conn, sess)


def _from_row(row: dict) -> Session:
    try:
        raw = decrypt_json(row["state_enc"]) if row["state_enc"] else row["state"]
        state = TripState.model_validate(upgrade(raw, row["state_version"]))
    except StateTooNew:
        raise
    except Exception:
        # состояние нечитаемо даже после миграций — считаем сессию истёкшей, фронт начнёт новую
        log.exception("Не удалось прочитать состояние сессии %s", row["id"])
        raise SessionNotFound(row["id"])
    try:
        deck = [Card.model_validate(c) for c in row["deck"]]
    except Exception:
        deck = []  # колода — временные данные, при несовместимости просто пересоберётся
    return Session(id=row["id"], user_id=row["user_id"], state=state, deck=deck, metrics=_metrics(row["metrics"]))


def _insert_session(conn: psycopg.Connection, sess: Session) -> None:
    conn.execute(
        """INSERT INTO sessions (id, user_id, state_enc, state_version, deck, metrics, expires_at)
           VALUES (%s, %s, %s, %s, '[]', %s, now() + make_interval(days => %s))""",
        (sess.id, sess.user_id, encrypt_json(sess.state.model_dump(mode="json")), STATE_VERSION,
         Jsonb(asdict(sess.metrics)), SESSION_TTL_DAYS))


def _save(conn: psycopg.Connection, sess: Session) -> None:
    if sess.pending_booking_events:
        _save_booking(conn, sess)  # первым: при коллизии номера брони он меняется в состоянии
    conn.execute(
        """UPDATE sessions SET state = NULL, state_enc = %s, state_version = %s, deck = %s, metrics = %s,
                  updated_at = now(), expires_at = now() + make_interval(days => %s)
           WHERE id = %s""",
        (encrypt_json(sess.state.model_dump(mode="json")), STATE_VERSION,
         Jsonb([c.model_dump(mode="json") for c in sess.deck]), Jsonb(asdict(sess.metrics)),
         SESSION_TTL_DAYS, sess.id))
    if sess.pending_messages:
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO messages (session_id, role, text_enc) VALUES (%s, %s, %s)",
                            [(sess.id, m["role"], encrypt_str(m["text"])) for m in sess.pending_messages])
        sess.pending_messages = []
    if sess.profile_dirty:
        _save_profile(conn, sess.user_id, sess.state)
        sess.profile_dirty = False
    for c in sess.pending_consents:
        conn.execute("""INSERT INTO consents (user_id, purpose, policy_version, granted, ip_hash)
                        VALUES (%s, %s, %s, %s, %s)""", (sess.user_id, c.purpose, c.policy_version, c.granted, c.ip_hash))
    sess.pending_consents = []


def _text(row: dict) -> str:
    return decrypt_str(row["text_enc"]) if row["text_enc"] else (row["text"] or "")


def recent_messages(session_id: str, user_id: str, limit: int = 50) -> List[dict]:
    with db.tx() as conn:
        rows = conn.execute(
            """SELECT m.role, m.text, m.text_enc, extract(epoch FROM m.created_at) AS at
               FROM messages m JOIN sessions s ON s.id = m.session_id
               WHERE m.session_id = %s AND s.user_id = %s ORDER BY m.id DESC LIMIT %s""",
            (session_id, user_id, limit)).fetchall()
    return [{"role": r["role"], "text": _text(r), "at": float(r["at"])} for r in reversed(rows)]


def all_metrics() -> List[Metrics]:
    with db.tx() as conn:
        rows = conn.execute("SELECT metrics FROM sessions WHERE (metrics->>'turns')::int > 0").fetchall()
    return [_metrics(r["metrics"]) for r in rows]


# ---------------------------------------------------------------- брони

def new_booking_id() -> str:
    return "OST-" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))


def _save_booking(conn: psycopg.Connection, sess: Session) -> None:
    s = sess.state
    b = s.booking
    from .validator import build_view, current_hotel  # локально: validator тянет tools и каталог
    h = current_hotel(s)
    details = {"cart": [i.model_dump(mode="json") for i in build_view(s).cart], "reminders": b.reminders}
    for ev in sess.pending_booking_events:
        if ev.type == "created":
            contact = encrypt_json(sess.pending_contact) if sess.pending_contact else None
            for _ in range(5):  # номер брони случайный: при совпадении с чужой бронью берём новый
                try:
                    with conn.transaction():  # savepoint — ошибка вставки не рушит всю транзакцию
                        conn.execute(
                            """INSERT INTO bookings (id, session_id, user_id, status, destination_id, check_in, check_out,
                                                     total, contact_enc, details)
                               VALUES (%s, %s, %s, 'confirmed', %s, %s, %s, %s, %s, %s)""",
                            (b.id, sess.id, sess.user_id, s.destination_id, h["check_in_date"] if h else None,
                             h["check_out_date"] if h else None, b.total, contact, Jsonb(details)))
                    break
                except psycopg.errors.UniqueViolation:
                    b.id = new_booking_id()
            else:
                raise RuntimeError("Не удалось подобрать уникальный номер брони")
            sess.pending_contact = None
        else:
            conn.execute(
                """UPDATE bookings SET status = %s, total = %s, check_in = %s, check_out = %s, details = %s,
                          updated_at = now() WHERE id = %s""",
                ("cancelled" if ev.type == "cancelled" else "changed", b.total,
                 h["check_in_date"] if h else None, h["check_out_date"] if h else None, Jsonb(details), b.id))
        conn.execute(
            """INSERT INTO booking_events (booking_id, type, amount_delta, total_after, description)
               VALUES (%s, %s, %s, %s, %s)""", (b.id, ev.type, ev.amount_delta, b.total, ev.description))
    sess.pending_booking_events = []


BOOKING_PUBLIC_FIELDS = "id, status, destination_id, check_in, check_out, total, details, created_at, updated_at"


def booking_history(booking_id: str, user_id: str) -> Optional[dict]:
    """Бронь владельца с историей изменений (без контактов)."""
    with db.tx() as conn:
        b = conn.execute(f"SELECT {BOOKING_PUBLIC_FIELDS} FROM bookings WHERE id = %s AND user_id = %s",
                         (booking_id, user_id)).fetchone()
        if not b:
            return None
        events = conn.execute(
            """SELECT type, amount_delta, total_after, description, created_at FROM booking_events
               WHERE booking_id = %s ORDER BY id""", (booking_id,)).fetchall()
    return {**b, "events": events}


def bookings_stats() -> Dict[str, int]:
    with db.tx() as conn:
        rows = conn.execute("SELECT status, count(*) AS n FROM bookings GROUP BY status").fetchall()
    return {r["status"]: r["n"] for r in rows}


# ---------------------------------------------------------------- профили

def _profile(row: Optional[dict]) -> Optional[dict]:
    if not row:
        return None
    return decrypt_json(row["profile_enc"]) if row["profile_enc"] else row["profile"]


def load_profile(user_id: Optional[str]) -> Optional[dict]:
    if not user_id:
        return None
    with db.tx() as conn:
        row = conn.execute("SELECT profile, profile_enc FROM profiles WHERE user_id = %s", (user_id,)).fetchone()
    return _profile(row)


def _save_profile(conn: psycopg.Connection, user_id: str, state: TripState) -> None:
    row = conn.execute("SELECT profile, profile_enc FROM profiles WHERE user_id = %s FOR UPDATE", (user_id,)).fetchone()
    trips = (_profile(row) or {}).get("trips", [])
    if state.booking and state.destination_id and state.booking.id not in [t.get("booking") for t in trips]:
        trips.append({"booking": state.booking.id, "destination": state.destination_id})
    profile = {
        "origin": state.origin,
        "travellers": state.travellers.model_dump(),
        "priorities": state.priorities.model_dump(),
        "trip_types": state.prefs.trip_types,
        "trips": trips[-10:],
    }
    conn.execute(
        """INSERT INTO profiles (user_id, profile, profile_enc) VALUES (%s, NULL, %s)
           ON CONFLICT (user_id) DO UPDATE SET profile = NULL, profile_enc = excluded.profile_enc, updated_at = now()""",
        (user_id, encrypt_json(profile)))


# ---------------------------------------------------------------- персональные данные (152-ФЗ)

def export_user_data(user_id: str) -> dict:
    """Всё, что мы храним о пользователе, в расшифрованном виде — для права на доступ к своим данным."""
    with db.tx() as conn:
        profile = _profile(conn.execute("SELECT profile, profile_enc FROM profiles WHERE user_id = %s",
                                        (user_id,)).fetchone())
        sessions = []
        for r in conn.execute("SELECT * FROM sessions WHERE user_id = %s ORDER BY created_at", (user_id,)).fetchall():
            msgs = conn.execute("SELECT role, text, text_enc, created_at FROM messages WHERE session_id = %s ORDER BY id",
                                (r["id"],)).fetchall()
            sessions.append({
                "id": r["id"], "created_at": r["created_at"], "expires_at": r["expires_at"],
                "trip": decrypt_json(r["state_enc"]) if r["state_enc"] else r["state"],
                "messages": [{"role": m["role"], "text": _text(m), "at": m["created_at"]} for m in msgs],
            })
        bookings = []
        for b in conn.execute(f"SELECT {BOOKING_PUBLIC_FIELDS}, contact_enc FROM bookings WHERE user_id = %s",
                              (user_id,)).fetchall():
            contact = decrypt_json(b.pop("contact_enc"))
            bookings.append({**b, "contact": contact})
        consents = conn.execute("SELECT purpose, policy_version, granted, created_at FROM consents WHERE user_id = %s "
                                "ORDER BY id", (user_id,)).fetchall()
        conn.execute("INSERT INTO privacy_log (user_hash, action) VALUES (%s, 'export')", (pseudonymize(user_id),))
    return {"user_id": user_id, "profile": profile, "sessions": sessions, "bookings": bookings, "consents": consents}


def erase_user_data(user_id: str) -> dict:
    """Удаление по запросу: сессии, история, профиль — удаляются; в бронях стираются контакты и привязка
    к пользователю (сама бронь остаётся как запись о сделке); согласия обезличиваются."""
    uh = pseudonymize(user_id)
    with db.tx() as conn:
        n_sessions = conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,)).rowcount
        conn.execute("DELETE FROM profiles WHERE user_id = %s", (user_id,))
        n_bookings = conn.execute(
            """UPDATE bookings SET contact_enc = NULL, contact_name = NULL, contact_email = NULL,
                      user_id = NULL, session_id = NULL, pii_erased_at = now() WHERE user_id = %s""",
            (user_id,)).rowcount
        conn.execute("UPDATE consents SET user_id = %s WHERE user_id = %s", ("erased:" + uh, user_id))
        conn.execute("DELETE FROM rate_limits WHERE key LIKE %s", (f"%:{user_id}",))
        details = {"sessions": n_sessions, "bookings_anonymized": n_bookings}
        conn.execute("INSERT INTO privacy_log (user_hash, action, details) VALUES (%s, 'erase', %s)", (uh, Jsonb(details)))
    return details


# ---------------------------------------------------------------- обслуживание

def purge_expired() -> dict:
    """Фоновая очистка: истёкшие сессии, старые счётчики лимитов, контакты в бронях после срока хранения."""
    with db.tx() as conn:
        sessions = conn.execute("DELETE FROM sessions WHERE expires_at < now()").rowcount
        conn.execute("DELETE FROM rate_limits WHERE window_start < now() - interval '2 days'")
        retained = conn.execute(
            """UPDATE bookings SET contact_enc = NULL, contact_name = NULL, contact_email = NULL, pii_erased_at = now()
               WHERE pii_erased_at IS NULL AND check_out < current_date - make_interval(days => %s)""",
            (config.BOOKING_PII_RETENTION_DAYS,)).rowcount
        if retained:
            conn.execute("INSERT INTO privacy_log (user_hash, action, details) VALUES ('-', 'retention', %s)",
                         (Jsonb({"bookings": retained}),))
    if sessions or retained:
        log.info("Очистка: сессий %s, контактов в бронях %s", sessions, retained)
    return {"sessions": sessions, "booking_contacts": retained}


def encrypt_legacy_rows() -> int:
    """Зашифровать строки, сохранённые до появления шифрования (идемпотентно, запускается при старте)."""
    n = 0
    with db.tx() as conn:
        for r in conn.execute("SELECT id, state FROM sessions WHERE state IS NOT NULL").fetchall():
            conn.execute("UPDATE sessions SET state_enc = %s, state = NULL WHERE id = %s", (encrypt_json(r["state"]), r["id"]))
            n += 1
        for r in conn.execute("SELECT id, text FROM messages WHERE text IS NOT NULL").fetchall():
            conn.execute("UPDATE messages SET text_enc = %s, text = NULL WHERE id = %s", (encrypt_str(r["text"]), r["id"]))
            n += 1
        for r in conn.execute("SELECT user_id, profile FROM profiles WHERE profile IS NOT NULL").fetchall():
            conn.execute("UPDATE profiles SET profile_enc = %s, profile = NULL WHERE user_id = %s",
                         (encrypt_json(r["profile"]), r["user_id"]))
            n += 1
        for r in conn.execute("""SELECT id, contact_name, contact_email FROM bookings
                                 WHERE contact_name IS NOT NULL OR contact_email IS NOT NULL""").fetchall():
            conn.execute("UPDATE bookings SET contact_enc = %s, contact_name = NULL, contact_email = NULL WHERE id = %s",
                         (encrypt_json({"name": r["contact_name"], "email": r["contact_email"]}), r["id"]))
            n += 1
    if n:
        log.info("Зашифровано старых строк с персональными данными: %s", n)
    return n
