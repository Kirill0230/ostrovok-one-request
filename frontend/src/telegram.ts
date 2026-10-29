const STORAGE_KEY = "odin_tg_init_data";

export function telegramInitData(): string | null {
  try {
    const params = new URLSearchParams(window.location.hash.slice(1));
    const fromHash = params.get("tgWebAppData");
    if (fromHash) {
      sessionStorage.setItem(STORAGE_KEY, fromHash);
      return fromHash;
    }
    return sessionStorage.getItem(STORAGE_KEY);
  } catch {
    return null;
  }
}

function postEvent(eventType: string, eventData: Record<string, unknown> = {}) {
  const w = window as any;
  try {
    if (w.TelegramWebviewProxy?.postEvent) {
      w.TelegramWebviewProxy.postEvent(eventType, JSON.stringify(eventData));
    } else if (w.external?.notify) {
      w.external.notify(JSON.stringify({ eventType, eventData }));
    } else if (window.parent !== window) {
      window.parent.postMessage(JSON.stringify({ eventType, eventData }), "https://web.telegram.org");
    }
  } catch {
  }
}

export function telegramReady() {
  if (!telegramInitData()) return;
  postEvent("web_app_ready");
  postEvent("web_app_expand");
}
