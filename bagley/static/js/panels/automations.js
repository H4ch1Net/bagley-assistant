// Automations: reminders, scheduled tasks, page watchers and the kinds feature modules add
// (briefings, routines, recaps, client health checks).

import { api } from "../api.js";
import { state } from "../state.js";
import { confirmDialog, toast } from "../ui.js";
import { debounce, el, icon, relTime } from "../util.js";
import { field, header, keepFocus } from "../settings-kit.js";

/** Every kind the form can create: [id, icon, label, explanation, defaults]. */
export const KINDS = [
  { id: "task", icon: "calendar-clock", label: "Task", help: "Bagley does something on a schedule and reports back.", when: "weekdays at 08:00", name: "e.g. Morning briefing", prompt: "Instructions", placeholder: "e.g. Give me today's weather for Porto and the top 3 tech headlines." },
  { id: "reminder", icon: "alarm-clock", label: "Reminder", help: "A message at a set time. No model needed.", when: "in 30 minutes", name: "e.g. Stretch", prompt: "Message", placeholder: "e.g. Stand up and stretch." },
  { id: "watch", icon: "eye", label: "Watch page", help: "Check a web page and tell you when it changes.", when: "every 1 hour", name: "e.g. Laptop price", prompt: "When it changes (optional)", placeholder: "Optional: what to do when it changes, e.g. tell me if the price drops below 800.", target: { label: "Page URL", placeholder: "https://…" } },
];
const ICONS = { task: "calendar-clock", reminder: "alarm-clock", watch: "eye" };

/** Feature modules register more kinds (and their icons) here. */
export function addKind(kind) {
  if (!KINDS.some((k) => k.id === kind.id)) KINDS.push(kind);
  ICONS[kind.id] = kind.icon;
}

let box = null;

export function render(panel, ctx) {
  ctx.newKind ||= "task";
  const kind = KINDS.find((k) => k.id === ctx.newKind) || KINDS[0];
  const name = el("input", { class: "input", placeholder: kind.name, maxlength: 80 });
  const prompt = kind.prompt ? el("textarea", { class: "textarea", rows: 3, maxlength: 4000, placeholder: kind.placeholder }) : null;
  const target = kind.target?.options
    ? el("select", { class: "select" }, ...kind.target.options().map(([v, label]) => el("option", { value: v, text: label })))
    : kind.target ? el("input", { class: "input mono", placeholder: kind.target.placeholder, spellcheck: "false" }) : null;
  const when = el("input", { class: "input", id: "auto-when", list: "schedule-presets", value: kind.when });
  const draft = ctx.automationDraft || {};
  name.value = draft.name || "";
  if (prompt) prompt.value = draft.prompt || "";
  if (target && draft.target) target.value = draft.target;
  const presets = el("datalist", { id: "schedule-presets" },
    ...["in 30 minutes", "at 18:00", "every 1 hour", "every 6 hours", "daily at 08:00", "weekdays at 09:00", "weekends at 10:00", "mondays at 09:00", "fridays at 17:00"].map((v) => el("option", { value: v })));
  const hint = el("div", { class: "help", "aria-live": "polite" });
  let previewSeq = 0;
  const check = debounce(async () => {
    const seq = ++previewSeq;
    try {
      const p = await api.get(`/api/automations/preview?kind=${kind.id}&schedule=${encodeURIComponent(when.value)}`);
      if (seq !== previewSeq) return;
      hint.textContent = `${p.description} · next ${relTime(p.next_run)}`;
      hint.classList.remove("error-text");
    } catch (err) {
      if (seq !== previewSeq) return;
      hint.textContent = err.message;
      hint.classList.add("error-text");
    }
  }, 250);
  when.addEventListener("input", check);
  check();
  const create = el("button", { class: "btn btn-primary", type: "button" }, icon("plus", "icon-sm"), "Create");
  create.addEventListener("click", async () => {
    create.disabled = true;
    try {
      const text = prompt ? prompt.value.trim() : "";
      await api.post("/api/automations", { kind: kind.id, name: name.value.trim() || kind.defaultName || "", prompt: text, schedule: when.value, target: target ? target.value.trim() : null });
      toast(kind.id === "reminder" ? "Reminder set" : kind.id === "watch" ? "Watching the page" : "Automation created");
      ctx.automationDraft = null;
      await ctx.onAutomationsChanged();
      ctx.refresh();
    } catch (err) {
      toast(err.message, { type: "error" });
      create.disabled = false;
    }
  });

  const seg = el("div", { class: "segmented", role: "group", "aria-label": "Kind" },
    ...KINDS.map((k) => el("button", { type: "button", "aria-pressed": String(kind.id === k.id), onclick: () => {
      ctx.automationDraft = { name: name.value, prompt: prompt?.value || draft.prompt || "", target: target && !kind.target?.options ? target.value : draft.target || "" };
      ctx.newKind = k.id;
      ctx.refresh();
    } }, icon(k.icon, "icon-sm"), k.label)));

  box = el("div", { class: "section" });
  header(panel, "Automations", ["Bagley works on its own while it's running: reminders, scheduled tasks, page watchers and more. Results arrive as chats and notifications, on the desktop and your phone if you set that up. You can also just ask, e.g. ", el("em", { text: "“every weekday at 8, brief me on the weather and news”" }), "."]);
  panel.append(
    state.automations.length ? box : null,
    el("div", { class: "section" },
      el("div", { class: "section-title", text: "New" }),
      el("div", { class: "field" }, el("div", { style: "overflow-x:auto" }, seg), el("div", { class: "help", text: kind.help })),
      el("div", { class: "field-row" }, field("Name", name), el("div", { class: "field" }, el("label", { for: "auto-when", text: "When" }), when, hint)),
      presets,
      target ? field(kind.target.label, target) : null,
      prompt ? field(kind.prompt, prompt) : null,
      el("div", { class: "inline" }, create, kind.id === "task" ? el("span", { class: "help", text: "Unattended runs can't use tools that need approval." }) : kind.note ? el("span", { class: "help", text: kind.note }) : null),
    ),
  );
  if (!box.parentNode) panel.append(box);
  renderList(ctx);
}

/** Redraw only the list, so background updates never wipe a half-filled form. */
export function renderList(ctx) {
  if (!box?.isConnected) return;
  const items = state.automations;
  keepFocus(box, () => box.replaceChildren(
    el("div", { class: "section-title", text: `Your automations · ${items.length}` }),
    el("div", { class: "list" }, ...(items.length ? items.map((a) => row(ctx, a)) : [el("div", { class: "list-empty", text: "Nothing scheduled yet." })])),
  ));
}

function row(ctx, a) {
  const next = a.running ? "running now…" : !a.enabled ? (a.state?.moved_to ? "moved to runner" : a.next_run ? "paused" : "done") : `next ${relTime(a.next_run)}`;
  const last = a.last_run ? `${a.last_status === "error" ? "Failed" : "Last run"} ${relTime(a.last_run)}${a.last_result ? `: ${a.last_result}` : ""}` : "Hasn't run yet";
  const toggle = el("input", { type: "checkbox", role: "switch", checked: a.enabled, "aria-label": `Enable ${a.name}`, disabled: !a.enabled && !a.next_run && a.schedule.startsWith("at ") });
  toggle.addEventListener("change", async () => {
    try {
      await api.patch(`/api/automations/${a.id}`, { enabled: toggle.checked });
      await ctx.onAutomationsChanged();
    } catch (err) {
      toggle.checked = !toggle.checked;
      toast(err.message, { type: "error" });
    }
  });
  const run = el("button", { class: "icon-btn icon-btn-sm", type: "button", title: "Run now", "aria-label": `Run ${a.name} now`, "aria-disabled": String(a.running) }, icon("play", "icon-sm"));
  run.addEventListener("click", async () => {
    if (run.getAttribute("aria-disabled") === "true") return;
    run.setAttribute("aria-disabled", "true");
    await api.post(`/api/automations/${a.id}/run`).catch((err) => {
      run.setAttribute("aria-disabled", "false");
      toast(err.message, { type: "error" });
    });
  });
  const remove = el("button", { class: "icon-btn icon-btn-sm", type: "button", title: "Delete", "aria-label": `Delete ${a.name}` }, icon("trash-2", "icon-sm"));
  remove.addEventListener("click", async () => {
    if (!(await confirmDialog({ title: `Delete “${a.name}”?`, message: "It won't run again. Its chat stays in your history.", confirm: "Delete", danger: true }))) return;
    await api.del(`/api/automations/${a.id}`).catch((err) => toast(err.message, { type: "error" }));
    await ctx.onAutomationsChanged();
  });
  const openChat = a.conversation_id
    ? el("button", { class: "icon-btn icon-btn-sm", type: "button", title: "Open chat", "aria-label": `Open chat for ${a.name}`, onclick: () => { ctx.dialog.close(); ctx.openChat(a.conversation_id); } }, icon("message-square", "icon-sm"))
    : null;
  const extra = ROW_ACTIONS.map((make) => make(ctx, a)).filter(Boolean);
  const failed = a.last_status === "error" || a.last_status === "crit";
  return el("div", { class: `list-item automation${a.enabled ? "" : " off"}`, dataset: { id: a.id } },
    el("span", { class: "tool-icon" }, a.running ? el("span", { class: "spinner" }) : icon(ICONS[a.kind] || "calendar-clock", "icon-sm")),
    el("div", { class: "grow" },
      el("div", { class: "name" }, el("span", { class: "auto-name", text: a.name }), el("span", { class: "badge", text: a.schedule_text }), el("span", { class: "subtle", style: "font-weight:400", text: next })),
      a.target ? el("div", { class: "desc mono", text: a.target }) : null,
      el("div", { class: `desc${failed ? " error-text" : ""}`, text: last }),
    ),
    el("div", { class: "inline", style: "gap:2px" }, run, openChat, ...extra, remove),
    el("label", { class: "switch" }, toggle, el("span")),
  );
}

/** Extra per-automation actions from feature modules (e.g. move to the runner). */
export const ROW_ACTIONS = [];
