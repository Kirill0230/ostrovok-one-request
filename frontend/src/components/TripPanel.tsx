import { rub } from "../api";
import type { Priorities, StateView } from "../types";

interface Props {
  view: StateView;
  busy: boolean;
  onAction: (type: string, payload?: Record<string, unknown>) => void;
  onSend: (text: string) => void;
  onCheckout: () => void;
  onEditParams: () => void;
  onExportData: () => void;
  onDeleteData: () => void;
}

const PRIORITY_LABELS: [keyof Priorities, string][] = [
  ["price", "Цена"], ["flight_time", "Время в пути"], ["flexibility", "Бесплатная отмена"],
  ["meals", "Питание"], ["location", "Расположение"], ["rating", "Рейтинг"],
];
const KIND_ICON: Record<string, string> = { flight: "✈️", hotel: "🏨", transfer: "🚐", extra: "➕" };
const TRANSFERS: [string, string][] = [["taxi", "🚕 Такси"], ["minivan", "🚐 Минивэн"], ["shuttle", "🚌 Шаттл"]];

const time = (iso: string) => {
  const d = new Date(iso);
  return `${d.getDate()}.${String(d.getMonth() + 1).padStart(2, "0")} ${String(d.getHours()).padStart(2, "0")}:${String(d.getMinutes()).padStart(2, "0")}`;
};

export default function TripPanel({ view, busy, onAction, onSend, onCheckout, onEditParams, onExportData, onDeleteData }: Props) {
  const t = view.trip;
  const booked = t.stage === "booked";
  const budget = t.budget;
  const pct = budget ? Math.min(100, Math.round((view.total / budget) * 100)) : 0;
  const transferKind = t.transfer_id?.split("~")[2];

  return (
    <div className="trip">
      {booked && t.booking && (
        <div className="booking-card">
          <div className="booking-head">
            <span>✅ Оплачено (демо)</span>
            <b>{t.booking.id}</b>
          </div>
          <div className="booking-total">{rub(t.booking.total)}</div>
          {t.booking.changes.length > 0 && (
            <div className="booking-changes">
              <h4>Изменения после оплаты</h4>
              <ul>{t.booking.changes.map((c, i) => <li key={i}>{c}</li>)}</ul>
            </div>
          )}
          <h4>Напоминания</h4>
          <ul className="reminders">{t.booking.reminders.map((r) => <li key={r}>🔔 {r}</li>)}</ul>
          <div className="quick">
            {["Добавь ранний заезд", "Добавь поздний выезд", "А если на день позже?", "Поменяй трансфер на минивэн"].map((q) => (
              <button key={q} className="chip" disabled={busy} onClick={() => onSend(q)}>{q}</button>
            ))}
          </div>
        </div>
      )}

      <section className="block">
        <div className="block-head">
          <h3>Параметры поездки</h3>
          <button className="ghost small" onClick={onEditParams} disabled={busy}>✏️ Изменить</button>
        </div>
        <div className="params">
          <div><span>Откуда</span>{view.origin_name}</div>
          <div><span>Куда</span>{view.destination_name ?? "подбираем"}</div>
          <div><span>Когда</span>{view.date_label ?? "—"}</div>
          <div><span>Кто едет</span>{view.travellers_label}</div>
          <div><span>Бюджет</span>{budget ? `до ${rub(budget)}` : t.budget_level === "low" ? "недорого" : t.budget_level === "high" ? "премиум" : "не задан"}</div>
        </div>
        {Object.keys(t.assumptions).length > 0 && (
          <p className="assumptions">🤔 Агент предположил: {Object.values(t.assumptions).join(", ")}. Поправьте, если не так.</p>
        )}
      </section>

      {!booked && (
        <section className="block">
          <div className="block-head">
            <h3>Что важнее</h3>
            <small className="muted">нажмите, чтобы изменить вес</small>
          </div>
          <div className="priorities">
            {PRIORITY_LABELS.map(([k, label]) => (
              <button key={k} className="prio" disabled={busy}
                      onClick={() => onAction("set_priorities", { [k]: (t.priorities[k] + 1) % 4 })}>
                <span>{label}</span>
                <i>{[0, 1, 2].map((i) => <b key={i} className={i < t.priorities[k] ? "on" : ""} />)}</i>
              </button>
            ))}
          </div>
        </section>
      )}

      <section className="block">
        <div className="block-head"><h3>{booked ? "Состав брони" : "Корзина"}</h3></div>
        {view.cart.length === 0 ? (
          <p className="muted">Пока пусто. Лайкайте варианты — они попадут сюда.</p>
        ) : (
          <ul className="cart">
            {view.cart.map((i) => (
              <li key={i.id}>
                <span className="cart-icon">{KIND_ICON[i.kind]}</span>
                <div className="cart-body">
                  <b>{i.title}</b>
                  <small>{i.subtitle}</small>
                  {i.kind === "transfer" && !booked && (
                    <div className="seg">
                      {TRANSFERS.map(([k, l]) => (
                        <button key={k} className={transferKind === k ? "on" : ""} disabled={busy}
                                onClick={() => onAction("add_extra", { transfer: k })}>{l}</button>
                      ))}
                    </div>
                  )}
                </div>
                <span className="cart-price">{rub(i.price)}</span>
                {!booked && i.removable && (
                  <button className="x" disabled={busy} onClick={() => onAction("remove_item", { kind: i.kind, id: i.id })} aria-label="Убрать">×</button>
                )}
              </li>
            ))}
          </ul>
        )}
        <div className="total-row">
          <span>Итого</span>
          <b>{rub(view.total)}</b>
        </div>
        {budget && view.cart.length > 0 ? (
          <div className={`budget-bar ${view.total > budget ? "over" : ""}`}>
            <div style={{ width: `${pct}%` }} />
            <small>{view.total > budget ? `выше бюджета на ${rub(view.total - budget)}` : `в бюджете, запас ${rub(budget - view.total)}`}</small>
          </div>
        ) : null}
      </section>

      {view.issues.length > 0 && (
        <section className="block">
          <div className="block-head"><h3>Проверка стыковок</h3></div>
          <ul className="issues">
            {view.issues.map((i) => (
              <li key={i.code} className={i.severity}>
                <span>{i.severity === "error" ? "❌" : i.severity === "warn" ? "⚠️" : "ℹ️"}</span>
                <div>
                  {i.message}
                  {i.fix && !booked && <button className="link" disabled={busy} onClick={() => onSend(i.fix!)}>{i.fix} →</button>}
                </div>
              </li>
            ))}
          </ul>
        </section>
      )}

      {view.timeline.length > 0 && (
        <section className="block">
          <div className="block-head"><h3>Маршрут по времени</h3><small className="muted">местное время</small></div>
          <ol className="timeline">
            {view.timeline.map((e, i) => (
              <li key={i} className={e.status}>
                <span className="tl-icon">{e.icon}</span>
                <div>
                  <div className="tl-row"><b>{e.label}</b><time>{time(e.at)}</time></div>
                  <small>{e.detail}</small>
                </div>
              </li>
            ))}
          </ol>
        </section>
      )}

      {!booked && (
        <div className="checkout-bar">
          <button className="primary wide" disabled={!view.can_checkout || busy} onClick={onCheckout}>
            {view.can_checkout ? `Оформить за ${rub(view.total)}` : view.cart.length ? "Есть что поправить" : "Соберите поездку"}
          </button>
        </div>
      )}
      <p className="disclaimer">Прототип: отели, рейсы и цены — синтетические данные, климат приблизительный, оплата имитируется. Видео направлений сгенерированы ИИ.</p>
      <nav className="privacy-links">
        <a href="/privacy.html" target="_blank" rel="noopener">Политика конфиденциальности</a>
        <button className="link" onClick={onExportData}>Скачать мои данные</button>
        <button className="link danger" onClick={onDeleteData}>Удалить мои данные</button>
      </nav>
    </div>
  );
}
