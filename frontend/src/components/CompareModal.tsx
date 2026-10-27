import type { Card } from "../types";

interface Props {
  cards: Card[];
  current: string;
  onClose: () => void;
  onPick: (index: number) => void;
}

export default function CompareModal({ cards, current, onClose, onPick }: Props) {
  const keys = Array.from(new Set(cards.flatMap((c) => Object.keys(c.compare))));
  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div className="sheet wide" onClick={(e) => e.stopPropagation()}>
        <div className="sheet-grip" />
        <div className="sheet-title-row">
          <h3>Сравнение вариантов</h3>
          <button className="ghost small" onClick={onClose}>✕</button>
        </div>
        <div className="table-wrap">
          <table className="compare">
            <thead>
              <tr>
                <th />
                {cards.map((c, i) => (
                  <th key={c.id} className={c.id === current ? "current" : ""}>
                    <button className="link" onClick={() => onPick(i)}>{c.title}</button>
                    <small>совпадение {c.score}%</small>
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {keys.map((k) => (
                <tr key={k}>
                  <td>{k}</td>
                  {cards.map((c) => <td key={c.id} className={c.id === current ? "current" : ""}>{c.compare[k] ?? "—"}</td>)}
                </tr>
              ))}
              <tr>
                <td>Риски</td>
                {cards.map((c) => <td key={c.id} className={`small ${c.id === current ? "current" : ""}`}>{c.risks.join("; ") || "—"}</td>)}
              </tr>
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}
