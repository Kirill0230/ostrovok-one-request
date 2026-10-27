// Зеркало backend/app/models.py
export type CardKind = "destination" | "hotel" | "flight";

export interface Fact { icon: string; label: string }
export interface Reason { id: string; label: string }

export interface Card {
  id: string;
  kind: CardKind;
  title: string;
  subtitle: string;
  media: { video_url: string | null; poster_url: string | null; gradient: string[]; emoji: string; ai_generated: boolean };
  facts: Fact[];
  price: number;
  price_note: string;
  badges: string[];
  why: string[];
  tradeoffs: string[];
  risks: string[];
  score: number;
  compare: Record<string, string>;
  dislike_reasons: Reason[];
  data: Record<string, unknown>;
}

export interface CartItem { id: string; kind: "hotel" | "flight" | "transfer" | "extra"; title: string; subtitle: string; price: number; removable: boolean }
export interface TimelineEvent { at: string; label: string; detail: string; status: "ok" | "warn" | "error"; icon: string }
export interface Issue { code: string; severity: "info" | "warn" | "error"; message: string; fix: string | null }

export interface Priorities { price: number; flight_time: number; flexibility: number; meals: number; location: number; rating: number }

export interface Trip {
  origin: string;
  date_from: string | null;
  nights: number;
  travellers: { adults: number; children: number; child_ages: number[]; pets: boolean };
  budget: number | null;
  budget_level: string | null;
  prefs: Record<string, unknown> & { trip_types: string[] };
  priorities: Priorities;
  destination_id: string | null;
  hotel_id: string | null;
  flight_id: string | null;
  transfer_id: string | null;
  assumptions: Record<string, string>;
  stage: "start" | "destination" | "hotel" | "flight" | "review" | "booked";
  booking: null | { id: string; created_at: string; total: number; reminders: string[]; changes: string[] };
}

export interface StateView {
  trip: Trip;
  origin_name: string;
  destination_name: string | null;
  date_label: string | null;
  travellers_label: string;
  cart: CartItem[];
  total: number;
  issues: Issue[];
  timeline: TimelineEvent[];
  can_checkout: boolean;
}

export interface AgentReply {
  text: string;
  cards: Card[];
  deck_kind: CardKind | null;
  quick_replies: string[];
  steps: { tool: string; summary: string }[];
  changes: string[];
  ui_action: "open_cart" | "open_checkout" | "open_booking" | null;
  state: StateView | null;
}

export interface ChatMessage {
  id: number;
  role: "user" | "agent";
  text: string;
  steps?: AgentReply["steps"];
}
