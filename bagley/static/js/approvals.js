// Approvals waiting outside the open chat: from the desktop overlay, the shell, the phone,
// an automation or another window. Any client can answer; the first answer wins.

import { api } from "./api.js";
import { state } from "./state.js";
import { announce, toast } from "./ui.js";
import { $, el, icon } from "./util.js";

export class Approvals {
  constructor({ isShownInThread, openChat }) {
    this.isShownInThread = isShownInThread;
    this.openChat = openChat;
    this.items = new Map();
  }

  async load() {
    try {
      const list = await api.get("/api/approvals");
      this.items = new Map(list.map((a) => [a.id, a]));
    } catch {
      this.items = new Map();
    }
    this.render();
  }

  add(item) {
    this.items.set(item.id, item);
    this.render();
    if (!this.isShownInThread(item.id)) announce(`Approval needed: ${item.summary}`);
  }

  remove(id) {
    this.items.delete(id);
    this.render();
  }

  async decide(item, decision) {
    try {
      await api.post(`/api/approvals/${encodeURIComponent(item.id)}`, { decision });
      this.remove(item.id);
    } catch (err) {
      if (err.status === 404) this.remove(item.id);
      else toast(err.message, { type: "error" });
    }
  }

  render() {
    const box = $("#approvals");
    const items = [...this.items.values()].filter((a) => !this.isShownInThread(a.id));
    state.approvals = [...this.items.values()];
    box.hidden = !items.length;
    box.replaceChildren(...items.map((a) => el("div", { class: "row", role: "group", "aria-label": `Approval for ${a.tool}` },
      el("span", { class: "code", text: "AWAIT" }),
      el("span", { class: "what", title: JSON.stringify(a.arguments) }, a.summary),
      el("span", { class: "from", text: `${a.tool} · ${a.source}` }),
      a.conversation_id && a.conversation_id !== state.activeId
        ? el("button", { class: "btn btn-sm btn-ghost", type: "button", onclick: () => this.openChat(a.conversation_id) }, icon("message-square", "icon-xs"), "Open")
        : null,
      el("button", { class: "btn btn-sm", type: "button", text: "Deny", onclick: () => this.decide(a, "deny") }),
      el("button", { class: "btn btn-sm btn-primary", type: "button", text: "Allow", onclick: () => this.decide(a, "allow") }),
    )));
  }
}
