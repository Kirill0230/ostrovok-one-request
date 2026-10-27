import { useState } from "react";
import type { StateView } from "../types";

const ORIGINS: [string, string][] = [["moscow", "Москва"], ["spb", "Санкт-Петербург"], ["kazan", "Казань"], ["ekb", "Екатеринбург"], ["nsk", "Новосибирск"]];

interface Props {
  view: StateView;
  onClose: () => void;
  onSave: (payload: Record<string, unknown>) => void;
}

export default function ParamsModal({ view, onClose, onSave }: Props) {
  const t = view.trip;
  const [origin, setOrigin] = useState(t.origin);
  const [dateFrom, setDateFrom] = useState(t.date_from ?? "");
  const [nights, setNights] = useState(t.nights);
  const [adults, setAdults] = useState(t.travellers.adults);
  const [children, setChildren] = useState(t.travellers.children);
  const [pets, setPets] = useState(t.travellers.pets);
  const [budget, setBudget] = useState(t.budget ? String(t.budget) : "");

  const save = (e: React.FormEvent) => {
    e.preventDefault();
    onSave({ origin, date_from: dateFrom || undefined, nights, adults, children, pets, budget: budget ? Number(budget) : null });
  };

  return (
    <div className="sheet-backdrop" onClick={onClose}>
      <div className="sheet" onClick={(e) => e.stopPropagation()}>
        <div className="sheet-grip" />
        <div className="sheet-title-row">
          <h3>Параметры поездки</h3>
          <button className="ghost small" onClick={onClose}>✕</button>
        </div>
        <p className="muted">Агент пересоберёт план и поменяет только то, что зависит от изменений.</p>
        <form className="params-form" onSubmit={save}>
          <label>Откуда
            <select value={origin} onChange={(e) => setOrigin(e.target.value)}>
              {ORIGINS.map(([id, n]) => <option key={id} value={id}>{n}</option>)}
            </select>
          </label>
          <label>Вылет<input type="date" value={dateFrom} onChange={(e) => setDateFrom(e.target.value)} /></label>
          <label>Ночей<input type="number" min={1} max={30} value={nights} onChange={(e) => setNights(Number(e.target.value))} /></label>
          <label>Взрослых<input type="number" min={1} max={8} value={adults} onChange={(e) => setAdults(Number(e.target.value))} /></label>
          <label>Детей<input type="number" min={0} max={6} value={children} onChange={(e) => setChildren(Number(e.target.value))} /></label>
          <label>Бюджет, ₽<input type="number" min={0} step={1000} value={budget} placeholder="не задан" onChange={(e) => setBudget(e.target.value)} /></label>
          <label className="check"><input type="checkbox" checked={pets} onChange={(e) => setPets(e.target.checked)} /> Едем с питомцем</label>
          <button type="submit" className="primary wide">Пересобрать план</button>
        </form>
      </div>
    </div>
  );
}
