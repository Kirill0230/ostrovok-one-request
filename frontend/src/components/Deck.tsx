import { useEffect, useRef, useState } from "react";
import { mediaUrl, rub } from "../api";
import type { Card, CardKind } from "../types";
import { QuickReplies } from "./Chat";
import CompareModal from "./CompareModal";
import DislikeSheet from "./DislikeSheet";

interface Props {
  cards: Card[];
  kind: CardKind | null;
  busy: boolean;
  quick: string[];
  lastAgentText?: string;
  stage?: string;
  onLike: (c: Card) => void;
  onDislike: (c: Card, reason: string | null, comment: string) => void;
  onSend: (t: string) => void;
}

const TITLES: Record<CardKind, string> = { destination: "Куда поехать", hotel: "Где жить", flight: "Как лететь" };
const SWIPE = 110;

export default function Deck({ cards, kind, busy, quick, lastAgentText, stage, onLike, onDislike, onSend }: Props) {
  const [idx, setIdx] = useState(0);
  const [dx, setDx] = useState(0);
  const [dislikeFor, setDislikeFor] = useState<Card | null>(null);
  const [compare, setCompare] = useState(false);
  const start = useRef<{ x: number; y: number } | null>(null);

  useEffect(() => setIdx(0), [cards]);
  const card = cards[Math.min(idx, cards.length - 1)];

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (!card || busy || dislikeFor || compare || (e.target as HTMLElement).tagName === "INPUT") return;
      if (e.key === "ArrowRight") onLike(card);
      if (e.key === "ArrowLeft") setDislikeFor(card);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [card, busy, dislikeFor, compare, onLike]);

  if (!card) {
    return (
      <div className="deck empty">
        <div className="empty-state">
          <div className="empty-emoji">{stage === "booked" ? "🎉" : stage === "review" ? "🧳" : "🧭"}</div>
          <p>{stage === "booked" ? "Поездка оплачена! Бронь и напоминания — в панели «Поездка»."
            : stage === "review" ? "Поездка собрана — проверьте корзину и маршрут. Можно попросить агента что-то поменять."
            : lastAgentText ? "Здесь появятся варианты. Напишите агенту, чего хочется, — он подберёт направления с видео." : "Вариантов пока нет."}</p>
          <QuickReplies quick={quick} busy={busy} onSend={onSend} />
        </div>
      </div>
    );
  }

  const onDown = (e: React.PointerEvent) => {
    if ((e.target as HTMLElement).closest("button, details, a, .card-scroll")) return;
    start.current = { x: e.clientX, y: e.clientY };
    (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
  };
  const onMove = (e: React.PointerEvent) => {
    if (!start.current) return;
    setDx(e.clientX - start.current.x);
  };
  const onUp = () => {
    if (!start.current) return;
    start.current = null;
    if (dx > SWIPE) onLike(card);
    else if (dx < -SWIPE) setDislikeFor(card);
    setDx(0);
  };

  const style = { transform: `translateX(${dx}px) rotate(${dx / 25}deg)`, transition: start.current ? "none" : "transform .25s ease" };

  return (
    <div className={`deck ${busy ? "is-busy" : ""}`}>
      <div className="deck-head">
        <h2>{kind ? TITLES[kind] : ""}</h2>
        <div className="deck-nav">
          <button className="ghost small" disabled={idx === 0} onClick={() => setIdx(idx - 1)} aria-label="Предыдущий">‹</button>
          <span>{idx + 1} / {cards.length}</span>
          <button className="ghost small" disabled={idx >= cards.length - 1} onClick={() => setIdx(idx + 1)} aria-label="Следующий">›</button>
          {cards.length > 1 && <button className="ghost small" onClick={() => setCompare(true)}>⚖️ Сравнить</button>}
        </div>
      </div>

      <div className="card-stage">
        {cards[idx + 1] && <div className="card ghost-card" />}
        <article className={`card card-${card.kind}`} style={style} onPointerDown={onDown} onPointerMove={onMove}
                 onPointerUp={onUp} onPointerCancel={onUp}>
          <div className={`stamp like ${dx > 40 ? "show" : ""}`}>ЕДЕМ</div>
          <div className={`stamp nope ${dx < -40 ? "show" : ""}`}>НЕ ТО</div>
          {card.kind === "destination" ? <DestinationBody card={card} /> : <OptionBody card={card} />}
        </article>
      </div>

      <div className="deck-actions">
        <button className="round nope" disabled={busy} onClick={() => setDislikeFor(card)} aria-label="Не нравится">✕</button>
        <div className="deck-hint">свайп или ← →</div>
        <button className="round like" disabled={busy} onClick={() => onLike(card)} aria-label="Нравится">❤</button>
      </div>

      {dislikeFor && (
        <DislikeSheet card={dislikeFor} onClose={() => setDislikeFor(null)}
                      onSubmit={(reason, comment) => { const c = dislikeFor; setDislikeFor(null); onDislike(c, reason, comment); }} />
      )}
      {compare && <CompareModal cards={cards} current={card.id} onClose={() => setCompare(false)}
                                onPick={(i) => { setIdx(i); setCompare(false); }} />}
    </div>
  );
}

function DestinationBody({ card }: { card: Card }) {
  const [videoOk, setVideoOk] = useState(true);
  const [muted, setMuted] = useState(true);
  const [open, setOpen] = useState(false);
  const video = mediaUrl(card.media.video_url);
  useEffect(() => setVideoOk(true), [card.id]);
  const bg = `linear-gradient(160deg, ${card.media.gradient[0]}, ${card.media.gradient[1] ?? card.media.gradient[0]})`;
  return (
    <>
      <div className="media" style={{ background: bg }}>
        {video && videoOk ? (
          <video key={card.id} src={video} poster={mediaUrl(card.media.poster_url) ?? undefined} autoPlay loop muted={muted} playsInline
                 onError={() => setVideoOk(false)} />
        ) : (
          <div className="media-fallback">{card.media.emoji}</div>
        )}
        <div className="media-shade" />
        <div className="media-top">
          {card.media.ai_generated && videoOk && <span className="pill dark">🎬 видео сгенерировано ИИ</span>}
          {card.media.credit && videoOk && (
            <a className="pill dark credit" href={card.media.credit.source_url} target="_blank" rel="noopener noreferrer"
               title={`${card.media.credit.author} · ${card.media.credit.license}${card.media.credit.note ? " · " + card.media.credit.note : ""}`}>
              📹 {card.media.credit.author} · {card.media.credit.license}
            </a>
          )}
          {video && videoOk && !card.media.credit && <button className="pill dark" onClick={() => setMuted(!muted)}>{muted ? "🔇" : "🔊"}</button>}
        </div>
        <div className="media-bottom">
          <div className="badges">{card.badges.map((b) => <span key={b} className="pill light">{b}</span>)}</div>
          <h3 className="dest-title">{card.title}</h3>
          <p className="dest-sub">{card.subtitle}</p>
          <div className="facts overlay">
            {card.facts.map((f) => <span key={f.label}>{f.icon} {f.label}</span>)}
          </div>
          <div className="price-row">
            <div><b>≈ {rub(card.price)}</b><small>{card.price_note}</small></div>
            <button className="pill light" onClick={() => setOpen(!open)}>{open ? "Скрыть" : "Почему это?"}</button>
          </div>
        </div>
      </div>
      {open && (
        <div className="explain-sheet card-scroll">
          <Explain card={card} />
          {Array.isArray(card.data.highlights) && (
            <div className="ex-block">
              <h4>Что посмотреть</h4>
              <ul>{(card.data.highlights as string[]).map((h) => <li key={h}>{h}</li>)}</ul>
            </div>
          )}
        </div>
      )}
    </>
  );
}

function OptionBody({ card }: { card: Card }) {
  const bg = `linear-gradient(135deg, ${card.media.gradient[0]}, ${card.media.gradient[1] ?? card.media.gradient[0]})`;
  const video = mediaUrl(card.media.video_url);
  return (
    <div className="option">
      <div className={`option-head${video ? " with-video" : ""}`} style={{ background: bg }}>
        {video && <video key={card.media.video_url} src={video} autoPlay loop muted playsInline aria-hidden="true" />}
        {video && <div className="option-shade" />}
        {card.media.credit && (
          <a className="pill dark credit option-credit" href={card.media.credit.source_url} target="_blank" rel="noopener noreferrer">
            📹 {card.media.credit.author} · {card.media.credit.license}
          </a>
        )}
        <span className="option-emoji">{card.media.emoji}</span>
        <div>
          <h3>{card.title}</h3>
          <p>{card.subtitle}</p>
        </div>
        <span className="score" title="Насколько подходит под ваши приоритеты">{card.score}%</span>
      </div>
      <div className="card-scroll">
        <div className="facts grid">
          {card.facts.map((f) => <div key={f.label}><span>{f.icon}</span>{f.label}</div>)}
        </div>
        <div className="price-line">
          <b>{rub(card.price)}</b>
          <small>{card.price_note}</small>
        </div>
        <Explain card={card} />
        {card.badges.length > 0 && <div className="badges muted">{card.badges.map((b) => <span key={b} className="pill">{b}</span>)}</div>}
      </div>
    </div>
  );
}

function Explain({ card }: { card: Card }) {
  return (
    <div className="explain">
      <div className="ex-block why"><h4>Почему подходит</h4><ul>{card.why.map((x) => <li key={x}>{x}</li>)}</ul></div>
      {card.tradeoffs.length > 0 && <div className="ex-block trade"><h4>Чем пожертвовали</h4><ul>{card.tradeoffs.map((x) => <li key={x}>{x}</li>)}</ul></div>}
      {card.risks.length > 0 && <div className="ex-block risk"><h4>Риски</h4><ul>{card.risks.map((x) => <li key={x}>{x}</li>)}</ul></div>}
    </div>
  );
}
