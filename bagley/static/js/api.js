// REST helpers and the reconnecting chat socket.

import { Emitter } from "./util.js";

export class ApiError extends Error {
  constructor(message, status, hint = "") {
    super(message);
    this.status = status;
    this.hint = hint;
  }
}

async function request(method, path, body) {
  const init = { method, headers: {} };
  if (body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    res = await fetch(path, init);
  } catch {
    throw new ApiError("Bagley's server isn't responding. Is it still running?", 0);
  }
  if (res.status === 204) return null;
  const type = res.headers.get("content-type") || "";
  const data = type.includes("json") ? await res.json() : await res.text();
  if (!res.ok) {
    const detail = typeof data === "object" ? data.detail || data.error : data;
    throw new ApiError(typeof detail === "string" ? detail : `Request failed (${res.status})`, res.status, data?.hint);
  }
  return data;
}

export const api = {
  get: (path) => request("GET", path),
  post: (path, body) => request("POST", path, body ?? {}),
  put: (path, body) => request("PUT", path, body),
  patch: (path, body) => request("PATCH", path, body),
  del: (path) => request("DELETE", path),

  /** POST and read a newline-delimited JSON stream, calling `onEvent` per line. */
  async stream(path, body, onEvent) {
    const res = await fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    if (!res.ok || !res.body) {
      const text = await res.text();
      let message = text;
      try {
        message = JSON.parse(text).detail || text;
      } catch {
        /* Plain text error. */
      }
      throw new ApiError(message || `Request failed (${res.status})`, res.status);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let nl;
      while ((nl = buffer.indexOf("\n")) >= 0) {
        const line = buffer.slice(0, nl).trim();
        buffer = buffer.slice(nl + 1);
        if (line) onEvent(JSON.parse(line));
      }
    }
  },
};

/** WebSocket that reconnects with backoff and re-emits server events by type. */
export class ChatSocket extends Emitter {
  constructor(url) {
    super();
    this.url = url;
    this.ws = null;
    this.delay = 500;
    this.open = false;
    this.connect();
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden && !this.open) this.reconnectNow();
    });
  }

  connect() {
    clearTimeout(this.timer);
    const ws = new WebSocket(this.url);
    this.ws = ws;
    ws.onopen = () => {
      this.open = true;
      this.delay = 500;
      this.emit("open");
    };
    ws.onmessage = (msg) => {
      let event;
      try {
        event = JSON.parse(msg.data);
      } catch {
        return;
      }
      this.emit(event.type, event);
    };
    ws.onclose = () => {
      if (this.ws !== ws) return;
      const wasOpen = this.open;
      this.open = false;
      if (wasOpen) this.emit("close");
      this.timer = setTimeout(() => this.connect(), this.delay);
      this.delay = Math.min(this.delay * 1.8, 6000);
    };
  }

  reconnectNow() {
    this.delay = 500;
    if (this.ws && this.ws.readyState === WebSocket.CONNECTING) return;
    this.connect();
  }

  send(message) {
    if (!this.open) return false;
    this.ws.send(JSON.stringify(message));
    return true;
  }
}
