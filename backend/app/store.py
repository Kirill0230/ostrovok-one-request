"""Хранилище на Postgres: сессии, история чата, профили, брони, согласия, аналитика.

Главные правила:
  • Оптимистичная блокировка. Сессия читается короткой транзакцией, агент (и LLM) работает БЕЗ открытого
    соединения, затем всё сохраняется одной транзакцией с проверкой версии строки. Если сессию за это
    время изменил параллельный запрос — save_session бросает SessionConflict, и запрос повторяется на
    свежем состоянии (main._handle).
  • Сессию видит только её владелец: чужой session_id ведёт себя как несуществующий.
  • Персональные данные лежат только в зашифрованных колонках *_enc (security/crypto.py).
  • Аналитика — отдельная обезличенная таблица analytics_events (analytics.py).

Агент в базу напрямую не пишет: он кладёт намерения в сессию (pending_*, profile_dirty),
а store сохраняет их при save_session.
"""
import logging
import os
import secrets
import string
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

import psycopg
from psycopg.types.json import Jsonb

from . import config, db
from .models import STATE_VERSION, Card, TripState
from .security.crypto import decrypt_json, decrypt_str, encrypt_json, encrypt_str, pseudonymize
from .state_migrations import StateTooNew, upgrade

log = logging.getLogger(__name__)

SESSION_TTL_DAYS = int(os.getenv("SESSION_TTL_DAYS", "30"))
MAINTENANCE_EVERY_S = int(os.getenv("MAINTENANCE_EVERY_S", "3600"))
MAINTENANCE_LOCK = 7212002


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
class AnalyticsEvent:
    event: str
    stage: Optional[str] = None
    props: Dict = field(default_factory=dict)


@dataclass
class Session:
    id: str
    user_id: str
    state: TripState = field(default_factory=TripState)
    deck: List[Card] = field(default_factory=list)
    metrics: Metrics = field(default_factory=Metrics)
    version: Optional[int] = None  # None — новая, ещё не сохранённая сессия
    # что сохранить при save_session
    pending_messages: List[Dict[str, str]] = field(default_factory=list)
    pending_booking_events: List[BookingEvent] = field(default_factory=list)
    pending_contact: Optional[Dict[str, str]] = None  # контакты для брони — только в bookings, не в состоянии
    pending_consents: List[Consent] = field(default_factory=list)
    pending_events: List[AnalyticsEvent] = field(default_factory=list)
    profile_dirty: bool = False

    def log(self, role: str, text: str) -> None:
        self.pending_messages.append({"role": role, "text": text})

    def track(self, event: str, **props) -> None:
        self.pending_events.append(AnalyticsEvent(event=event, stage=self.state.stage, props=props))


class SessionNotFound(Exception):
    pass


class SessionConflict(Exception):
    """Сессию изменил параллельный запрос, пока работал агент."""


def _metrics(raw: dict) -> Metrics:
    return Metrics(**{k: v for k, v in (raw or {}).items() if k in Metrics.__dataclass_fields__})


# ---------------------------------------------------------------- сессии

def new_session(user_id: str) -> Session:
    """Новая сессия в памяти; в базу попадёт при save_session."""
    return Session(id=uuid.uuid4().hex, user_id=user_id)


async def load_session(user_id: str, session_id: str) -> Session:
    async with db.tx() as conn:
        row = await (await conn.execute(
            "SELECT * FROM sessions WHERE id = %s AND user_id = %s AND expires_at > now()",
            (session_id, user_id))).fetchone()
    if not row:
        raise SessionNotFound(session_id)
    return _from_row(row)


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
    return Session(id=row["id"], user_id=row["user_id"], state=state, deck=deck, metrics=_metrics(row["metrics"]),
                   version=row["version"])


async def save_session(sess: Session) -> None:
    """Сохранить всё, что изменил агент, одной транзакцией. SessionConflict — если версия устарела."""
    state_enc = encrypt_json(sess.state.model_dump(mode="json"))
    deck = Jsonb([c.model_dump(mode="json") for c in sess.deck])
    metrics = Jsonb(asdict(sess.metrics))
    async with db.tx() as conn:
        if sess.version is None:
            await conn.execute(
                """INSERT INTO sessions (id, user_id, state_enc, state_version, deck, metrics, version, expires_at)
                   VALUES (%s, %s, %s, %s, %s, %s, 1, now() + make_interval(days => %s))""",
                (sess.id, sess.user_id, state_enc, STATE_VERSION, deck, metrics, SESSION_TTL_DAYS))
            new_version = 1
        else:
            row = await (await conn.execute(
                """UPDATE sessions SET state = NULL, state_enc = %s, state_version = %s, deck = %s, metrics = %s,
                          version = version + 1, updated_at = now(), expires_at = now() + make_interval(days => %s)
                   WHERE id = %s AND user_id = %s AND version = %s
                   RETURNING version""",
                (state_enc, STATE_VERSION, deck, metrics, SESSION_TTL_DAYS, sess.id, sess.user_id,
                 sess.version))).fetchone()
            if not row:
                raise SessionConflict(sess.id)  # rollback: ничего из этого запроса не сохранится
            new_version = row["version"]
        if sess.pending_booking_events:
            await _save_booking(conn, sess)
            # номер брони мог смениться при коллизии — перезаписываем состояние с актуальным номером
            await conn.execute("UPDATE sessions SET state_enc = %s WHERE id = %s",
                               (encrypt_json(sess.state.model_dump(mode="json")), sess.id))
        if sess.pending_messages:
            async with conn.cursor() as cur:
                await cur.executemany("INSERT INTO messages (session_id, role, text_enc) VALUES (%s, %s, %s)",
                                      [(sess.id, m["role"], encrypt_str(m["text"])) for m in sess.pending_messages])
        if sess.profile_dirty:
            await _save_profile(conn, sess.user_id, sess.state)
        for c in sess.pending_consents:
            await conn.execute("""INSERT INTO consents (user_id, purpose, policy_version, granted, ip_hash)
                                  VALUES (%s, %s, %s, %s, %s)""",
                               (sess.user_id, c.purpose, c.policy_version, c.granted, c.ip_hash))
        if sess.pending_events:
            ref = pseudonymize("session:" + sess.id)
            async with conn.cursor() as cur:
                await cur.executemany(
                    "INSERT INTO analytics_events (session_ref, event, stage, props) VALUES (%s, %s, %s, %s)",
                    [(ref, e.event, e.stage, Jsonb(e.props)) for e in sess.pending_events])
    sess.version = new_version
    sess.pending_messages, sess.pending_booking_events, sess.pending_consents, sess.pending_events = [], [], [], []
    sess.pending_contact, sess.profile_dirty = None, False


def _text(row: dict) -> str:
    return decrypt_str(row["text_enc"]) if row["text_enc"] else (row["text"] or "")


async def recent_messages(session_id: str, user_id: str, limit: int = 50) -> List[dict]:
    async with db.tx() as conn:
        rows = await (await conn.execute(
            """SELECT m.role, m.text, m.text_enc, extract(epoch FROM m.created_at) AS at
               FROM messages m JOIN sessions s ON s.id = m.session_id
               WHERE m.session_id = %s AND s.user_id = %s ORDER BY m.id DESC LIMIT %s""",
            (session_id, user_id, limit))).fetchall()
    return [{"role": r["role"], "text": _text(r), "at": float(r["at"])} for r in reversed(rows)]


async def all_metrics() -> List[Metrics]:
    async with db.tx() as conn:
        rows = await (await conn.execute(
            "SELECT metrics FROM sessions WHERE (metrics->>'turns')::int > 0")).fetchall()
    return [_metrics(r["metrics"]) for r in rows]


# ---------------------------------------------------------------- брони

def new_booking_id() -> str:
    return "OST-" + "".join(secrets.choice(string.ascii_uppercase + string.digits) for _ in range(8))


async def _save_booking(conn: psycopg.AsyncConnection, sess: Session) -> None:
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
                    async with conn.transaction():  # savepoint — ошибка вставки не рушит всю транзакцию
                        await conn.execute(
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
        else:
            await conn.execute(
                """UPDATE bookings SET status = %s, total = %s, check_in = %s, check_out = %s, details = %s,
                          updated_at = now() WHERE id = %s""",
                ("cancelled" if ev.type == "cancelled" else "changed", b.total,
                 h["check_in_date"] if h else None, h["check_out_date"] if h else None, Jsonb(details), b.id))
        await conn.execute(
            """INSERT INTO booking_events (booking_id, type, amount_delta, total_after, description)
               VALUES (%s, %s, %s, %s, %s)""", (b.id, ev.type, ev.amount_delta, b.total, ev.description))


BOOKING_PUBLIC_FIELDS = "id, status, destination_id, check_in, check_out, total, details, created_at, updated_at"


async def booking_history(booking_id: str, user_id: str) -> Optional[dict]:
    """Бронь владельца с историей изменений (без контактов)."""
    async with db.tx() as conn:
        b = await (await conn.execute(f"SELECT {BOOKING_PUBLIC_FIELDS} FROM bookings WHERE id = %s AND user_id = %s",
                                      (booking_id, user_id))).fetchone()
        if not b:
            return None
        events = await (await conn.execute(
            """SELECT type, amount_delta, total_after, description, created_at FROM booking_events
               WHERE booking_id = %s ORDER BY id""", (booking_id,))).fetchall()
    return {**b, "events": events}


async def bookings_stats() -> Dict[str, int]:
    async with db.tx() as conn:
        rows = await (await conn.execute("SELECT status, count(*) AS n FROM bookings GROUP BY status")).fetchall()
    return {r["status"]: r["n"] for r in rows}


# ---------------------------------------------------------------- профили

def _profile(row: Optional[dict]) -> Optional[dict]:
    if not row:
        return None
    return decrypt_json(row["profile_enc"]) if row["profile_enc"] else row["profile"]


async def load_profile(user_id: Optional[str]) -> Optional[dict]:
    if not user_id:
        return None
    async with db.tx() as conn:
        row = await (await conn.execute("SELECT profile, profile_enc FROM profiles WHERE user_id = %s",
                                        (user_id,))).fetchone()
    return _profile(row)


async def _save_profile(conn: psycopg.AsyncConnection, user_id: str, state: TripState) -> None:
    row = await (await conn.execute("SELECT profile, profile_enc FROM profiles WHERE user_id = %s FOR UPDATE",
                                    (user_id,))).fetchone()
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
    await conn.execute(
        """INSERT INTO profiles (user_id, profile, profile_enc) VALUES (%s, NULL, %s)
           ON CONFLICT (user_id) DO UPDATE SET profile = NULL, profile_enc = excluded.profile_enc, updated_at = now()""",
        (user_id, encrypt_json(profile)))


# ---------------------------------------------------------------- персональные данные (152-ФЗ)

async def export_user_data(user_id: str) -> dict:
    """Всё, что мы храним о пользователе, в расшифрованном виде — для права на доступ к своим данным."""
    async with db.tx() as conn:
        profile = _profile(await (await conn.execute("SELECT profile, profile_enc FROM profiles WHERE user_id = %s",
                                                     (user_id,))).fetchone())
        sessions = []
        for r in await (await conn.execute("SELECT * FROM sessions WHERE user_id = %s ORDER BY created_at",
                                           (user_id,))).fetchall():
            msgs = await (await conn.execute(
                "SELECT role, text, text_enc, created_at FROM messages WHERE session_id = %s ORDER BY id",
                (r["id"],))).fetchall()
            sessions.append({
                "id": r["id"], "created_at": r["created_at"], "expires_at": r["expires_at"],
                "trip": decrypt_json(r["state_enc"]) if r["state_enc"] else r["state"],
                "messages": [{"role": m["role"], "text": _text(m), "at": m["created_at"]} for m in msgs],
            })
        bookings = []
        for b in await (await conn.execute(f"SELECT {BOOKING_PUBLIC_FIELDS}, contact_enc FROM bookings WHERE user_id = %s",
                                           (user_id,))).fetchall():
            contact = decrypt_json(b.pop("contact_enc"))
            bookings.append({**b, "contact": contact})
        consents = await (await conn.execute(
            "SELECT purpose, policy_version, granted, created_at FROM consents WHERE user_id = %s ORDER BY id",
            (user_id,))).fetchall()
        await conn.execute("INSERT INTO privacy_log (user_hash, action) VALUES (%s, 'export')", (pseudonymize(user_id),))
    return {"user_id": user_id, "profile": profile, "sessions": sessions, "bookings": bookings, "consents": consents}


async def erase_user_data(user_id: str) -> dict:
    """Удаление по запросу: сессии, история, профиль — удаляются; в бронях стираются контакты и привязка
    к пользователю (сама бронь остаётся как запись о сделке); согласия обезличиваются.
    Счётчики лимитов в Redis истекают сами в течение суток; аналитика и так обезличена."""
    uh = pseudonymize(user_id)
    async with db.tx() as conn:
        n_sessions = (await conn.execute("DELETE FROM sessions WHERE user_id = %s", (user_id,))).rowcount
        await conn.execute("DELETE FROM profiles WHERE user_id = %s", (user_id,))
        n_bookings = (await conn.execute(
            """UPDATE bookings SET contact_enc = NULL, contact_name = NULL, contact_email = NULL,
                      user_id = NULL, session_id = NULL, pii_erased_at = now() WHERE user_id = %s""",
            (user_id,))).rowcount
        await conn.execute("UPDATE consents SET user_id = %s WHERE user_id = %s", ("erased:" + uh, user_id))
        details = {"sessions": n_sessions, "bookings_anonymized": n_bookings}
        await conn.execute("INSERT INTO privacy_log (user_hash, action, details) VALUES (%s, 'erase', %s)",
                           (uh, Jsonb(details)))
    return details


# ---------------------------------------------------------------- обслуживание

async def run_maintenance(force: bool = False) -> Optional[dict]:
    """Очистка истёкших сессий и контактов в бронях после срока хранения + шифрование старых строк.

    Каждый экземпляр сервиса периодически вызывает эту функцию, но выполняет её только один:
    advisory-блокировка не даёт запускать параллельно, а maintenance_runs — чаще MAINTENANCE_EVERY_S.
    Возвращает результат или None, если в этот раз работу сделал (или делает) кто-то другой.
    """
    async with db.tx() as conn:
        got = await (await conn.execute("SELECT pg_try_advisory_xact_lock(%s) AS ok", (MAINTENANCE_LOCK,))).fetchone()
        if not got["ok"]:
            return None
        last = await (await conn.execute(
            "SELECT last_run_at > now() - make_interval(secs => %s) AS fresh FROM maintenance_runs WHERE job = 'purge'",
            (MAINTENANCE_EVERY_S,))).fetchone()
        if last and last["fresh"] and not force:
            return None
        result = {"encrypted_legacy": await _encrypt_legacy_rows(conn)}
        result["sessions"] = (await conn.execute("DELETE FROM sessions WHERE expires_at < now()")).rowcount
        result["booking_contacts"] = (await conn.execute(
            """UPDATE bookings SET contact_enc = NULL, contact_name = NULL, contact_email = NULL, pii_erased_at = now()
               WHERE pii_erased_at IS NULL AND check_out < current_date - make_interval(days => %s)""",
            (config.BOOKING_PII_RETENTION_DAYS,))).rowcount
        if result["booking_contacts"]:
            await conn.execute("INSERT INTO privacy_log (user_hash, action, details) VALUES ('-', 'retention', %s)",
                               (Jsonb({"bookings": result["booking_contacts"]}),))
        await conn.execute(
            """INSERT INTO maintenance_runs (job, last_run_at, last_result) VALUES ('purge', now(), %s)
               ON CONFLICT (job) DO UPDATE SET last_run_at = now(), last_result = excluded.last_result""",
            (Jsonb(result),))
    if any(result.values()):
        log.info("Обслуживание: %s", result)
    return result


async def _encrypt_legacy_rows(conn: psycopg.AsyncConnection) -> int:
    """Зашифровать строки, сохранённые до появления шифрования (идемпотентно)."""
    n = 0
    for r in await (await conn.execute("SELECT id, state FROM sessions WHERE state IS NOT NULL")).fetchall():
        await conn.execute("UPDATE sessions SET state_enc = %s, state = NULL WHERE id = %s",
                           (encrypt_json(r["state"]), r["id"]))
        n += 1
    for r in await (await conn.execute("SELECT id, text FROM messages WHERE text IS NOT NULL")).fetchall():
        await conn.execute("UPDATE messages SET text_enc = %s, text = NULL WHERE id = %s", (encrypt_str(r["text"]), r["id"]))
        n += 1
    for r in await (await conn.execute("SELECT user_id, profile FROM profiles WHERE profile IS NOT NULL")).fetchall():
        await conn.execute("UPDATE profiles SET profile_enc = %s, profile = NULL WHERE user_id = %s",
                           (encrypt_json(r["profile"]), r["user_id"]))
        n += 1
    for r in await (await conn.execute("""SELECT id, contact_name, contact_email FROM bookings
                                          WHERE contact_name IS NOT NULL OR contact_email IS NOT NULL""")).fetchall():
        await conn.execute("UPDATE bookings SET contact_enc = %s, contact_name = NULL, contact_email = NULL WHERE id = %s",
                           (encrypt_json({"name": r["contact_name"], "email": r["contact_email"]}), r["id"]))
        n += 1
    return n


# ---------------------------------------------------------------- аналитика

async def funnel(days: int = 30) -> dict:
    """Воронка и основные срезы по обезличенным событиям за последние days дней."""
    async with db.tx() as conn:
        def q(sql, *args):
            return conn.execute(sql, args)
        steps = await (await q(
            """SELECT
                 count(DISTINCT session_ref) FILTER (WHERE event = 'session_started')                       AS started,
                 count(DISTINCT session_ref) FILTER (WHERE event = 'recommendations_shown')                 AS saw_options,
                 count(DISTINCT session_ref) FILTER (WHERE event = 'feedback' AND props->>'action' = 'like'
                                                     AND props->>'kind' = 'destination')                    AS chose_destination,
                 count(DISTINCT session_ref) FILTER (WHERE event = 'feedback' AND props->>'action' = 'like'
                                                     AND props->>'kind' = 'hotel')                          AS chose_hotel,
                 count(DISTINCT session_ref) FILTER (WHERE event = 'cart_ready')                            AS reached_cart,
                 count(DISTINCT session_ref) FILTER (WHERE event = 'booked')                                AS booked
               FROM analytics_events WHERE at > now() - make_interval(days => %s)""", days)).fetchone()
        dislikes = await (await q(
            """SELECT props->>'kind' AS kind, coalesce(props->>'reason', 'без причины') AS reason, count(*) AS n
               FROM analytics_events WHERE event = 'feedback' AND props->>'action' = 'dislike'
                 AND at > now() - make_interval(days => %s)
               GROUP BY 1, 2 ORDER BY n DESC LIMIT 20""", days)).fetchall()
        destinations = await (await q(
            """SELECT props->>'destination' AS destination,
                      count(*) FILTER (WHERE event = 'feedback' AND props->>'action' = 'like') AS liked,
                      count(*) FILTER (WHERE event = 'feedback' AND props->>'action' = 'dislike') AS disliked,
                      count(*) FILTER (WHERE event = 'booked') AS booked
               FROM analytics_events WHERE props ? 'destination' AND at > now() - make_interval(days => %s)
               GROUP BY 1 ORDER BY booked DESC, liked DESC""", days)).fetchall()
        replans = await (await q(
            """SELECT count(*) AS n FROM analytics_events
               WHERE event = 'plan_changed' AND at > now() - make_interval(days => %s)""", days)).fetchone()
    started = steps["started"] or 1
    return {
        "days": days,
        "funnel": {k: {"sessions": v, "share": round(v / started, 3)} for k, v in steps.items()},
        "dislike_reasons": dislikes,
        "destinations": destinations,
        "plan_changes": replans["n"],
    }
