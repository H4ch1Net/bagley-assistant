// Memory: facts Bagley keeps between conversations.

import { api } from "../api.js";
import { state } from "../state.js";
import { confirmDialog, toast } from "../ui.js";
import { $, el, icon } from "../util.js";
import { header, section } from "../settings-kit.js";

export function render(panel, ctx) {
  const input = el("input", { class: "input", placeholder: "e.g. I'm vegetarian", maxlength: 500, "aria-label": "New memory" });
  const add = async () => {
    const content = input.value.trim();
    if (!content) return input.focus();
    try {
      await api.post("/api/memories", { content });
      input.value = "";
      await ctx.onMemoriesChanged();
      ctx.refresh();
      $("#settings-panel input")?.focus();
    } catch (err) {
      toast(err.message, { type: "error" });
    }
  };
  input.addEventListener("keydown", (e) => e.key === "Enter" && add());
  const items = state.memories.map((m) => el("div", { class: "list-item" },
    el("span", { class: "subtle mono", style: "font-size:.74rem;margin-top:2px", text: `#${String(m.id).padStart(3, "0")}` }),
    el("div", { class: "grow", text: m.content }),
    el("button", {
      class: "icon-btn icon-btn-sm", type: "button", "aria-label": `Forget: ${m.content}`, title: "Forget",
      onclick: async () => {
        await api.del(`/api/memories/${m.id}`).catch((err) => toast(err.message, { type: "error" }));
        await ctx.onMemoriesChanged();
        ctx.refresh();
        toast("Forgotten", {
          action: { label: "Undo", run: async () => { await api.post("/api/memories", { content: m.content }); await ctx.onMemoriesChanged(); ctx.refresh(); } },
        });
      },
    }, icon("trash-2", "icon-sm")),
  ));
  const clear = el("button", {
    class: "btn btn-sm btn-danger", type: "button", disabled: !state.memories.length,
    onclick: async () => {
      if (!(await confirmDialog({ title: "Forget everything?", message: `Bagley will forget all ${state.memories.length} saved memories. This can't be undone.`, confirm: "Forget all", danger: true }))) return;
      await Promise.all(state.memories.map((m) => api.del(`/api/memories/${m.id}`)));
      await ctx.onMemoriesChanged();
      ctx.refresh();
    },
  }, "Forget all");
  header(panel, "Memory", "Facts Bagley keeps between conversations. It saves them when you share something lasting, or when you ask it to remember. Everything here goes into each conversation's context.");
  panel.append(
    section("", el("div", { class: "inline" }, input, el("button", { class: "btn", type: "button", onclick: add }, icon("plus", "icon-sm"), "Add"))),
    section("",
      el("div", { class: "list" }, ...(items.length ? items : [el("div", { class: "list-empty", text: "Nothing remembered yet. Try “Remember that I prefer metric units.”" })])),
      state.memories.length ? el("div", { style: "margin-top:12px" }, clear) : null,
    ),
  );
}
