from datetime import date
from typing import Any, Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class Travellers(BaseModel):
    adults: int = 2
    children: int = 0
    child_ages: List[int] = Field(default_factory=list)
    pets: bool = False


class Prefs(BaseModel):
    trip_types: List[str] = Field(default_factory=list)
    candidates: List[str] = Field(default_factory=list)
    max_flight_hours: Optional[float] = None
    direct_only: bool = False
    min_temp: Optional[int] = None
    max_temp: Optional[int] = None
    max_price_level: Optional[int] = None
    no_visa: bool = False
    meal_min: Optional[str] = None
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
    at: str
    label: str
    detail: str = ""
    status: Literal["ok", "warn", "error"] = "ok"
    icon: str = "•"


class Issue(BaseModel):
    code: str
    severity: Literal["info", "warn", "error"]
    message: str
    fix: Optional[str] = None


class Booking(BaseModel):
    id: str
    created_at: str
    total: int
    reminders: List[str] = Field(default_factory=list)
    changes: List[str] = Field(default_factory=list)


STATE_VERSION = 1


class TripState(BaseModel):
    model_config = ConfigDict(extra="ignore")

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
    hotel_check_in: Optional[date] = None
    flight_id: Optional[str] = None
    transfer_id: Optional[str] = None
    extras: List[CartItem] = Field(default_factory=list)

    excluded: Dict[str, List[str]] = Field(default_factory=lambda: {"destination": [], "hotel": [], "flight": []})
    assumptions: Dict[str, str] = Field(default_factory=dict)
    asked: List[str] = Field(default_factory=list)
    stage: Literal["start", "destination", "hotel", "flight", "review", "booked"] = "start"
    booking: Optional[Booking] = None


class VideoCredit(BaseModel):
    author: str
    license: str
    license_url: str = ""
    source_url: str
    note: str = ""


class Media(BaseModel):
    video_url: Optional[str] = None
    poster_url: Optional[str] = None
    gradient: List[str] = Field(default_factory=lambda: ["#0ea5e9", "#6366f1"])
    emoji: str = "✈️"
    ai_generated: bool = False
    credit: Optional[VideoCredit] = None


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
    tool: str
    summary: str


class StateView(BaseModel):
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
    changes: List[str] = Field(default_factory=list)
    ui_action: Optional[Literal["open_cart", "open_checkout", "open_booking"]] = None
    state: Optional[StateView] = None


SessionId = Field(min_length=8, max_length=64, pattern=r"^[a-f0-9]+$")


class TelegramAuthRequest(BaseModel):
    init_data: str = Field(min_length=1, max_length=4096)


class ChatRequest(BaseModel):
    session_id: str = SessionId
    message: str = Field(min_length=1, max_length=1000)


class FeedbackRequest(BaseModel):
    session_id: str = SessionId
    card_id: str = Field(max_length=120)
    kind: Literal["destination", "hotel", "flight"]
    action: Literal["like", "dislike"]
    reason: Optional[str] = Field(default=None, max_length=40)
    comment: Optional[str] = Field(default=None, max_length=500)


class ActionRequest(BaseModel):
    session_id: str = SessionId
    type: Literal["set_params", "set_priorities", "remove_item", "add_extra", "reset"]
    payload: Dict[str, Any] = Field(default_factory=dict, max_length=12)


class CheckoutRequest(BaseModel):
    session_id: str = SessionId
    contact_name: str = Field(default="", max_length=120)
    contact_email: str = Field(default="", max_length=254, pattern=r"^$|^[^@\s]+@[^@\s]+\.[^@\s]+$")
    consent: bool = False
    policy_version: str = Field(default="", max_length=40)
