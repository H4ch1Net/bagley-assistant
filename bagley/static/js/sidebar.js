// Conversation list: grouping by date, search, rename, delete with undo.

import { api } from "./api.js";
import { state } from "./state.js";
import { toast } from "./ui.js";
import { $, dateBucket, debounce, el, icon } from "./util.js";

export class Sidebar {
  constructor({ onOpen, onDeletedActive }) {
    this.onOpen = onOpen;
    this.onDeletedActive = onDeletedActive;
    this.search = $("#search");
    this.search.addEventListener("input", debounce(() => this.refresh(), 180));
    this.search.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && this.search.value) {
        e.stopPropagation();
        this.search.value = "";
        this.refresh();
      } else if (e.key === "ArrowDown") {
        e.preventDefault();
        $("#conv-list .conv-link")?.focus();
      }
    });
    $("#conv-list").addEventListener("keydown", (e) => {
      if (!["ArrowDown", "ArrowUp"].includes(e.key) || !e.target.classList.contains("conv-link")) return;
      e.preventDefault();
      const links = [...document.querySelectorAll("#conv-list .conv-link")];
      const i = links.indexOf(e.target);
      (e.key === "ArrowDown" ? links[i + 1] : links[i - 1] || this.search).focus();
    });
  }

  async refresh() {
    state.query = this.search.value.trim();
    const q = state.query ? `?q=${encodeURIComponent(state.query)}` : "";
    const seq = (this.seq = (this.seq || 0) + 1);
    try {
      const list = await api.get(`/api/conversations${q}`);
      if (seq !== this.seq) return; // A newer refresh or local change superseded this one.
      state.conversations = list;
    } catch (err) {
      if (seq === this.seq) toast(err.message, { type: "error" });
    }
    this.render();
  }

  upsert(conv) {
    this.seq = (this.seq || 0) + 1; // Invalidate in-flight refreshes that predate this change.
    const i = state.conversations.findIndex((c) => c.id === conv.id);
    if (i >= 0) state.conversations[i] = { ...state.conversations[i], ...conv };
    else state.conversations.unshift(conv);
    this.render();
  }

  /** Merge a partial change into a conversation that is already listed. */
  update(partial) {
    const item = state.conversations.find((c) => c.id === partial.id);
    if (!item) return;
    Object.assign(item, partial);
    this.seq = (this.seq || 0) + 1;
    this.render();
  }

  render() {
    const root = $("#conv-list");
    if (root.querySelector(".conv-rename")) {
      this.deferred = true; // Don't destroy an open rename field; render when it closes.
      return;
    }
    this.deferred = false;
    if (!state.conversations.length) {
      root.replaceChildren(
        el("div", { class: "conv-empty" }, state.query ? `No chats match “${state.query}”.` : "Your chats will appear here."),
      );
      return;
    }
    const groups = new Map();
    for (const c of state.conversations) {
      const key = dateBucket(c.updated_at);
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(c);
    }
    const nodes = [];
    for (const [label, items] of groups) {
      nodes.push(el("section", { class: "conv-group" }, el("h2", { text: label }), ...items.map((c) => this.item(c))));
    }
    root.replaceChildren(...nodes);
  }

  item(conv) {
    const active = conv.id === state.activeId;
    const running = state.run?.conversationId === conv.id;
    const link = el("button", {
      class: "conv-link",
      type: "button",
      title: conv.title,
      "aria-current": active ? "page" : null,
      onclick: () => this.onOpen(conv.id),
    }, conv.title);
    const node = el("div", { class: `conv-item${active ? " active" : ""}`, dataset: { id: conv.id } },
      link,
      running ? el("span", { class: "spinner running", title: "Working…" }) : null,
      el("div", { class: "conv-actions" },
        el("button", { class: "icon-btn icon-btn-sm", type: "button", "aria-label": `Rename ${conv.title}`, title: "Rename", onclick: () => this.rename(conv, node) }, icon("pencil", "icon-sm")),
        el("button", { class: "icon-btn icon-btn-sm", type: "button", "aria-label": `Delete ${conv.title}`, title: "Delete", onclick: () => this.remove(conv) }, icon("trash-2", "icon-sm")),
      ),
    );
    return node;
  }

  rename(conv, node) {
    const input = el("input", { class: "conv-rename", value: conv.title, "aria-label": "Chat title", maxlength: 120 });
    node.replaceChildren(input);
    input.focus();
    input.select();
    let done = false;
    const finish = async (save) => {
      if (done) return;
      done = true;
      const title = input.value.trim();
      input.remove(); // Lets render() run again.
      if (save && title && title !== conv.title) {
        try {
          this.upsert(await api.patch(`/api/conversations/${conv.id}`, { title }));
          document.dispatchEvent(new CustomEvent("bagley:renamed", { detail: { id: conv.id, title } }));
          return;
        } catch (err) {
          toast(err.message, { type: "error" });
        }
      }
      this.render();
    };
    input.addEventListener("keydown", (e) => {
      if (e.key === "Enter") finish(true);
      if (e.key === "Escape") { e.stopPropagation(); finish(false); }
    });
    input.addEventListener("blur", () => finish(true));
  }

  async remove(conv) {
    if (state.run?.conversationId === conv.id) {
      toast("Stop the current reply before deleting this chat.", { type: "error" });
      return;
    }
    try {
      await api.del(`/api/conversations/${conv.id}`);
    } catch (err) {
      toast(err.message, { type: "error" });
      return;
    }
    const wasActive = conv.id === state.activeId;
    state.conversations = state.conversations.filter((c) => c.id !== conv.id);
    this.render();
    if (wasActive) this.onDeletedActive();
    toast(`Deleted “${conv.title}”`, {
      action: {
        label: "Undo",
        run: async () => {
          await api.post(`/api/conversations/${conv.id}/restore`);
          await this.refresh();
          if (wasActive) this.onOpen(conv.id);
        },
      },
    });
  }
}
