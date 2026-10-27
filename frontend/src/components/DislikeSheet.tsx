import { useState } from "react";
import type { Card } from "../types";

interface Props {
  card: Card;
  onClose: () => void;
  onSubmit: (reason: string | null, comment: string) => void;
}

export default function DislikeSheet({ card, onClose, onSubmit }: Props) {
  const [comment, setComment] = useState("");
  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div className="sheet" onClick={(e) => e.stopPropagation()}>
        <div className="sheet-grip" />
        <h3>Что не так с «{card.title}»?</h3>
        <p className="muted">Агент поменяет именно это и пересоберёт варианты.</p>
        <div className="reasons">
          {card.dislike_reasons.map((r) => (
            <button key={r.id} className="chip big" onClick={() => onSubmit(r.id, comment)}>{r.label}</button>
          ))}
        </div>
        <form onSubmit={(e) => { e.preventDefault(); onSubmit(null, comment); }} className="sheet-form">
          <input value={comment} onChange={(e) => setComment(e.target.value)} placeholder="Своими словами: «хочу потише и без жары»" autoFocus />
          <button type="submit" className="primary">{comment ? "Отправить" : "Просто дальше"}</button>
        </form>
      </div>
    </div>
  );
}
