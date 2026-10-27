import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "./api";
import { telegramReady } from "./telegram";
import type { AgentReply, Card, CardKind, ChatMessage, StateView } from "./types";
import Chat from "./components/Chat";
import Deck from "./components/Deck";
import TripPanel from "./components/TripPanel";
import CheckoutModal from "./components/CheckoutModal";
import ParamsModal from "./components/ParamsModal";

type Tab = "chat" | "deck" | "trip";

export default function App() {
  const [sessionId, setSessionId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [cards, setCards] = useState<Card[]>([]);
  const [deckKind, setDeckKind] = useState<CardKind | null>(null);
  const [view, setView] = useState<StateView | null>(null);
  const [quick, setQuick] = useState<string[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [tab, setTab] = useState<Tab>("chat");
  const [checkoutOpen, setCheckoutOpen] = useState(false);
  const [paramsOpen, setParamsOpen] = useState(false);
  const msgId = useRef(0);

  const push = (m: Omit<ChatMessage, "id">) => setMessages((prev) => [...prev, { ...m, id: ++msgId.current }]);

  const applyReply = useCallback((r: AgentReply, opts: { keepDeck?: boolean } = {}) => {
    push({ role: "agent", text: r.text, steps: r.steps });
    if (r.state) setView(r.state);
    setQuick(r.quick_replies);
    if (r.cards.length || !opts.keepDeck) {
      setCards(r.cards);
      setDeckKind(r.deck_kind);
    }
    if (r.cards.length) setTab("deck");
    if (r.ui_action === "open_cart" || r.ui_action === "open_booking") setTab("trip");
    if (r.ui_action === "open_checkout") setCheckoutOpen(true);
  }, []);

  const start = useCallback(async () => {
    setError(null);
    try {
      const { session_id, reply } = await api.newSession();
      setSessionId(session_id);
      setMessages([]);
      setCards([]);
      applyReply(reply);
      setTab("chat");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : "Не удалось подключиться к серверу. Запущен ли бэкенд на :8765?");
    }
  }, [applyReply]);

  const started = useRef(false);
  useEffect(() => {
    if (started.current) return; // StrictMode вызывает эффект дважды — не создаём две сессии
    started.current = true;
    telegramReady();
    start();
  }, [start]);

  const run = async (fn: () => Promise<AgentReply>, opts?: { keepDeck?: boolean }) => {
    if (!sessionId) return;
    setBusy(true);
    setError(null);
    try {
      applyReply(await fn(), opts);
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) {
        await start();
        setError("Сессия истекла — начали заново.");
      } else setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  };

  const send = (text: string) => {
    if (!text.trim()) return;
    push({ role: "user", text });
    if (text.trim().toLowerCase() === "оформить" && view?.can_checkout) {
      setCheckoutOpen(true);
      return;
    }
    run(() => api.chat(sessionId!, text));
  };

  const like = (card: Card) => {
    push({ role: "user", text: `❤️ ${card.title}` });
    run(() => api.feedback(sessionId!, card.id, card.kind, "like"));
  };

  const dislike = (card: Card, reason: string | null, comment: string) => {
    const label = card.dislike_reasons.find((r) => r.id === reason)?.label;
    push({ role: "user", text: `✖️ ${card.title}${label ? ` — ${label}` : ""}${comment ? ` — «${comment}»` : ""}` });
    run(() => api.feedback(sessionId!, card.id, card.kind, "dislike", reason, comment || undefined));
  };

  const action = (type: string, payload: Record<string, unknown> = {}) => run(() => api.action(sessionId!, type, payload), { keepDeck: false });

  const checkout = async (name: string, email: string, consent: boolean) => {
    setCheckoutOpen(false);
    push({ role: "user", text: "💳 Оплатить (демо)" });
    await run(() => api.checkout(sessionId!, name, email, consent));
  };

  const exportData = async () => {
    try {
      const data = await api.exportMyData();
      const url = URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" }));
      const a = document.createElement("a");
      a.href = url;
      a.download = "moi-dannye-odin-zapros.json";
      a.click();
      URL.revokeObjectURL(url);
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const deleteData = async () => {
    if (!window.confirm("Удалить все ваши данные: поездки, историю чата и сохранённые предпочтения? "
      + "Оформленные брони останутся обезличенными. Отменить нельзя.")) return;
    try {
      await api.deleteMyData();
      await start();
      setError("Ваши данные удалены. Начали с чистого листа.");
    } catch (e) {
      setError((e as Error).message);
    }
  };

  const cartCount = view?.cart.length ?? 0;
  const booked = view?.trip.stage === "booked";

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand">
          <span className="logo">О!</span>
          <div>
            <div className="brand-title">О!дин запрос</div>
            <div className="brand-sub">AI-агент собирает поездку целиком</div>
          </div>
        </div>
        <Progress stage={view?.trip.stage ?? "start"} />
        <button className="ghost small" onClick={() => action("reset")} title="Начать заново">↺ Заново</button>
      </header>

      {error && <div className="error-banner" onClick={() => setError(null)}>{error}</div>}

      <main className={`layout tab-${tab}`}>
        <section className="pane pane-chat">
          <Chat messages={messages} quick={quick} busy={busy} onSend={send} />
        </section>
        <section className="pane pane-deck">
          <Deck cards={cards} kind={deckKind} busy={busy} onLike={like} onDislike={dislike}
                lastAgentText={messages.filter((m) => m.role === "agent").slice(-1)[0]?.text} stage={view?.trip.stage}
                onSend={send} quick={quick} />
        </section>
        <section className="pane pane-trip">
          {view && (
            <TripPanel view={view} busy={busy} onAction={action} onSend={send} onExportData={exportData} onDeleteData={deleteData}
                       onCheckout={() => setCheckoutOpen(true)} onEditParams={() => setParamsOpen(true)} />
          )}
        </section>
      </main>

      <nav className="tabbar">
        <button className={tab === "chat" ? "active" : ""} onClick={() => setTab("chat")}>💬<span>Чат</span></button>
        <button className={tab === "deck" ? "active" : ""} onClick={() => setTab("deck")}>
          ✨<span>Варианты</span>{cards.length > 0 && <i className="badge">{cards.length}</i>}
        </button>
        <button className={tab === "trip" ? "active" : ""} onClick={() => setTab("trip")}>
          🧳<span>{booked ? "Бронь" : "Поездка"}</span>{cartCount > 0 && <i className="badge">{cartCount}</i>}
        </button>
      </nav>

      {checkoutOpen && view && <CheckoutModal view={view} onClose={() => setCheckoutOpen(false)} onPay={checkout} />}
      {paramsOpen && view && (
        <ParamsModal view={view} onClose={() => setParamsOpen(false)}
                     onSave={(p) => { setParamsOpen(false); push({ role: "user", text: "✏️ Изменил параметры поездки" }); action("set_params", p); }} />
      )}
    </div>
  );
}

const STEPS: [string, string][] = [["destination", "Куда"], ["hotel", "Отель"], ["flight", "Перелёт"], ["review", "Корзина"], ["booked", "Оплачено"]];

function Progress({ stage }: { stage: string }) {
  const idx = STEPS.findIndex(([s]) => s === stage);
  return (
    <ol className="progress">
      {STEPS.map(([s, label], i) => (
        <li key={s} className={i < idx ? "done" : i === idx ? "current" : ""}>{label}</li>
      ))}
    </ol>
  );
}
