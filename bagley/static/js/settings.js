// Settings dialog. Server preferences save immediately; appearance and voice live in the browser.

import { api } from "./api.js";
import { bus, setUi, state } from "./state.js";
import { confirmDialog, keepToasts, toast } from "./ui.js";
import { $, debounce, el, formatBytes, icon, relTime } from "./util.js";
import { voice } from "./voice.js";

const ENV_NAMES = {
  provider: "BAGLEY_PROVIDER", base_url: "BAGLEY_BASE_URL", api_key: "BAGLEY_API_KEY", model: "BAGLEY_MODEL",
  temperature: "BAGLEY_TEMPERATURE", context_tokens: "BAGLEY_CONTEXT_TOKENS", max_steps: "BAGLEY_MAX_STEPS",
  tool_mode: "BAGLEY_TOOL_MODE", think: "BAGLEY_THINK", persona: "BAGLEY_PERSONA",
};

export const RECOMMENDED_MODELS = [
  { name: "qwen3:4b", note: "Fast, good at tools · 2.5 GB" },
  { name: "llama3.2:3b", note: "Small and quick · 2.0 GB" },
  { name: "qwen3:8b", note: "Stronger reasoning · 5.2 GB" },
  { name: "gpt-oss:20b", note: "Best quality, needs 16 GB RAM · 14 GB" },
];

const ACCENTS = [
  { h: 188, name: "Signal cyan" },
  { h: 152, name: "Mint" },
  { h: 38, name: "Amber" },
  { h: 262, name: "Violet" },
  { h: 340, name: "Rose" },
  { h: 214, name: "Azure" },
];

const TABS = [
  { id: "general", label: "General", icon: "sparkles" },
  { id: "model", label: "Model", icon: "cpu" },
  { id: "automations", label: "Automations", icon: "calendar-clock" },
  { id: "skills", label: "Skills", icon: "graduation-cap" },
  { id: "telegram", label: "Telegram", icon: "send" },
  { id: "tools", label: "Tools", icon: "wrench" },
  { id: "knowledge", label: "Knowledge", icon: "library" },
  { id: "memory", label: "Memory", icon: "bookmark" },
  { id: "voice", label: "Voice", icon: "volume-2" },
  { id: "appearance", label: "Appearance", icon: "sun" },
];

const locked = (key) => state.prefs?.locked.includes(key);
const lockedBy = (key) => state.prefs?.locked_by?.[key] || ENV_NAMES[key];
const value = (key) => state.prefs?.values[key];

export async function savePrefs(changes) {
  try {
    state.prefs = await api.put("/api/preferences", changes);
    bus.emit("prefs");
    flashSaved();
    return true;
  } catch (err) {
    toast(err.message, { type: "error" });
    return false;
  }
}

function flashSaved() {
  const node = $("#save-state");
  if (!node) return;
  node.replaceChildren(icon("check", "icon-xs"), "Saved");
  clearTimeout(flashSaved.timer);
  flashSaved.timer = setTimeout(() => node.replaceChildren(), 1600);
}

let fieldSeq = 0;

function field(label, control, { help, key, id } = {}) {
  const lockNote = key && locked(key) ? el("span", { class: "lock", title: `Locked by the ${lockedBy(key)} environment variable` }, icon("lock", "icon-xs"), lockedBy(key)) : null;
  if (key && locked(key)) control.disabled = true;
  // Label the form control itself, even when it sits in a wrapper next to a button.
  const target = control.matches("input, select, textarea") ? control : control.querySelector("input, select, textarea");
  if (target) target.id = id || target.id || `field-${++fieldSeq}`;
  return el("div", { class: "field" },
    el("label", { for: target?.id || null }, label, lockNote),
    control,
    help ? el("div", { class: "help", text: help }) : null,
  );
}

/** Redraw `box` and give focus back to the same control if it had it. */
function keepFocus(box, redraw) {
  const active = document.activeElement;
  const name = (n) => n.getAttribute("aria-label") || n.textContent.trim();
  const label = box.contains(active) && active !== box ? name(active) : null;
  redraw();
  if (label) [...box.querySelectorAll("button, input, select")].find((n) => name(n) === label && !n.disabled)?.focus();
}

function toggleRow(title, help, checked, onChange, { key, label } = {}) {
  const input = el("input", { type: "checkbox", role: "switch", "aria-label": label || title });
  input.checked = Boolean(checked);
  if (key && locked(key)) input.disabled = true;
  input.addEventListener("change", () => onChange(input.checked));
  return el("div", { class: "toggle-row" },
    el("div", { class: "text" }, el("strong", { text: title }), help ? el("span", { class: "help", text: help }) : null),
    el("label", { class: "switch" }, input, el("span")),
  );
}

function select(options, current, onChange) {
  const node = el("select", { class: "select" }, ...options.map(([v, label]) => el("option", { value: v, selected: String(v) === String(current) }, label)));
  node.addEventListener("change", () => onChange(node.value));
  return node;
}

export class Settings {
  constructor({ onModelsChanged, onMemoriesChanged, onSkillsChanged, onAutomationsChanged, openChat }) {
    this.dialog = $("#settings-dialog");
    this.onModelsChanged = onModelsChanged;
    this.onMemoriesChanged = onMemoriesChanged;
    this.onSkillsChanged = onSkillsChanged;
    this.onAutomationsChanged = onAutomationsChanged;
    this.openChat = openChat;
    this.tab = "general";
    this.dialog.addEventListener("close", () => (this.isOpen = false));
    voice.onVoicesChanged(() => this.fillVoices?.());
  }

  open(tab = this.tab) {
    this.tab = tab;
    if (!this.dialog.open) this.dialog.showModal();
    this.isOpen = true;
    this.renderFrame();
  }

  renderFrame() {
    const tabs = el("div", { class: "tabs", role: "tablist", "aria-orientation": "vertical" },
      ...TABS.map((t) => el("button", {
        class: "tab", role: "tab", type: "button", id: `tab-${t.id}`,
        "aria-selected": String(t.id === this.tab), "aria-controls": "settings-panel",
        tabindex: t.id === this.tab ? "0" : "-1",
        onclick: () => { this.tab = t.id; this.renderFrame(); $(`#tab-${t.id}`)?.focus(); },
      }, icon(t.icon, "icon-sm"), t.label)),
    );
    tabs.addEventListener("keydown", (e) => {
      if (!["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight"].includes(e.key)) return;
      e.preventDefault();
      const i = TABS.findIndex((t) => t.id === this.tab);
      const next = TABS[(i + (e.key === "ArrowDown" || e.key === "ArrowRight" ? 1 : TABS.length - 1)) % TABS.length];
      this.tab = next.id;
      this.renderFrame();
      $(`#tab-${next.id}`)?.focus();
    });
    const panel = el("section", { class: "panel", id: "settings-panel", role: "tabpanel", "aria-labelledby": `tab-${this.tab}` });
    this.dialog.replaceChildren(
      el("div", { class: "dialog-head" },
        el("h2", { id: "settings-title", text: "Settings" }),
        el("div", { class: "inline" },
          el("span", { class: "save-state", id: "save-state", "aria-live": "polite" }),
          el("button", { class: "icon-btn", type: "button", "aria-label": "Close settings", onclick: () => this.dialog.close() }, icon("x")),
        ),
      ),
      el("div", { class: "settings" }, tabs, panel),
    );
    keepToasts();
    this[`render_${this.tab}`](panel);
  }

  /** Re-render the open dialog, keeping scroll position and keyboard focus. */
  refresh() {
    if (!this.dialog.open) return;
    const controls = () => [...this.dialog.querySelectorAll("button, input, select, textarea")];
    const focused = controls().indexOf(document.activeElement);
    const scroll = $("#settings-panel")?.scrollTop;
    this.renderFrame();
    if (scroll) $("#settings-panel").scrollTop = scroll;
    if (focused >= 0) controls()[focused]?.focus({ preventScroll: true });
  }

  // General ---------------------------------------------------------------------------------

  render_general(panel) {
    const personas = state.prefs.personas;
    const grid = el("div", { class: "choice-grid", role: "radiogroup", "aria-label": "Personality" },
      ...Object.entries(personas).map(([id, p]) => {
        const input = el("input", { type: "radio", name: "persona", value: id, checked: value("persona") === id, disabled: locked("persona") });
        input.addEventListener("change", () => savePrefs({ persona: id }));
        return el("label", { class: "choice" }, input, el("strong", { text: p.label }), el("span", { text: p.description }));
      }),
    );
    const instructions = el("textarea", { class: "textarea", rows: 5, maxlength: 4000, placeholder: "e.g. Call me Sam. I live in Leeds. Prefer short answers with examples in Python." });
    instructions.value = value("custom_instructions") || "";
    const counter = el("span", { class: "help" });
    const updateCount = () => (counter.textContent = `${instructions.value.length} / 4000`);
    updateCount();
    instructions.addEventListener("input", updateCount);
    instructions.addEventListener("input", debounce(() => savePrefs({ custom_instructions: instructions.value }), 600));

    panel.append(
      el("h3", { text: "General" }),
      el("p", { class: "lead", text: "How Bagley talks to you." }),
      el("div", { class: "section" },
        el("div", { class: "field" }, el("span", { class: "field-label", text: "Personality" }, locked("persona") ? el("span", { class: "lock" }, icon("lock", "icon-xs"), ENV_NAMES.persona) : null), grid),
        field("Custom instructions", instructions, { id: "custom-instructions", help: "Added to every conversation. Bagley also keeps its own memory, see the Memory tab." }),
        counter,
      ),
      el("div", { class: "section" },
        toggleRow("Smart titles", "Ask the model for a short title after the first reply.", value("smart_titles"), (v) => savePrefs({ smart_titles: v })),
        toggleRow("Desktop notifications", "Reminders, automations and notify requests appear as system notifications while this tab is in the background.", state.ui.desktopNotify, async (v) => {
          if (v && "Notification" in window && Notification.permission !== "granted") {
            const result = await Notification.requestPermission();
            if (result !== "granted") {
              toast("Notifications are blocked for this site in your browser settings.", { type: "error" });
              this.refresh();
              return;
            }
          }
          setUi("desktopNotify", v);
        }),
      ),
    );
  }

  // Model -----------------------------------------------------------------------------------

  render_model(panel) {
    const provider = select([["auto", "Auto-detect"], ["ollama", "Ollama"], ["openai", "OpenAI-compatible"]], value("provider"), (v) => this.saveConnection({ provider: v }));
    const baseUrl = el("input", { class: "input mono", value: value("base_url"), spellcheck: "false", placeholder: "http://localhost:11434" });
    baseUrl.addEventListener("change", () => this.saveConnection({ base_url: baseUrl.value.trim() }));
    const apiKey = el("input", { class: "input mono", type: "password", autocomplete: "off", placeholder: value("has_api_key") ? "•••••••• saved" : "Not needed for local servers" });
    apiKey.addEventListener("change", () => this.saveConnection({ api_key: apiKey.value }));
    const status = el("div", { class: "status-line", "aria-live": "polite" });
    const test = el("button", { class: "btn btn-sm", type: "button", onclick: () => this.testConnection(status) }, icon("zap", "icon-sm"), "Test connection");

    const modelSelect = el("select", { class: "select mono" });
    const fillModels = () => {
      const chat = state.models.filter((m) => !/embed|minilm|bge-|e5-|gte-/i.test(m.name));
      const names = chat.map((m) => m.name);
      const current = value("model");
      const options = [el("option", { value: "", text: names.length ? `Automatic (${names[0]})` : "Automatic" })];
      for (const m of chat) options.push(el("option", { value: m.name, text: [m.name, m.parameter_size].filter(Boolean).join("  ·  ") }));
      if (current && !names.includes(current)) options.push(el("option", { value: current, text: `${current} (not installed)` }));
      modelSelect.replaceChildren(...options);
      modelSelect.value = current || "";
    };
    fillModels();
    modelSelect.addEventListener("change", () => savePrefs({ model: modelSelect.value }));
    const refresh = el("button", { class: "icon-btn", type: "button", "aria-label": "Refresh model list", title: "Refresh model list", onclick: async () => { await this.onModelsChanged(); fillModels(); } }, icon("refresh-cw", "icon-sm"));

    const temp = el("input", { class: "range", type: "range", min: 0, max: 2, step: 0.1, value: value("temperature"), "aria-label": "Temperature" });
    const tempOut = el("output", { text: Number(value("temperature")).toFixed(1) });
    temp.addEventListener("input", () => (tempOut.textContent = Number(temp.value).toFixed(1)));
    temp.addEventListener("change", () => savePrefs({ temperature: Number(temp.value) }));
    if (locked("temperature")) temp.disabled = true;

    const ctxSizes = [2048, 4096, 8192, 16384, 32768, 65536, 131072];
    const current = value("context_tokens");
    if (!ctxSizes.includes(current)) ctxSizes.push(current);
    const ctx = select(ctxSizes.sort((a, b) => a - b).map((n) => [n, `${n.toLocaleString()} tokens`]), current, (v) => savePrefs({ context_tokens: Number(v) }));

    const steps = el("input", { class: "input", type: "number", min: 1, max: 32, value: value("max_steps") });
    steps.addEventListener("change", () => savePrefs({ max_steps: Math.max(1, Math.min(32, Number(steps.value) || 8)) }));

    const toolMode = select([["auto", "Automatic"], ["native", "Native function calling"], ["prompt", "Text-based (any model)"], ["off", "Off"]], value("tool_mode"), (v) => savePrefs({ tool_mode: v }));

    panel.append(
      el("h3", { text: "Model" }),
      el("p", { class: "lead", text: "Bagley works with Ollama and any OpenAI-compatible server: LM Studio, llama.cpp, vLLM, LocalAI, Jan, or a hosted API." }),
      el("div", { class: "section" },
        el("div", { class: "section-title", text: "Server" }),
        el("div", { class: "field-row" },
          field("Server type", provider, { key: "provider", id: "pref-provider" }),
          field("Server URL", baseUrl, { key: "base_url", id: "pref-base-url", help: "Ollama 11434 · LM Studio 1234 · llama.cpp 8080" }),
        ),
        field("API key", apiKey, { key: "api_key", id: "pref-api-key", help: "Only for hosted APIs. Stored in Bagley's local database." }),
        el("div", { class: "inline" }, test),
        status,
      ),
      el("div", { class: "section" },
        el("div", { class: "section-title", text: "Generation" }),
        field("Model", el("div", { class: "inline" }, modelSelect, refresh), { key: "model", id: "pref-model" }),
        el("div", { class: "field-row" },
          field("Temperature", el("div", { class: "range-row" }, temp, tempOut), { key: "temperature", help: "Lower is more focused, higher is more creative." }),
          field("Context window", ctx, { key: "context_tokens", id: "pref-ctx", help: "Longer remembers more but uses more memory." }),
        ),
        el("div", { class: "field-row" },
          field("Tool calling", toolMode, { key: "tool_mode", id: "pref-tool-mode", help: "Text-based mode lets models without function calling use tools." }),
          field("Max steps per reply", steps, { key: "max_steps", id: "pref-steps", help: "How many tool rounds Bagley may take." }),
        ),
        toggleRow("Reasoning", "Let models that support it think before answering. Slower, often better.", value("think"), (v) => savePrefs({ think: v }), { key: "think" }),
        toggleRow("Load tools on demand", "Send the core tools every time and the rest (file changes, automations, system, code) only when a chat needs them. Saves context and keeps small models focused.", value("tool_routing"), (v) => savePrefs({ tool_routing: v })),
      ),
    );
    if (locked("model")) modelSelect.disabled = true;
    if (state.health?.provider === "ollama" || (!state.health && value("provider") !== "openai")) {
      panel.append(this.installedSection(), this.pullSection());
    }
  }

  installedSection() {
    const list = el("div", { class: "list" });
    const memory = el("div", { class: "help", "aria-live": "polite" });
    const refresh = async () => {
      const loaded = await api.get("/api/models/loaded").catch(() => []);
      const inMemory = new Map(loaded.map((m) => [m.name, m]));
      const vram = loaded.reduce((sum, m) => sum + (m.size_vram || 0), 0);
      memory.textContent = loaded.length
        ? `${loaded.length} in memory · ${formatBytes(vram)} on the GPU`
        : "Nothing in memory. A model loads on its first message and unloads after a few idle minutes.";
      const rows = state.models.map((m) => this.modelRow(m, inMemory.get(m.name), refresh));
      list.replaceChildren(...(rows.length ? rows : [el("div", { class: "list-empty", text: "No models installed." })]));
    };
    refresh();
    return el("div", { class: "section" },
      el("div", { class: "section-title", text: "Installed models" }),
      memory,
      list,
    );
  }

  modelRow(m, loaded, refresh) {
    const active = state.health?.model === m.name;
    const unload = loaded && el("button", { class: "btn btn-sm", type: "button", title: "Free its memory now" }, icon("power", "icon-xs"), "Unload");
    unload?.addEventListener("click", async () => {
      unload.disabled = true;
      try {
        await api.post("/api/models/unload", { name: m.name });
        toast(`Unloaded ${m.name}`);
      } catch (err) {
        toast(err.message, { type: "error" });
      }
      await refresh();
    });
    const remove = el("button", { class: "icon-btn icon-btn-sm", type: "button", title: "Delete", "aria-label": `Delete ${m.name}` }, icon("trash-2", "icon-sm"));
    remove.addEventListener("click", async () => {
      const ok = await confirmDialog({
        title: `Delete ${m.name}?`,
        message: `This frees ${formatBytes(m.size || 0)} of disk. You can download it again later.`,
        confirm: "Delete",
        danger: true,
      });
      if (!ok) return;
      try {
        await api.del(`/api/models/${encodeURIComponent(m.name)}`);
        toast(`Deleted ${m.name}`);
      } catch (err) {
        toast(err.message, { type: "error" });
      }
      await this.onModelsChanged();
      this.refresh();
    });
    const facts = [m.parameter_size, m.quantization, m.size ? formatBytes(m.size) : ""].filter(Boolean).join(" · ");
    return el("div", { class: "list-item model-row", dataset: { name: m.name } },
      el("span", { class: "tool-icon" }, icon(loaded ? "activity" : "hard-drive", "icon-sm")),
      el("div", { class: "grow" },
        el("div", { class: "name" }, m.name,
          active ? el("span", { class: "badge", text: "in use" }) : null,
          loaded ? el("span", { class: "badge badge-ok", text: `in memory${loaded.size_vram ? ` · ${formatBytes(loaded.size_vram)} VRAM` : ""}` }) : null),
        facts ? el("div", { class: "desc", text: facts }) : null,
      ),
      el("div", { class: "inline", style: "gap:4px" }, unload, remove),
    );
  }

  async saveConnection(changes) {
    if (await savePrefs(changes)) {
      await this.onModelsChanged();
      this.refresh();
    }
  }

  async testConnection(status) {
    status.replaceChildren(el("span", { class: "spinner" }), "Checking…");
    const health = await api.get("/api/health").catch((err) => ({ ok: false, error: err.message }));
    state.health = health;
    bus.emit("health");
    if (health.ok) {
      status.replaceChildren(el("span", { class: "dot ok" }), `Connected to ${health.provider} ${health.version || ""} · ${health.models} model${health.models === 1 ? "" : "s"}`);
    } else {
      status.replaceChildren(el("span", { class: "dot bad" }), health.error || "Not reachable");
    }
  }

  pullSection() {
    const input = el("input", { class: "input mono", placeholder: "e.g. qwen3:4b", "aria-label": "Model to download", spellcheck: "false" });
    const progress = el("div", { class: "progress", hidden: true }, el("i"));
    const statusText = el("div", { class: "pull-status" });
    const button = el("button", { class: "btn", type: "button" }, icon("hard-drive-download", "icon-sm"), "Download");
    const run = async (name) => {
      name = (name || input.value).trim();
      if (!name) return input.focus();
      input.value = name;
      button.disabled = true;
      await pullModel(name, { progress, status: statusText });
      button.disabled = false;
      await this.onModelsChanged();
      this.refresh();
    };
    button.addEventListener("click", () => run());
    input.addEventListener("keydown", (e) => e.key === "Enter" && run());
    const installed = new Set(state.models.map((m) => m.name));
    return el("div", { class: "section" },
      el("div", { class: "section-title", text: "Download a model" }),
      el("div", { class: "inline" }, input, button),
      progress,
      statusText,
      el("div", { class: "inline", style: "flex-wrap:wrap;margin-top:12px" },
        ...RECOMMENDED_MODELS.map((m) => el("button", {
          class: "btn btn-sm", type: "button", title: m.note, disabled: installed.has(m.name),
          onclick: () => run(m.name),
        }, installed.has(m.name) ? icon("check", "icon-xs") : icon("plus", "icon-xs"), m.name)),
      ),
      el("div", { class: "help", style: "margin-top:8px", text: "Browse more at ollama.com/library. Models with tool support work best." }),
    );
  }

  // Automations -----------------------------------------------------------------------------

  render_automations(panel) {
    const kinds = [
      ["task", "calendar-clock", "Task", "Bagley does something on a schedule and reports back."],
      ["reminder", "alarm-clock", "Reminder", "A message at a set time. No model needed."],
      ["watch", "eye", "Watch page", "Check a web page and tell you when it changes."],
    ];
    this.newKind ||= "task";
    const kind = this.newKind;
    const name = el("input", { class: "input", placeholder: kind === "watch" ? "e.g. Laptop price" : kind === "reminder" ? "e.g. Stretch" : "e.g. Morning briefing", maxlength: 80 });
    const prompt = el("textarea", { class: "textarea", rows: 3, maxlength: 4000, placeholder: kind === "task"
      ? "e.g. Give me today's weather for Porto and the top 3 tech headlines."
      : kind === "reminder" ? "e.g. Stand up and stretch." : "Optional: what to do when it changes, e.g. tell me if the price drops below 800." });
    const url = el("input", { class: "input mono", placeholder: "https://…", spellcheck: "false" });
    const when = el("input", { class: "input", id: "auto-when", list: "schedule-presets", value: kind === "watch" ? "every 1 hour" : kind === "reminder" ? "in 30 minutes" : "weekdays at 08:00" });
    const draft = this.automationDraft || {};
    name.value = draft.name || "";
    prompt.value = draft.prompt || "";
    url.value = draft.url || "";
    const presets = el("datalist", { id: "schedule-presets" },
      ...["in 30 minutes", "at 18:00", "every 1 hour", "every 6 hours", "daily at 08:00", "weekdays at 09:00", "weekends at 10:00", "mondays at 09:00"].map((v) => el("option", { value: v })));
    const hint = el("div", { class: "help", "aria-live": "polite" });
    let previewSeq = 0;
    const check = debounce(async () => {
      const seq = ++previewSeq;
      try {
        const p = await api.get(`/api/automations/preview?kind=${kind}&schedule=${encodeURIComponent(when.value)}`);
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
        await api.post("/api/automations", { kind, name: name.value.trim(), prompt: prompt.value.trim(), schedule: when.value, target: kind === "watch" ? url.value.trim() : null });
        toast(kind === "reminder" ? "Reminder set" : kind === "watch" ? "Watching the page" : "Automation created");
        this.automationDraft = null;
        await this.onAutomationsChanged();
        this.refresh();
      } catch (err) {
        toast(err.message, { type: "error" });
        create.disabled = false;
      }
    });

    const seg = el("div", { class: "segmented", role: "group", "aria-label": "Kind" },
      ...kinds.map(([id, ic, label]) => el("button", { type: "button", "aria-pressed": String(kind === id), onclick: () => {
        this.automationDraft = { name: name.value, prompt: prompt.value, url: url.value };
        this.newKind = id;
        this.refresh();
      } }, icon(ic, "icon-sm"), label)));

    this.automationBox = el("div", { class: "section" });
    panel.append(
      el("h3", { text: "Automations" }),
      el("p", { class: "lead" }, "Bagley can work on its own while it's running: reminders, scheduled tasks and page watchers. Results arrive as chats and notifications. You can also just ask, e.g. ", el("em", { text: "“every weekday at 8, brief me on the weather and news”" }), "."),
      // Existing automations come first once there are any; the form follows.
      state.automations.length ? this.automationBox : null,
      el("div", { class: "section" },
        el("div", { class: "section-title", text: "New" }),
        el("div", { class: "field" }, seg, el("div", { class: "help", text: kinds.find((k) => k[0] === kind)[3] })),
        el("div", { class: "field-row" }, field("Name", name), el("div", { class: "field" }, el("label", { for: "auto-when", text: "When" }), when, hint)),
        presets,
        kind === "watch" ? field("Page URL", url) : null,
        field(kind === "reminder" ? "Message" : kind === "watch" ? "When it changes (optional)" : "Instructions", prompt),
        el("div", { class: "inline" }, create, kind === "task" ? el("span", { class: "help", text: "Unattended runs can't use tools that need approval." }) : null),
      ),
    );

    if (!this.automationBox.parentNode) panel.append(this.automationBox);
    this.renderAutomationList();
  }

  /** Redraw only the list, so background updates never wipe a half-filled form. */
  renderAutomationList() {
    if (!this.automationBox?.isConnected) return;
    const items = state.automations;
    keepFocus(this.automationBox, () => this.automationBox.replaceChildren(
      el("div", { class: "section-title", text: `Your automations · ${items.length}` }),
      el("div", { class: "list" }, ...(items.length ? items.map((a) => this.automationRow(a)) : [el("div", { class: "list-empty", text: "Nothing scheduled yet." })])),
    ));
  }

  automationRow(a) {
    const kindIcon = { task: "calendar-clock", reminder: "alarm-clock", watch: "eye" }[a.kind];
    const next = a.running ? "running now…" : !a.enabled ? (a.next_run ? "paused" : "done") : `next ${relTime(a.next_run)}`;
    const last = a.last_run ? `${a.last_status === "error" ? "Failed" : "Last run"} ${relTime(a.last_run)}${a.last_result ? `: ${a.last_result}` : ""}` : "Hasn't run yet";
    const toggle = el("input", { type: "checkbox", role: "switch", checked: a.enabled, "aria-label": `Enable ${a.name}`, disabled: !a.enabled && !a.next_run && a.schedule.startsWith("at ") });
    toggle.addEventListener("change", async () => {
      try {
        await api.patch(`/api/automations/${a.id}`, { enabled: toggle.checked });
        await this.onAutomationsChanged();
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
      await this.onAutomationsChanged();
    });
    const openChat = a.conversation_id
      ? el("button", { class: "icon-btn icon-btn-sm", type: "button", title: "Open chat", "aria-label": `Open chat for ${a.name}`, onclick: () => { this.dialog.close(); this.openChat(a.conversation_id); } }, icon("message-square", "icon-sm"))
      : null;
    return el("div", { class: `list-item automation${a.enabled ? "" : " off"}`, dataset: { id: a.id } },
      el("span", { class: "tool-icon" }, a.running ? el("span", { class: "spinner" }) : icon(kindIcon, "icon-sm")),
      el("div", { class: "grow" },
        el("div", { class: "name" }, el("span", { class: "auto-name", text: a.name }), el("span", { class: "badge", text: a.schedule_text }), el("span", { class: "subtle", style: "font-weight:400", text: next })),
        a.target ? el("div", { class: "desc mono", text: a.target }) : null,
        el("div", { class: `desc${a.last_status === "error" ? " error-text" : ""}`, text: last }),
      ),
      el("div", { class: "inline", style: "gap:2px" }, run, openChat, remove),
      el("label", { class: "switch" }, toggle, el("span")),
    );
  }

  // Skills ----------------------------------------------------------------------------------

  render_skills(panel) {
    const folder = `${state.info?.data_dir || "~/.bagley"}/skills`;
    this.skillBox = el("div", { class: "list" });
    this.skillEditor = el("div");
    const create = el("button", { class: "btn btn-sm", type: "button", onclick: () => this.editSkill(null) }, icon("plus", "icon-sm"), "New skill");
    panel.append(
      el("h3", { text: "Skills" }),
      el("p", { class: "lead" }, "Procedures Bagley follows for recurring tasks. After it works through a task that takes several steps, it writes the procedure down as a skill, and it improves a skill each time it uses one. Skills from other agents work too: drop their folders into ", el("code", { class: "mono", text: folder }), "."),
      el("div", { class: "section" },
        toggleRow("Learn from tasks", "After a reply that took five or more tool calls, Bagley looks back at it and may save a skill or a fact you told it. You get a notification each time.", value("learning"), (v) => savePrefs({ learning: v })),
      ),
      el("div", { class: "section" },
        el("div", { class: "inline", style: "justify-content:space-between;margin-bottom:12px" }, el("div", { class: "section-title", style: "margin:0", text: "Your skills" }), create),
        this.skillEditor,
        this.skillBox,
      ),
    );
    this.renderSkillList();
  }

  renderSkillList() {
    if (!this.skillBox?.isConnected) return;
    const label = { learned: "learned", builtin: "built-in", user: "yours", pending: "needs review" };
    keepFocus(this.skillBox, () => this.skillBox.replaceChildren(...(state.skills.length ? state.skills.map((sk) => {
      if (sk.source === "pending") return this.draftRow(sk);
      const edit = el("button", { class: "icon-btn icon-btn-sm", type: "button", title: sk.editable ? "Edit" : "Customize", "aria-label": `Edit ${sk.name}`, onclick: () => this.editSkill(sk) }, icon("pencil", "icon-sm"));
      const remove = sk.editable ? el("button", { class: "icon-btn icon-btn-sm", type: "button", title: "Delete", "aria-label": `Delete ${sk.name}` }, icon("trash-2", "icon-sm")) : null;
      remove?.addEventListener("click", async () => {
        if (!(await confirmDialog({ title: `Delete ${sk.name}?`, message: "Bagley won't follow this procedure any more.", confirm: "Delete", danger: true }))) return;
        try {
          await api.del(`/api/skills/${encodeURIComponent(sk.name)}`);
          toast(`Deleted ${sk.name}`);
        } catch (err) {
          toast(err.message, { type: "error" });
        }
        await this.onSkillsChanged();
      });
      return el("div", { class: "list-item skill-row", dataset: { name: sk.name } },
        el("span", { class: "tool-icon" }, icon(sk.source === "learned" ? "sparkles" : sk.source === "builtin" ? "book-open" : "pencil", "icon-sm")),
        el("div", { class: "grow" },
          el("div", { class: "name" }, sk.name,
            el("span", { class: `badge${sk.source === "learned" ? " badge-accent" : ""}`, text: label[sk.source] }),
            sk.source !== "builtin" ? el("span", { class: "subtle", style: "font:400 12px var(--font-sans)", text: relTime(sk.updated) }) : null),
          el("div", { class: "desc", text: sk.description }),
        ),
        el("div", { class: "inline", style: "gap:2px" }, edit, remove),
      );
    }) : [el("div", { class: "list-empty", text: "No skills yet." })])));
  }

  /** A skill learned from a task that read web content: shown in full, used only once approved. */
  draftRow(sk) {
    const act = async (request, done) => {
      try {
        await request();
        toast(done);
      } catch (err) {
        toast(err.message, { type: "error" });
      }
      await this.onSkillsChanged();
    };
    const name = encodeURIComponent(sk.name);
    return el("div", { class: "list-item skill-row draft", dataset: { name: sk.name } },
      el("span", { class: "tool-icon" }, icon("sparkles", "icon-sm")),
      el("div", { class: "grow" },
        el("div", { class: "name" }, sk.name, el("span", { class: "badge badge-warn", text: "needs review" })),
        el("div", { class: "desc", text: sk.description }),
        el("details", { class: "draft-steps" }, el("summary", { text: "Show the steps" }), el("pre", { text: sk.instructions })),
        el("div", { class: "help", text: "Drafted after a task that read web content. Bagley won't use it until you approve it." }),
      ),
      el("div", { class: "inline", style: "gap:4px" },
        el("button", { class: "btn btn-sm btn-primary", type: "button", "aria-label": `Approve ${sk.name}`, onclick: () => act(() => api.post(`/api/skills/${name}/approve`), `Approved ${sk.name}`) }, icon("check", "icon-sm"), "Approve"),
        el("button", { class: "icon-btn icon-btn-sm", type: "button", title: "Discard", "aria-label": `Discard ${sk.name}`, onclick: () => act(() => api.del(`/api/skills/${name}/draft`), `Discarded ${sk.name}`) }, icon("trash-2", "icon-sm")),
      ),
    );
  }

  async editSkill(sk) {
    let current = { name: "", description: "", instructions: "" };
    if (sk) {
      try {
        current = await api.get(`/api/skills/${encodeURIComponent(sk.name)}`);
      } catch (err) {
        return toast(err.message, { type: "error" });
      }
    }
    const name = el("input", { class: "input mono", value: current.name, placeholder: "weekly-report", maxlength: 64, disabled: Boolean(sk), spellcheck: "false" });
    const description = el("input", { class: "input", value: current.description, maxlength: 300, placeholder: "One sentence: what it does and when to use it" });
    const instructions = el("textarea", { class: "textarea mono", rows: 10, maxlength: 12000, placeholder: "1. Search for…\n2. Read…\n3. Answer with…" });
    instructions.value = current.instructions;
    const close = () => this.skillEditor.replaceChildren();
    const save = el("button", { class: "btn btn-primary btn-sm", type: "button" }, icon("check", "icon-sm"), "Save");
    save.addEventListener("click", async () => {
      const key = name.value.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "");
      if (!key) return name.focus();
      if (!description.value.trim()) return description.focus();
      if (!instructions.value.trim()) return instructions.focus();
      save.disabled = true;
      try {
        await api.put(`/api/skills/${encodeURIComponent(key)}`, { description: description.value.trim(), instructions: instructions.value });
        toast(`Saved ${key}`);
        close();
        await this.onSkillsChanged();
      } catch (err) {
        toast(err.message, { type: "error" });
        save.disabled = false;
      }
    });
    this.skillEditor.replaceChildren(el("div", { class: "skill-editor" },
      field("Name", name, { help: sk?.source === "builtin" ? "Saving makes your own copy, which replaces the built-in one." : "Lowercase words joined by hyphens." }),
      field("Description", description),
      field("Instructions", instructions, { help: "Markdown. Numbered steps that name the tools work best." }),
      el("div", { class: "inline" }, save, el("button", { class: "btn btn-sm", type: "button", text: "Cancel", onclick: close })),
    ));
    (sk ? instructions : name).focus();
  }

  // Telegram --------------------------------------------------------------------------------

  render_telegram(panel) {
    this.telegramBox = el("div");
    panel.append(
      el("h3", { text: "Telegram" }),
      el("p", { class: "lead", text: "Talk to Bagley from your phone. Approvals and questions arrive as buttons, charts as photos, and automation results and reminders as messages. Bagley only connects out to Telegram, so nothing on your network is opened up." }),
      this.telegramBox,
    );
    this.renderTelegram();
    api.get("/api/telegram").then((status) => {
      state.telegram = null; // A fresh read wins, even if the server restarted its counter.
      this.applyTelegram(status);
    }).catch(() => {});
  }

  /** Keep the newest status: a broadcast can overtake the reply to the request that caused it. */
  applyTelegram(status, render = true) {
    if (!state.telegram || status.version >= state.telegram.version) state.telegram = status;
    if (render) this.renderTelegram();
  }

  renderTelegram() {
    const box = this.telegramBox;
    if (!box?.isConnected) return;
    const t = state.telegram;
    if (!t) return box.replaceChildren(el("div", { class: "status-line" }, el("span", { class: "spinner" }), "Checking…"));
    const put = async (body, done) => {
      try {
        this.applyTelegram(await api.put("/api/telegram", body), false);
        if (done) toast(done);
      } catch (err) {
        toast(err.message, { type: "error" });
        return false; // Leave the form as it is, so a rejected token can be corrected.
      }
      this.renderTelegram();
      return true;
    };
    if (!t.configured) {
      const token = el("input", { class: "input mono", type: "password", autocomplete: "off", spellcheck: "false", placeholder: "123456789:AA…", "aria-label": "Bot token" });
      const connect = el("button", { class: "btn btn-primary", type: "button" }, icon("send", "icon-sm"), "Connect");
      const go = () => (token.value.trim() ? put({ token: token.value.trim() }) : token.focus());
      connect.addEventListener("click", go);
      token.addEventListener("keydown", (e) => e.key === "Enter" && go());
      return box.replaceChildren(el("div", { class: "section" },
        el("div", { class: "section-title", text: "Create a bot" }),
        el("p", { class: "help", style: "margin:0 0 10px" }, "In Telegram, message ", el("b", { text: "@BotFather" }), ", send ", el("code", { text: "/newbot" }), ", pick a name, and paste the token it gives you."),
        el("div", { class: "inline" }, token, connect),
        t.locked ? el("div", { class: "help", text: "Set by BAGLEY_TELEGRAM_TOKEN." }) : null,
      ));
    }
    const dot = { ok: "ok", error: "bad", connecting: "", off: "" }[t.state];
    const statusText = t.state === "ok" ? `Connected as @${t.bot}` : t.state === "error" ? t.error : "Connecting…";
    const sections = [el("div", { class: "section" },
      el("div", { class: "kb-status" },
        t.state === "connecting" ? el("span", { class: "spinner" }) : el("span", { class: `dot ${dot}` }),
        el("span", { class: "grow", text: statusText }),
        t.locked ? null : el("button", { class: "btn btn-sm btn-ghost", type: "button", text: "Disconnect", onclick: () => put({ token: "" }, "Telegram disconnected") }),
      ),
    )];
    if (t.state === "ok") {
      const code = t.code;
      const link = `https://t.me/${t.bot}?start=${code.replace("-", "")}`;
      sections.push(el("div", { class: "section" },
        el("div", { class: "section-title", text: "Pair your phone" }),
        el("p", { class: "help", style: "margin:0 0 10px" }, "Open the link on your phone, or send ", el("code", { class: "mono", text: `/pair ${code}` }), ` to @${t.bot}. Each code pairs one chat and expires after 15 minutes.`),
        el("div", { class: "inline pair-row" },
          el("a", { class: "btn btn-primary btn-sm", href: link, target: "_blank", rel: "noopener" }, icon("external-link", "icon-sm"), "Open in Telegram"),
          el("span", { class: "pair-code mono", text: code }),
          el("button", { class: "btn btn-sm btn-ghost", type: "button", text: "New code", onclick: async () => {
            try {
              this.applyTelegram(await api.post("/api/telegram/code"));
            } catch (err) {
              toast(err.message, { type: "error" });
            }
          } }),
        ),
      ));
    }
    sections.push(el("div", { class: "section" },
      el("div", { class: "section-title", text: `Paired chats · ${t.chats.length}` }),
      el("div", { class: "list" }, ...(t.chats.length ? t.chats.map((c) => el("div", { class: "list-item telegram-chat" },
        el("span", { class: "tool-icon" }, icon("send", "icon-sm")),
        el("div", { class: "grow" }, el("div", { class: "name", text: c.name }), el("div", { class: "desc mono", text: `chat ${c.id}` })),
        el("button", { class: "btn btn-sm btn-ghost", type: "button", text: "Unpair", "aria-label": `Unpair ${c.name}`, onclick: async () => {
          try {
            this.applyTelegram(await api.del(`/api/telegram/chats/${c.id}`), false);
          } catch (err) {
            toast(err.message, { type: "error" });
          }
          this.renderTelegram();
        } }),
      )) : [el("div", { class: "list-empty", text: "No phone paired yet." })])),
      toggleRow("Send automation results here", "Reminders, scheduled task results and page changes also go to your paired chats.", t.notify, (v) => put({ notify: v })),
    ));
    keepFocus(box, () => box.replaceChildren(...sections));
  }

  // Knowledge -------------------------------------------------------------------------------

  render_knowledge(panel) {
    this.knowledgeBox = el("div");
    const pathInput = el("input", { class: "input mono", placeholder: "~/Documents/Notes", spellcheck: "false", "aria-label": "Folder path" });
    const add = el("button", { class: "btn", type: "button" }, icon("folder-plus", "icon-sm"), "Add folder");
    const submit = async () => {
      if (!pathInput.value.trim()) return pathInput.focus();
      add.disabled = true;
      try {
        state.knowledge = await api.post("/api/knowledge/folders", { path: pathInput.value.trim() });
        pathInput.value = "";
        toast("Folder added. Indexing…");
        this.renderKnowledge();
      } catch (err) {
        toast(err.message, { type: "error" });
      }
      add.disabled = false;
    };
    add.addEventListener("click", submit);
    pathInput.addEventListener("keydown", (e) => e.key === "Enter" && submit());

    const query = el("input", { class: "input", placeholder: "Try a search, e.g. budget for Q3", "aria-label": "Search the knowledge base" });
    const results = el("div", { class: "kb-results" });
    let searchSeq = 0;
    query.addEventListener("input", debounce(async () => {
      const seq = ++searchSeq;
      const q = query.value.trim();
      if (!q) return results.replaceChildren();
      const hits = await api.get(`/api/knowledge/search?q=${encodeURIComponent(q)}`).catch(() => []);
      if (seq !== searchSeq) return;
      results.replaceChildren(...(hits.length ? hits.map((h) => el("div", { class: "kb-hit" },
        el("div", { class: "name" }, el("span", { class: "mono", text: h.path }), el("span", { class: `badge${h.match === "keyword" ? "" : " badge-accent"}`, text: h.match })),
        el("div", { class: "desc", text: h.text })))
        : [el("div", { class: "list-empty", text: "No matches." })]));
    }, 250));

    panel.append(
      el("h3", { text: "Knowledge" }),
      el("p", { class: "lead", text: "Bagley searches these folders when you ask about your notes and documents. Files are indexed on this computer and never uploaded. Text, Markdown, code, HTML and CSV are supported (PDF too with the pdf extra)." }),
      this.knowledgeBox,
      el("div", { class: "section" },
        el("div", { class: "section-title", text: "Add a folder" }),
        el("div", { class: "inline" }, pathInput, add),
        el("div", { class: "help", style: "margin-top:6px", text: "A full path on this computer. Hidden folders and node_modules are skipped." }),
      ),
      el("div", { class: "section" }, el("div", { class: "section-title", text: "Search" }), query, results),
    );
    this.renderKnowledge();
  }

  /** Status and folder list; redrawn on index progress without touching the inputs. */
  renderKnowledge() {
    const box = this.knowledgeBox;
    if (!box?.isConnected) return;
    const k = state.knowledge || { folders: [], files: 0, passages: 0 };
    const indexing = k.state === "indexing";
    const semantic = k.embedding_model
      ? el("span", { class: "badge badge-accent", text: `search by meaning · ${k.embedding_model}` })
      : el("span", { class: "badge", text: "keyword search" });
    // Stays enabled while indexing (a second request is ignored) so keyboard focus survives redraws.
    const reindex = el("button", { class: "btn btn-sm", type: "button", "aria-label": "Reindex now", onclick: async () => {
      try {
        await api.post("/api/knowledge/reindex");
        toast("Reindexing…");
      } catch (err) {
        toast(err.message, { type: "error" });
      }
    } }, icon("refresh-cw", "icon-sm"), "Reindex now");

    const embedModels = state.models.filter((m) => /embed|minilm|bge-|e5-|gte-/i.test(m.name));
    const embedSelect = el("select", { class: "select" },
      el("option", { value: "", text: "Automatic" }),
      el("option", { value: "off", text: "Off (keyword search only)" }),
      ...embedModels.map((m) => el("option", { value: m.name, text: m.name })));
    embedSelect.value = value("embedding_model") || "";
    embedSelect.addEventListener("change", async () => {
      if (await savePrefs({ embedding_model: embedSelect.value })) await api.post("/api/knowledge/reindex");
    });
    const canPull = state.health?.provider === "ollama" && !embedModels.length;
    const pullBox = el("div");
    if (canPull) {
      const progress = el("div", { class: "progress", hidden: true }, el("i"));
      const status = el("div", { class: "pull-status" });
      const pull = el("button", { class: "btn btn-sm", type: "button" }, icon("hard-drive-download", "icon-sm"), "Download nomic-embed-text (274 MB)");
      pull.addEventListener("click", async () => {
        pull.disabled = true;
        if (await pullModel("nomic-embed-text", { progress, status })) {
          await this.onModelsChanged();
          await api.post("/api/knowledge/reindex");
        } else pull.disabled = false;
      });
      pullBox.append(el("div", { class: "help", style: "margin:4px 0 8px", text: "For search by meaning (not just keywords), download a small local embedding model:" }), pull, progress, status);
    }

    keepFocus(box, () => box.replaceChildren(
      el("div", { class: "section" },
        el("div", { class: "kb-status" },
          indexing ? el("span", { class: "spinner" }) : icon("library", "icon-sm"),
          el("span", { text: indexing ? `Indexing ${k.progress || "…"}` : `${k.files.toLocaleString()} files · ${k.passages.toLocaleString()} passages` }),
          semantic,
          el("span", { class: "grow" }),
          reindex,
        ),
        k.error ? el("div", { class: "help error-text", style: "margin-top:6px", text: k.error }) : null,
        el("div", { class: "list", style: "margin-top:12px" }, ...k.folders.map((f) => el("div", { class: "list-item" },
          el("span", { class: "tool-icon" }, icon("folder-open", "icon-sm")),
          el("div", { class: "grow" },
            el("div", { class: "name" }, f.label, el("span", { class: "badge", text: `${f.files} files` }), f.exists ? null : el("span", { class: "badge badge-danger", text: "missing" })),
            el("div", { class: "desc mono", text: f.path }),
          ),
          f.removable
            ? el("button", { class: "icon-btn icon-btn-sm", type: "button", title: "Remove", "aria-label": `Remove ${f.label}`, onclick: async () => {
                state.knowledge = await api.del(`/api/knowledge/folders?path=${encodeURIComponent(f.path)}`).catch((err) => (toast(err.message, { type: "error" }), state.knowledge));
                this.renderKnowledge();
              } }, icon("x", "icon-sm"))
            : el("span", { class: "subtle", style: "font-size:12px", text: "always included" }),
        ))),
      ),
      el("div", { class: "section" },
        el("div", { class: "section-title", text: "Search by meaning" }),
        field("Embedding model", embedSelect, { help: "Automatic uses an installed embedding model if there is one." }),
        pullBox,
      ),
    ));
  }

  // Tools -----------------------------------------------------------------------------------

  render_tools(panel) {
    const disabled = new Set(value("disabled_tools") || []);
    const info = state.toolsInfo || {};
    const categories = { web: "Web", utility: "Utilities", files: "Files", memory: "Memory", system: "System", mcp: "MCP servers", general: "Other" };
    const grouped = new Map();
    for (const t of state.tools) {
      const key = categories[t.category] ? t.category : "general";
      if (!grouped.has(key)) grouped.set(key, []);
      grouped.get(key).push(t);
    }
    panel.append(
      el("h3", { text: "Tools" }),
      el("p", { class: "lead" }, "What Bagley can do on its own. Tools marked ", el("span", { class: "badge badge-warn", text: "asks first" }), " wait for your approval every time."),
    );
    for (const [cat, tools] of [...grouped].sort((a, b) => Object.keys(categories).indexOf(a[0]) - Object.keys(categories).indexOf(b[0]))) {
      panel.append(el("div", { class: "section" },
        el("div", { class: "section-title", text: categories[cat] }),
        el("div", { class: "list" }, ...tools.map((t) => {
          const input = el("input", { type: "checkbox", role: "switch", checked: !disabled.has(t.name), "aria-label": `Enable ${t.name}` });
          input.addEventListener("change", () => {
            const next = new Set(value("disabled_tools") || []);
            if (input.checked) next.delete(t.name);
            else next.add(t.name);
            savePrefs({ disabled_tools: [...next] });
          });
          return el("div", { class: "list-item" },
            el("div", { class: "grow" },
              el("div", { class: "name" }, t.name,
                t.risk === "confirm" ? el("span", { class: "badge badge-warn", text: "asks first" }) : null,
                t.source !== "builtin" ? el("span", { class: "badge", text: t.source }) : null,
              ),
              el("div", { class: "desc", text: t.description.split("\n")[0] }),
            ),
            el("label", { class: "switch" }, input, el("span")),
          );
        })),
      ));
    }
    const mcp = info.mcp || [];
    panel.append(el("div", { class: "section" },
      el("div", { class: "section-title", text: "MCP servers" }),
      mcp.length
        ? el("div", { class: "list" }, ...mcp.map((s) => el("div", { class: "list-item" },
            el("span", { class: `dot ${s.state === "running" ? "ok" : "bad"}`, style: "margin-top:6px" }),
            el("div", { class: "grow" },
              el("div", { class: "name" }, s.name, s.trusted ? el("span", { class: "badge", text: "trusted" }) : null),
              el("div", { class: "desc", text: s.state === "running" ? `${s.tools} tool${s.tools === 1 ? "" : "s"}` : s.error || s.state }),
            ),
          )))
        : el("p", { class: "help" }, "Connect Model Context Protocol servers by listing them in ", el("code", { class: "mono", text: `${state.info?.data_dir || "~/.bagley"}/mcp.json` }), ", then restart Bagley."),
    ));
    const notes = [];
    if (!info.shell_enabled) notes.push("Shell commands are off. Start Bagley with BAGLEY_ENABLE_SHELL=true to let it run commands (each one still asks first).");
    if (state.info?.workspace) notes.push(`File tools only see ${state.info.workspace}. Change it with BAGLEY_WORKSPACE.`);
    for (const err of info.errors || []) notes.push(`${err.source}: ${err.error}`);
    if (notes.length) panel.append(el("div", { class: "section" }, ...notes.map((n) => el("div", { class: "notice", style: "margin-bottom:8px" }, icon("info", "icon-sm"), el("span", { text: n })))));
  }

  // Memory ----------------------------------------------------------------------------------

  render_memory(panel) {
    const input = el("input", { class: "input", placeholder: "e.g. I'm vegetarian", maxlength: 500, "aria-label": "New memory" });
    const add = async () => {
      const content = input.value.trim();
      if (!content) return input.focus();
      try {
        await api.post("/api/memories", { content });
        input.value = "";
        await this.onMemoriesChanged();
        this.refresh();
        $("#settings-panel input")?.focus();
      } catch (err) {
        toast(err.message, { type: "error" });
      }
    };
    input.addEventListener("keydown", (e) => e.key === "Enter" && add());
    this.memoryBox = el("div", { class: "section" });
    panel.append(
      el("h3", { text: "Memory" }),
      el("p", { class: "lead", text: "Facts Bagley keeps between conversations. It saves them when you share something lasting, or when you ask it to remember. Everything here goes into each conversation's context." }),
      el("div", { class: "section" }, el("div", { class: "inline" }, input, el("button", { class: "btn", type: "button", onclick: add }, icon("plus", "icon-sm"), "Add"))),
      this.memoryBox,
    );
    this.renderMemoryList();
  }

  renderMemoryList() {
    if (!this.memoryBox?.isConnected) return;
    const items = state.memories.map((m) => el("div", { class: "list-item" },
      el("span", { class: "subtle mono", style: "font-size:12px;margin-top:2px", text: `#${m.id}` }),
      el("div", { class: "grow", text: m.content }),
      el("button", {
        class: "icon-btn icon-btn-sm", type: "button", "aria-label": `Forget: ${m.content}`, title: "Forget",
        onclick: async () => {
          await api.del(`/api/memories/${m.id}`).catch((err) => toast(err.message, { type: "error" }));
          await this.onMemoriesChanged();
          this.refresh();
          toast("Forgotten", {
            action: { label: "Undo", run: async () => { await api.post("/api/memories", { content: m.content }); await this.onMemoriesChanged(); this.refresh(); } },
          });
        },
      }, icon("trash-2", "icon-sm")),
    ));
    const clear = el("button", {
      class: "btn btn-sm btn-danger", type: "button", disabled: !state.memories.length,
      onclick: async () => {
        if (!(await confirmDialog({ title: "Forget everything?", message: `Bagley will forget all ${state.memories.length} saved memories. This can't be undone.`, confirm: "Forget all", danger: true }))) return;
        await Promise.all(state.memories.map((m) => api.del(`/api/memories/${m.id}`)));
        await this.onMemoriesChanged();
        this.refresh();
      },
    }, "Forget all");
    keepFocus(this.memoryBox, () => this.memoryBox.replaceChildren(
      el("div", { class: "list" }, ...(items.length ? items : [el("div", { class: "list-empty", text: "Nothing remembered yet. Try “Remember that I prefer metric units.”" })])),
      state.memories.length ? el("div", { style: "margin-top:12px" }, clear) : null,
    ));
  }

  // Voice -----------------------------------------------------------------------------------

  render_voice(panel) {
    panel.append(el("h3", { text: "Voice" }), el("p", { class: "lead", text: "Speech uses the voices installed on your system, so it works offline." }));
    if (!voice.canSpeak) {
      panel.append(el("div", { class: "notice" }, icon("info", "icon-sm"), el("span", { text: "This browser doesn't support speech synthesis." })));
      return;
    }
    const voiceSelect = el("select", { class: "select" });
    const fill = () => {
      const voices = voice.voices();
      const lang = (navigator.language || "en").slice(0, 2);
      const sorted = [...voices].sort((a, b) => Number(b.lang.startsWith(lang)) - Number(a.lang.startsWith(lang)) || a.name.localeCompare(b.name));
      voiceSelect.replaceChildren(el("option", { value: "", text: "System default" }),
        ...sorted.map((v) => el("option", { value: v.voiceURI, text: `${v.name} (${v.lang})${v.localService ? "" : " · online"}` })));
      voiceSelect.value = state.ui.voice || "";
    };
    fill();
    this.fillVoices = fill;
    voiceSelect.addEventListener("change", () => setUi("voice", voiceSelect.value));
    const rate = el("input", { class: "range", type: "range", min: 0.6, max: 1.6, step: 0.1, value: state.ui.rate, "aria-label": "Speaking rate" });
    const rateOut = el("output", { text: `${Number(state.ui.rate).toFixed(1)}×` });
    rate.addEventListener("input", () => { rateOut.textContent = `${Number(rate.value).toFixed(1)}×`; setUi("rate", Number(rate.value)); });
    panel.append(
      el("div", { class: "section" },
        toggleRow("Read replies aloud", "Bagley speaks each reply when it finishes. The avatar pulses with each word.", state.ui.speak, (v) => setUi("speak", v)),
      ),
      el("div", { class: "section" },
        field("Voice", voiceSelect, { id: "pref-voice" }),
        field("Speed", el("div", { class: "range-row" }, rate, rateOut)),
        el("button", { class: "btn btn-sm", type: "button", onclick: () => document.dispatchEvent(new CustomEvent("bagley:speak", { detail: "Hello. I'm Bagley, and this is how I sound." })) }, icon("volume-2", "icon-sm"), "Test voice"),
      ),
      el("div", { class: "section" },
        el("div", { class: "section-title", text: "Voice input" }),
        el("p", { class: "help", text: voice.canListen
          ? "The microphone button in the message box uses your browser's speech recognition. In Chrome and Edge that service runs online."
          : "This browser doesn't offer speech recognition. Chrome, Edge and Safari do." }),
      ),
    );
  }

  // Appearance ------------------------------------------------------------------------------

  render_appearance(panel) {
    const themes = [["system", "monitor", "System"], ["dark", "moon", "Dark"], ["light", "sun", "Light"]];
    const seg = el("div", { class: "segmented", role: "group", "aria-label": "Theme" },
      ...themes.map(([id, ic, label]) => el("button", {
        type: "button", "aria-pressed": String(state.ui.theme === id),
        onclick: () => { setUi("theme", id); this.refresh(); },
      }, icon(ic, "icon-sm"), label)),
    );
    const swatches = el("div", { class: "swatches", role: "group", "aria-label": "Accent colour" },
      ...ACCENTS.map((a) => el("button", {
        class: "swatch", type: "button", style: `--h:${a.h}`, title: a.name, "aria-label": a.name,
        "aria-pressed": String(Number(state.ui.accent) === a.h),
        onclick: () => { setUi("accent", a.h); this.refresh(); },
      })),
    );
    panel.append(
      el("h3", { text: "Appearance" }),
      el("p", { class: "lead", text: "Saved in this browser." }),
      el("div", { class: "section" }, el("div", { class: "field" }, el("span", { class: "field-label", text: "Theme" }), seg)),
      el("div", { class: "section" }, el("div", { class: "field" }, el("span", { class: "field-label", text: "Accent" }), swatches)),
      el("div", { class: "section" },
        toggleRow("Reduce motion", "Calmer avatar and no animated transitions. Your system setting is respected either way.", state.ui.reduceMotion, (v) => setUi("reduceMotion", v)),
      ),
    );
  }
}

/** Download a model through the server, updating a progress bar. Shared with onboarding. */
export async function pullModel(name, { progress, status }) {
  progress.hidden = false;
  const bar = progress.firstElementChild;
  bar.style.width = "0%";
  status.textContent = `Starting download of ${name}…`;
  let failed = false;
  try {
    await api.stream("/api/models/pull", { name }, (ev) => {
      if (ev.error) {
        failed = true;
        status.textContent = ev.error;
        return;
      }
      if (ev.total && ev.completed !== undefined) {
        const pct = Math.min(100, (ev.completed / ev.total) * 100);
        bar.style.width = `${pct}%`;
        status.textContent = `${ev.status} · ${formatBytes(ev.completed)} of ${formatBytes(ev.total)} (${pct.toFixed(0)}%)`;
      } else if (ev.status) {
        status.textContent = ev.status;
      }
    });
  } catch (err) {
    failed = true;
    status.textContent = err.message;
  }
  if (!failed) {
    bar.style.width = "100%";
    status.textContent = `${name} is ready.`;
    toast(`${name} downloaded`);
  } else {
    toast(`Couldn't download ${name}`, { type: "error" });
  }
  return !failed;
}
