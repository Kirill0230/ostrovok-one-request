import type { AgentReply, CardKind } from "./types";

const BASE = import.meta.env.VITE_API_URL ?? "";

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail?.detail ?? `Ошибка ${res.status}`);
  }
  return res.json();
}

export const mediaUrl = (u: string | null) => (u ? `${BASE}${u}` : null);

export const api = {
  newSession: (user_id: string) => post<{ session_id: string; reply: AgentReply }>("/api/session", { user_id }),
  chat: (session_id: string, message: string) => post<AgentReply>("/api/chat", { session_id, message }),
  feedback: (session_id: string, card_id: string, kind: CardKind, action: "like" | "dislike", reason?: string | null, comment?: string) =>
    post<AgentReply>("/api/feedback", { session_id, card_id, kind, action, reason, comment }),
  action: (session_id: string, type: string, payload: Record<string, unknown> = {}) =>
    post<AgentReply>("/api/action", { session_id, type, payload }),
  checkout: (session_id: string, contact_name: string, contact_email: string) =>
    post<AgentReply>("/api/checkout", { session_id, contact_name, contact_email }),
};

export function userId(): string {
  const tg = (window as any).Telegram?.WebApp?.initDataUnsafe?.user?.id;
  if (tg) return `tg-${tg}`;
  try {
    let id = localStorage.getItem("odin_user_id");
    if (!id) {
      id = Math.random().toString(36).slice(2) + Date.now().toString(36);
      localStorage.setItem("odin_user_id", id);
    }
    return id;
  } catch {
    return "anon";
  }
}

export const rub = (n: number) => `${Math.round(n).toLocaleString("ru-RU")} ₽`;
