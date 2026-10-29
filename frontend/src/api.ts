import type { AgentReply, CardKind } from "./types";
import { telegramInitData } from "./telegram";

const BASE = import.meta.env.VITE_API_URL ?? "";
const TOKEN_KEY = "odin_token";

export class ApiError extends Error {
  constructor(public status: number, message: string) {
    super(message);
  }
}

let token: string | null = readToken();
export let policyVersion = "";

function readToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_KEY);
  } catch {
    return null;
  }
}

function saveToken(t: string | null) {
  token = t;
  try {
    if (t) localStorage.setItem(TOKEN_KEY, t);
    else localStorage.removeItem(TOKEN_KEY);
  } catch {
  }
}

async function rawFetch(path: string, init: RequestInit = {}): Promise<Response> {
  const headers = new Headers(init.headers);
  if (init.body) headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  return fetch(`${BASE}${path}`, { ...init, headers });
}

async function errorFrom(res: Response): Promise<ApiError> {
  const detail = await res.json().catch(() => ({}));
  if (res.status === 429) return new ApiError(429, "Слишком много запросов — подождите немного и попробуйте снова.");
  const msg = typeof detail?.detail === "string" ? detail.detail : `Ошибка ${res.status}`;
  return new ApiError(res.status, msg);
}

export async function login(force = false): Promise<void> {
  if (token && !force) {
    if (!policyVersion) policyVersion = (await (await rawFetch("/api/privacy")).json()).policy_version;
    return;
  }
  const initData = telegramInitData();
  const res = initData
    ? await rawFetch("/api/auth/telegram", { method: "POST", body: JSON.stringify({ init_data: initData }) })
    : await rawFetch("/api/auth/anonymous", { method: "POST" });
  if (!res.ok) throw await errorFrom(res);
  const data = await res.json();
  saveToken(data.token);
  policyVersion = data.policy_version;
}

async function call<T>(path: string, init: RequestInit = {}, retried = false): Promise<T> {
  await login();
  const res = await rawFetch(path, init);
  if (res.status === 401 && !retried) {
    saveToken(null);
    await login(true);
    return call<T>(path, init, true);
  }
  if (!res.ok) throw await errorFrom(res);
  return res.json();
}

const post = <T>(path: string, body?: unknown) =>
  call<T>(path, { method: "POST", body: body === undefined ? undefined : JSON.stringify(body) });

export const mediaUrl = (u: string | null) => (u ? `${BASE}${u}` : null);

export const api = {
  newSession: () => post<{ session_id: string; reply: AgentReply }>("/api/session"),
  chat: (session_id: string, message: string) => post<AgentReply>("/api/chat", { session_id, message }),
  feedback: (session_id: string, card_id: string, kind: CardKind, action: "like" | "dislike", reason?: string | null, comment?: string) =>
    post<AgentReply>("/api/feedback", { session_id, card_id, kind, action, reason, comment }),
  action: (session_id: string, type: string, payload: Record<string, unknown> = {}) =>
    post<AgentReply>("/api/action", { session_id, type, payload }),
  checkout: (session_id: string, contact_name: string, contact_email: string, consent: boolean) =>
    post<AgentReply>("/api/checkout", { session_id, contact_name, contact_email, consent, policy_version: policyVersion }),
  exportMyData: () => call<unknown>("/api/me/export"),
  deleteMyData: async () => {
    const r = await call<unknown>("/api/me", { method: "DELETE" });
    saveToken(null);
    return r;
  },
};

export const rub = (n: number) => `${Math.round(n).toLocaleString("ru-RU")} ₽`;
