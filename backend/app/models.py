"""Схемы данных: состояние поездки, карточки, ответы API.

TripState — единственный источник правды о поездке. И агент-заглушка, и будущий
LLM-агент меняют только его, а фронт получает его целиком в каждом ответе.
"""
from datetime import date
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, Field


class Travellers(BaseModel):
    adults: int = 2
    children: int = 0
    child_ages: List[int] = Field(default_factory=list)
    pets: bool = False


class Prefs(BaseModel):
    """Жёсткие и мягкие ограничения, выведенные из запроса и дизлайков."""
    trip_types: List[str] = Field(default_factory=list)  # beach, city, food, party, relax, nature, shopping
    candidates: List[str] = Field(default_factory=list)  # ограничить направления этим списком (напр. «Таиланд»)
    max_flight_hours: Optional[float] = None
    direct_only: bool = False
    min_temp: Optional[int] = None
    max_temp: Optional[int] = None
    max_price_level: Optional[int] = None
    no_visa: bool = False
    meal_min: Optional[str] = None  # RO < BB < HB < AI
    free_cancel: bool = False
    max_beach_m: Optional[int] = None
    min_stars: Optional[int] = None
    min_rating: Optional[float] = None
    kids_club: bool = False
    hotel_max_night: Optional[int] = None
    flight_max: Optional[int] = None
    daytime_flights: bool = False
    baggage: bool = False


class Priorities(BaseModel):
    """Веса ранжирования 0..3. Пользователь может править их в UI."""
    price: int = 2
    flight_time: int = 1
    flexibility: int = 1
    meals: int = 1
    location: int = 1
    rating: int = 1


class CartItem(BaseModel):
    id: str
    kind: Literal["hotel", "flight", "transfer", "extra"]
    title: str
    subtitle: str = ""
    price: int
    removable: bool = True


class TimelineEvent(BaseModel):
    at: str  # ISO datetime, местное время точки
    label: str
    detail: str = ""
    status: Literal["ok", "warn", "error"] = "ok"
    icon: str = "•"


class Issue(BaseModel):
    code: str
    severity: Literal["info", "warn", "error"]
    message: str
    fix: Optional[str] = None  # фраза, которую фронт может отправить агенту как быстрый ответ


class Booking(BaseModel):
    id: str
    created_at: str
    total: int
    contact_name: str = ""
    contact_email: str = ""
    reminders: List[str] = Field(default_factory=list)
    changes: List[str] = Field(default_factory=list)


class TripState(BaseModel):
    origin: str = "moscow"
    date_from: Optional[date] = None
    nights: int = 7
    travellers: Travellers = Field(default_factory=Travellers)
    budget: Optional[int] = None
    budget_level: Optional[Literal["low", "mid", "high"]] = None
    prefs: Prefs = Field(default_factory=Prefs)
    priorities: Priorities = Field(default_factory=Priorities)

    destination_id: Optional[str] = None
    hotel_id: Optional[str] = None
    hotel_check_in: Optional[date] = None  # может отличаться от date_from, если рейс прилетает на следующий день
    flight_id: Optional[str] = None
    transfer_id: Optional[str] = None
    extras: List[CartItem] = Field(default_factory=list)

    excluded: Dict[str, List[str]] = Field(default_factory=lambda: {"destination": [], "hotel": [], "flight": []})
    assumptions: Dict[str, str] = Field(default_factory=dict)  # поле -> человеко-понятное допущение
    asked: List[str] = Field(default_factory=list)
    stage: Literal["start", "destination", "hotel", "flight", "review", "booked"] = "start"
    booking: Optional[Booking] = None


class Media(BaseModel):
    video_url: Optional[str] = None
    poster_url: Optional[str] = None
    gradient: List[str] = Field(default_factory=lambda: ["#0ea5e9", "#6366f1"])
    emoji: str = "✈️"
    ai_generated: bool = False


class Fact(BaseModel):
    icon: str
    label: str


class Reason(BaseModel):
    id: str
    label: str


class Card(BaseModel):
    id: str
    kind: Literal["destination", "hotel", "flight"]
    title: str
    subtitle: str = ""
    media: Media = Field(default_factory=Media)
    facts: List[Fact] = Field(default_factory=list)
    price: int = 0
    price_note: str = ""
    badges: List[str] = Field(default_factory=list)
    why: List[str] = Field(default_factory=list)
    tradeoffs: List[str] = Field(default_factory=list)
    risks: List[str] = Field(default_factory=list)
    score: float = 0
    compare: Dict[str, str] = Field(default_factory=dict)
    dislike_reasons: List[Reason] = Field(default_factory=list)
    data: Dict[str, Any] = Field(default_factory=dict)


class AgentStep(BaseModel):
    """Лог действий агента — показываем на фронте, чтобы было видно, что он делает, а не болтает."""
    tool: str
    summary: str


class StateView(BaseModel):
    """То, что фронт рисует в панели «Поездка»: состояние + производные данные."""
    trip: TripState
    origin_name: str
    destination_name: Optional[str] = None
    date_label: Optional[str] = None
    travellers_label: str
    cart: List[CartItem]
    total: int
    issues: List[Issue]
    timeline: List[TimelineEvent]
    can_checkout: bool


class AgentReply(BaseModel):
    text: str
    cards: List[Card] = Field(default_factory=list)
    deck_kind: Optional[Literal["destination", "hotel", "flight"]] = None
    quick_replies: List[str] = Field(default_factory=list)
    steps: List[AgentStep] = Field(default_factory=list)
    changes: List[str] = Field(default_factory=list)  # что поменялось в плане (для пересборки)
    ui_action: Optional[Literal["open_cart", "open_checkout", "open_booking"]] = None
    state: Optional[StateView] = None


# ---------- запросы API ----------

class NewSessionRequest(BaseModel):
    user_id: Optional[str] = None


class ChatRequest(BaseModel):
    session_id: str
    message: str


class FeedbackRequest(BaseModel):
    session_id: str
    card_id: str
    kind: Literal["destination", "hotel", "flight"]
    action: Literal["like", "dislike"]
    reason: Optional[str] = None
    comment: Optional[str] = None


class ActionRequest(BaseModel):
    """Прямые действия из UI без текста: правка параметров, приоритетов, удаление из корзины."""
    session_id: str
    type: Literal["set_params", "set_priorities", "remove_item", "add_extra", "reset"]
    payload: Dict[str, Any] = Field(default_factory=dict)


class CheckoutRequest(BaseModel):
    session_id: str
    contact_name: str = ""
    contact_email: str = ""
