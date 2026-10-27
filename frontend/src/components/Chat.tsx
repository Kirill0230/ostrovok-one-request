import { useEffect, useRef, useState } from "react";
import type { ChatMessage } from "../types";

interface Props {
  messages: ChatMessage[];
  quick: string[];
  busy: boolean;
  onSend: (text: string) => void;
}

export default function Chat({ messages, quick, busy, onSend }: Props) {
  const [text, setText] = useState("");
  const endRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: "smooth", block: "end" });
  }, [messages, busy]);

  const submit = (e?: React.FormEvent) => {
    e?.preventDefault();
    if (!text.trim() || busy) return;
    onSend(text);
    setText("");
  };

  return (
    <div className="chat">
      <div className="chat-scroll">
        {messages.map((m) => (
          <div key={m.id} className={`msg ${m.role}`}>
            <div className="bubble">{m.text}</div>
            {m.steps && m.steps.length > 0 && (
              <details className="steps">
                <summary>🔧 Что сделал агент · {m.steps.length}</summary>
                <ul>
                  {m.steps.map((s, i) => (
                    <li key={i}><code>{s.tool}</code> {s.summary}</li>
                  ))}
                </ul>
              </details>
            )}
          </div>
        ))}
        {busy && (
          <div className="msg agent">
            <div className="bubble typing"><span /><span /><span /></div>
          </div>
        )}
        <div ref={endRef} />
      </div>
      <QuickReplies quick={quick} busy={busy} onSend={onSend} />
      <form className="composer" onSubmit={submit}>
        <input value={text} onChange={(e) => setText(e.target.value)} disabled={busy}
               placeholder="Например: к морю в ноябре с ребёнком, до 200 тысяч" />
        <button type="submit" className="primary" disabled={busy || !text.trim()} aria-label="Отправить">➤</button>
      </form>
    </div>
  );
}

export function QuickReplies({ quick, busy, onSend }: { quick: string[]; busy: boolean; onSend: (t: string) => void }) {
  if (!quick.length) return null;
  return (
    <div className="quick">
      {quick.map((q) => (
        <button key={q} className="chip" disabled={busy} onClick={() => onSend(q)}>{q}</button>
      ))}
    </div>
  );
}
