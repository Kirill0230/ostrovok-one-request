import { useState } from "react";
import { rub } from "../api";
import type { StateView } from "../types";

interface Props {
  view: StateView;
  onClose: () => void;
  onPay: (name: string, email: string) => void;
}

export default function CheckoutModal({ view, onClose, onPay }: Props) {
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const warns = view.issues.filter((i) => i.severity === "warn");
  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div className="sheet" onClick={(e) => e.stopPropagation()}>
        <div className="sheet-grip" />
        <div className="sheet-title-row">
          <h3>Оформление</h3>
          <button className="ghost small" onClick={onClose}>✕</button>
        </div>
        <ul className="cart compact">
          {view.cart.map((i) => (
            <li key={i.id}><div className="cart-body"><b>{i.title}</b><small>{i.subtitle}</small></div><span className="cart-price">{rub(i.price)}</span></li>
          ))}
        </ul>
        <div className="total-row"><span>К оплате</span><b>{rub(view.total)}</b></div>
        {warns.length > 0 && <p className="warn-note">⚠️ {warns.map((w) => w.message).join("; ")}</p>}
        <form className="pay-form" onSubmit={(e) => { e.preventDefault(); onPay(name, email); }}>
          <label>Имя контактного лица<input value={name} onChange={(e) => setName(e.target.value)} placeholder="Иван Иванов" /></label>
          <label>Email для подтверждения<input type="email" value={email} onChange={(e) => setEmail(e.target.value)} placeholder="ivan@example.com" /></label>
          <p className="muted small">Это демо: оплата имитируется, деньги не списываются, данные карты не нужны.</p>
          <button type="submit" className="primary wide">💳 Оплатить {rub(view.total)} (демо)</button>
        </form>
      </div>
    </div>
  );
}
