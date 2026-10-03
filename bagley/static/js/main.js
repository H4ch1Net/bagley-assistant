// Entry point: wires state, socket, avatar, thread, sidebar, settings and shortcuts together.

import { api, ChatSocket } from "./api.js";
import { Attachments } from "./attachments.js";
import { mountAvatars } from "./avatar.js";
import { Chat } from "./chat.js";
import { pullModel, RECOMMENDED_MODELS, savePrefs, Settings } from "./settings.js";
import { Sidebar } from "./sidebar.js";
import { bus, setUi, state, STATUS_TEXT } from "./state.js";
import { announce, closeMenus, openMenu, toast } from "./ui.js";
import { $, copyText, el, formatBytes, formatTokens, icon, isMac, kbdLabel, timeOfDay } from "./util.js";
import { voice } from "./voice.js";

const avatars = mountAvatars();
const socket = new ChatSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/ws`);
const input = $("#composer-input");

// Status -----------------------------------------------------------------------------------------

let settleTimer;
function setStatus(name, label) {
  clearTimeout(settleTimer);
  const offline = !state.connected || (state.health && !state.health.ok);
  if (name === "idle" && offline) name = "offline";
  const text = label || (name === "offline" ? offlineText() : STATUS_TEXT[name] || name);
  avatars.setState(name);
  const pill = $("#presence-state");
  pill.dataset.state = name;
  pill.replaceChildren(...(["thinking", "reasoning", "tool", "writing"].includes(name) ? [el("span", { class: "spinner" })] : []), text);
  $("#tb-status").textContent = text;
  if (name === "happy") settleTimer = setTimeout(() => setStatus("idle"), 1600);
  if (name === "error") settleTimer = setTimeout(() => setStatus("idle"), 3200);
}

function offlineText() {
  if (!state.connected) return "Reconnecting…";
  return state.health?.error ? "Model server offline" : "Offline";
}

// Chat, sidebar, settings ------------------------------------------------------------------------

const chat = new Chat({
  socket,
  avatars,
  voice,
  setStatus,
  onRunStart: () => {
    updateComposer();
    sidebar.render();
  },
  onRunEnd: (ev, { failed }) => {
    setStatus(failed ? "error" : ev.stopped ? "idle" : "happy");
    if (document.hidden && !ev.stopped) document.title = `● ${document.title.replace(/^● /, "")}`;
    updateComposer();
    sidebar.refresh();
    renderStats();
  },
});

const sidebar = new Sidebar({
  onOpen: (id) => navigate(id),
  onDeletedActive: () => navigate(null),
});

const settings = new Settings({
  onModelsChanged: async () => {
    await Promise.all([loadModels(), refreshHealth()]);
  },
  onMemoriesChanged: loadMemories,
});

// Data -------------------------------------------------------------------------------------------

async function loadPrefs() {
  state.prefs = await api.get("/api/preferences");
  bus.emit("prefs");
}

async function loadTools() {
  const data = await api.get("/api/tools");
  state.tools = data.tools;
  state.toolsInfo = data;
  renderStats();
}

async function loadMemories() {
  state.memories = await api.get("/api/memories");
  renderStats();
}

async function loadModels() {
  try {
    const data = await api.get("/api/models");
    state.models = data.models;
    state.canPull = data.can_pull;
  } catch {
    state.models = [];
  }
  renderModelButton();
}

let healthTimer;
async function refreshHealth() {
  clearTimeout(healthTimer);
  try {
    state.health = await api.get("/api/health");
  } catch (err) {
    state.health = { ok: false, error: err.message };
  }
  bus.emit("health");
  healthTimer = setTimeout(refreshHealth, state.health.ok ? 60000 : 8000);
}

bus.on("health", () => {
  renderConnection();
  renderSetup();
  renderModelButton();
  renderStats();
  if (!state.run) setStatus("idle");
});

bus.on("prefs", () => {
  renderModelButton();
  renderStats();
  renderPrivacy();
  settings.refresh();
  loadTools().catch(() => {});
});

// Navigation -------------------------------------------------------------------------------------

function navigate(id) {
  const hash = id ? `#/c/${id}` : "#/";
  if (location.hash !== hash) history.pushState(null, "", hash);
  route();
}

async function route() {
  const match = location.hash.match(/^#\/c\/([\w-]+)/);
  const id = match ? match[1] : null;
  closeSidebar();
  if (!id) {
    state.activeId = null;
    chat.render([]);
    setTitle(null);
    sidebar.render();
    renderStats();
    return;
  }
  try {
    const data = await api.get(`/api/conversations/${id}`);
    state.activeId = id;
    chat.render(data.messages);
    setTitle(data.conversation.title);
    const last = [...data.messages].reverse().find((m) => m.role === "assistant" && m.meta?.model);
    state.lastStats = last ? { tokens_per_second: last.meta.tokens_per_second, prompt_tokens: last.meta.prompt_tokens } : null;
  } catch (err) {
    toast(err.status === 404 ? "That chat no longer exists." : err.message, { type: "error" });
    history.replaceState(null, "", "#/");
    state.activeId = null;
    chat.render([]);
    setTitle(null);
  }
  sidebar.render();
  renderStats();
}

addEventListener("popstate", route);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) document.title = document.title.replace(/^● /, "");
});

function setTitle(title) {
  const btn = $("#title-btn");
  btn.textContent = title || "New chat";
  btn.disabled = !state.activeId;
  $("#export-btn").disabled = !state.activeId;
  document.title = title ? `${title} · Bagley` : "Bagley";
}

function newChat() {
  navigate(null);
  input.focus();
}

// Socket events ----------------------------------------------------------------------------------

socket.on("open", () => {
  state.connected = true;
  refreshHealth();
  updateComposer();
});

socket.on("close", () => {
  state.connected = false;
  if (state.run) {
    chat.onError({ message: "Lost connection to Bagley's server." });
    chat.onRunEndEvent({ stopped: true, stats: {} });
  }
  renderConnection();
  setStatus("idle");
  updateComposer();
});

socket.on("conversation", (ev) => {
  const wasNew = !state.activeId;
  const ours = state.run && state.run.conversationId === null;
  if (ours) state.run.conversationId = ev.conversation.id;
  sidebar.upsert(ev.conversation);
  if (wasNew && ours) {
    state.activeId = ev.conversation.id;
    history.replaceState(null, "", `#/c/${ev.conversation.id}`);
    setTitle(ev.conversation.title);
    sidebar.render();
  }
});

socket.on("title", (ev) => {
  sidebar.upsert({ id: ev.conversation_id, title: ev.title });
  if (ev.conversation_id === state.activeId) setTitle(ev.title);
});

socket.on("model", (ev) => {
  state.runModel = ev.model;
  renderStats();
});

document.addEventListener("bagley:renamed", (e) => {
  if (e.detail.id === state.activeId) setTitle(e.detail.title);
});
document.addEventListener("bagley:speak", (e) => chat.speak(e.detail));

// Composer ---------------------------------------------------------------------------------------

const attachments = new Attachments({ onChange: () => updateComposer() });
attachments.bindDrop($("#main"), $("#composer"));
$("#attach-btn").addEventListener("click", () => $("#file-input").click());
$("#file-input").addEventListener("change", (e) => {
  attachments.add([...e.target.files]);
  e.target.value = "";
});
input.addEventListener("paste", (e) => {
  const files = [...(e.clipboardData?.files || [])];
  if (files.length) {
    e.preventDefault();
    attachments.add(files);
  }
});

function autosize() {
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, innerHeight * 0.4)}px`;
}

function updateComposer() {
  const btn = $("#send-btn");
  const running = Boolean(state.run);
  btn.classList.toggle("stop", running);
  btn.replaceChildren(icon(running ? "square" : "arrow-up"));
  btn.setAttribute("aria-label", running ? "Stop reply" : "Send message");
  btn.title = running ? "Stop (Esc)" : "Send (Enter)";
  const hasContent = input.value.trim() || attachments.ready.length;
  btn.disabled = !running && (!hasContent || attachments.uploading || !state.connected);
}

function submit() {
  if (state.run) {
    chat.stop();
    return;
  }
  if (attachments.uploading) {
    toast("Still attaching files…");
    return;
  }
  const typed = input.value.trim() || (attachments.ready.length ? "Take a look at the attached file." : "");
  if (!typed) return;
  if (chat.send(typed + attachments.note())) {
    input.value = "";
    attachments.clear();
    autosize();
    updateComposer();
  }
}

$("#composer").addEventListener("submit", (e) => {
  e.preventDefault();
  submit();
});

input.addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey && !e.isComposing) {
    e.preventDefault();
    if (!state.run) submit();
  } else if (e.key === "ArrowUp" && !input.value && !state.run) {
    const last = [...document.querySelectorAll(".turn-user.last [aria-label='Edit and resend']")].pop();
    if (last && !last.hidden) {
      e.preventDefault();
      last.click();
    }
  }
});

input.addEventListener("input", () => {
  autosize();
  updateComposer();
  if (!state.run) setStatus(input.value.trim() ? "listening" : "idle");
});
input.addEventListener("blur", () => {
  if (!state.run && avatars.list[0]?.name === "listening") setStatus("idle");
});

// Voice input
if (voice.canListen) {
  const mic = $("#mic-btn");
  mic.hidden = false;
  let stopListening = null;
  mic.addEventListener("click", () => {
    if (stopListening) {
      stopListening();
      return;
    }
    const before = input.value ? `${input.value.trimEnd()} ` : "";
    mic.classList.add("recording");
    mic.setAttribute("aria-pressed", "true");
    setStatus("listening", "Listening…");
    stopListening = voice.listen({
      onText: (text) => {
        input.value = before + text;
        autosize();
        updateComposer();
      },
      onError: (err) => toast(err === "not-allowed" ? "Microphone access was blocked." : `Voice input failed: ${err}`, { type: "error" }),
      onEnd: () => {
        stopListening = null;
        mic.classList.remove("recording");
        mic.setAttribute("aria-pressed", "false");
        if (!state.run) setStatus("idle");
        input.focus();
      },
    });
  });
}

// Model picker -----------------------------------------------------------------------------------

function currentModel() {
  return state.prefs?.values.model || state.health?.model || "";
}

function renderModelButton() {
  const name = currentModel();
  $("#model-name").textContent = name || (state.health?.ok ? "No model" : "Model");
  const btn = $("#model-btn");
  btn.title = state.prefs?.locked.includes("model") ? "Model set by BAGLEY_MODEL" : "Switch model";
}

$("#model-btn").addEventListener("click", () => {
  const picker = $("#model-picker");
  if (picker.querySelector(".menu")) return closeMenus();
  loadModels();
  openMenu(picker, (menu) => {
    const lockedModel = state.prefs?.locked.includes("model");
    menu.append(el("div", { class: "menu-label", text: "Model" }));
    if (!state.models.length) {
      menu.append(el("div", { class: "menu-note", text: state.health?.ok ? "No models installed yet." : "Model server is not reachable." }));
    }
    const current = currentModel();
    for (const m of state.models) {
      menu.append(el("button", {
        class: "menu-item", type: "button", role: "option", "aria-selected": String(m.name === current), disabled: lockedModel,
        onclick: async () => {
          menu.close();
          if (m.name !== current && (await savePrefs({ model: m.name }))) {
            await refreshHealth();
            toast(`Switched to ${m.name}`);
          }
        },
      },
      icon(m.name === current ? "check" : "cpu", "icon-sm"),
      el("span", { class: "grow mono", text: m.name }),
      el("span", { class: "subtle", style: "font-size:12px", text: [m.parameter_size, formatBytes(m.size)].filter(Boolean).join(" · ") })));
    }
    menu.append(el("div", { class: "menu-sep" }),
      el("button", { class: "menu-item", type: "button", onclick: () => { menu.close(); settings.open("model"); } }, icon("settings", "icon-sm"), el("span", { class: "grow", text: "Model settings…" })));
  });
});

// Title rename -----------------------------------------------------------------------------------

$("#title-btn").addEventListener("click", () => {
  const btn = $("#title-btn");
  const field = el("input", { value: btn.textContent, "aria-label": "Chat title", maxlength: 120 });
  btn.replaceWith(field);
  field.focus();
  field.select();
  let done = false;
  const finish = async (save) => {
    if (done) return;
    done = true;
    field.replaceWith(btn);
    const title = field.value.trim();
    if (save && title && title !== btn.textContent && state.activeId) {
      try {
        const conv = await api.patch(`/api/conversations/${state.activeId}`, { title });
        sidebar.upsert(conv);
        setTitle(conv.title);
      } catch (err) {
        toast(err.message, { type: "error" });
      }
    }
  };
  field.addEventListener("keydown", (e) => {
    if (e.key === "Enter") finish(true);
    if (e.key === "Escape") { e.stopPropagation(); finish(false); }
  });
  field.addEventListener("blur", () => finish(true));
});

$("#export-btn").addEventListener("click", () => {
  if (state.activeId) location.href = `/api/conversations/${state.activeId}/export`;
});

// Presence panel ---------------------------------------------------------------------------------

function applyPresence() {
  $("#app").classList.toggle("presence-open", state.ui.presence);
  const btn = $("#presence-toggle");
  btn.setAttribute("aria-pressed", String(state.ui.presence));
  btn.setAttribute("aria-label", state.ui.presence ? "Hide Bagley panel" : "Show Bagley panel");
}

$("#presence-toggle").addEventListener("click", () => setUi("presence", !state.ui.presence));

function stat(iconName, key, valueNode, onClick, extra) {
  const tag = onClick ? "button" : "div";
  return el(tag, { class: "stat", type: onClick ? "button" : null, onclick: onClick || null },
    icon(iconName, "icon-sm"), el("span", { class: "k", text: key }), valueNode, extra);
}

function renderStats() {
  const stats = $("#stats");
  if (!stats) return;
  const modelName = state.runModel && state.run ? state.runModel : currentModel();
  const info = state.models.find((m) => m.name === modelName);
  const s = state.lastStats || {};
  const ctxLimit = state.prefs?.values.context_tokens || 8192;
  const used = s.prompt_tokens || 0;
  const pct = Math.min(100, (used / ctxLimit) * 100);
  const enabledTools = state.tools.filter((t) => t.enabled).length;
  const ctxBar = el("div", { class: "ctx-bar", title: `${used} of ${ctxLimit} tokens` }, el("i", { style: `width:${pct}%` }));
  stats.replaceChildren(
    stat("cpu", "Model", el("span", { class: "v mono", text: modelName ? `${modelName}${info?.parameter_size ? ` · ${info.parameter_size}` : ""}` : "–" }), () => settings.open("model")),
    stat("gauge", "Speed", el("span", { class: "v", text: s.tokens_per_second ? `${Math.round(s.tokens_per_second)} tokens/s` : "–" })),
    el("div", { class: "stat", style: "flex-direction:column;align-items:stretch;gap:0" },
      el("div", { style: "display:flex;gap:12px;align-items:center" }, icon("brain", "icon-sm"), el("span", { class: "k", text: "Context" }),
        el("span", { class: "v", text: used ? `${formatTokens(used)} / ${formatTokens(ctxLimit)}` : `${formatTokens(ctxLimit)} window` })),
      used ? ctxBar : null,
    ),
    stat("wrench", "Tools", el("span", { class: "v", text: `${enabledTools} enabled` }), () => settings.open("tools")),
    stat("bookmark", "Memory", el("span", { class: "v", text: `${state.memories.length} ${state.memories.length === 1 ? "fact" : "facts"}` }), () => settings.open("memory")),
    voice.canSpeak
      ? stat(state.ui.speak ? "volume-2" : "volume-x", "Voice", el("span", { class: "v", text: state.ui.speak ? "Reads replies" : "Muted" }), () => {
          setUi("speak", !state.ui.speak);
          if (!state.ui.speak) voice.stop();
        })
      : null,
  );
  renderPrivacy();
}

function isLocalServer() {
  const url = state.prefs?.values.base_url || "";
  try {
    const host = new URL(url).hostname;
    return ["localhost", "127.0.0.1", "[::1]", "::1"].includes(host) || host.endsWith(".local");
  } catch {
    return true;
  }
}

function renderPrivacy() {
  const local = isLocalServer();
  const text = local ? "Local model · stays on this machine" : "Remote model server";
  $("#presence-foot").replaceChildren(icon(local ? "lock" : "globe", "icon-xs"), text);
  $("#privacy").replaceChildren(icon(local ? "lock" : "globe", "icon-xs"), local ? "Runs locally" : "Remote model");
}

function renderConnection() {
  const dot = $("#conn-dot");
  const label = $("#conn-label");
  const h = state.health;
  if (!state.connected) {
    dot.className = "dot warn";
    label.textContent = "Reconnecting to Bagley…";
  } else if (!h) {
    dot.className = "dot";
    label.textContent = "Checking model server…";
  } else if (h.ok && h.models) {
    dot.className = "dot ok";
    label.textContent = `${h.provider === "ollama" ? "Ollama" : "OpenAI-compatible"} · ${h.models} model${h.models === 1 ? "" : "s"}`;
  } else if (h.ok) {
    dot.className = "dot warn";
    label.textContent = "No models installed";
  } else {
    dot.className = "dot bad";
    label.textContent = "Model server offline";
  }
  $("#conn-status").title = h?.error ? `${h.error} ${h.hint || ""}` : `Model server: ${state.prefs?.values.base_url || ""}`;
}

$("#conn-status").addEventListener("click", () => settings.open("model"));

// Empty state: suggestions and onboarding --------------------------------------------------------

const SUGGESTIONS = [
  { icon: "cloud-sun", title: "Weekend weather", text: "What's the weather in Lisbon this weekend?" },
  { icon: "calculator", title: "Split a bill", text: "Split €128.68 four ways with a 12% tip" },
  { icon: "globe", title: "Catch up", text: "Search the web for today's top tech headlines" },
  { icon: "bookmark", title: "Teach me", text: "Remember that I prefer metric units and Python examples" },
];

function renderSuggestions() {
  $("#greeting").textContent = timeOfDay();
  $("#suggestions").replaceChildren(...SUGGESTIONS.map((s) => el("button", {
    class: "suggestion", type: "button",
    onclick: () => {
      input.value = s.text;
      autosize();
      updateComposer();
      submit();
    },
  }, el("span", { class: "tool-icon" }, icon(s.icon, "icon-sm")), el("div", {}, el("strong", { text: s.title }), el("span", { text: s.text })))));
}

function cmdLine(text) {
  const btn = el("button", { class: "icon-btn icon-btn-sm", type: "button", "aria-label": `Copy ${text}`, title: "Copy" }, icon("copy", "icon-sm"));
  btn.addEventListener("click", () => copyText(text).then(() => toast("Copied")));
  return el("div", { class: "cmd" }, el("code", { text }), btn);
}

function renderSetup() {
  const box = $("#setup");
  const h = state.health;
  const suggestions = $("#suggestions");
  if (!h || (h.ok && h.models && h.installed !== false)) {
    box.hidden = true;
    suggestions.hidden = false;
    return;
  }
  suggestions.hidden = true;
  box.hidden = false;
  const retry = el("button", { class: "btn btn-primary", type: "button", onclick: async () => { retry.disabled = true; await refreshHealth(); await loadModels(); retry.disabled = false; } }, icon("refresh-cw", "icon-sm"), "Check again");
  const openSettings = el("button", { class: "btn", type: "button", onclick: () => settings.open("model") }, icon("settings", "icon-sm"), "Model settings");

  if (!h.ok) {
    box.replaceChildren(
      el("div", { class: "setup-head" }, el("span", { class: "tool-icon" }, icon("plug", "icon-sm")),
        el("div", {}, el("h3", { text: "Connect a model" }), el("p", { text: h.error || "Bagley can't reach a model server." }))),
      el("ol", { class: "steps" },
        el("li", {}, "Install ", el("a", { href: "https://ollama.com/download", target: "_blank", rel: "noopener", text: "Ollama" }), " (or start LM Studio, llama.cpp or any OpenAI-compatible server)."),
        el("li", {}, "Download a model with tool support:", cmdLine("ollama pull qwen3:4b")),
        el("li", {}, "Make sure the server is running, then check again. Using another server or port? Change it in model settings."),
      ),
      el("div", { class: "setup-actions" }, retry, openSettings),
    );
    return;
  }

  const missing = h.models && h.installed === false;
  const installed = new Set(state.models.map((m) => m.name));
  const cards = RECOMMENDED_MODELS.filter((m) => !installed.has(m.name)).map((m) => {
    const progress = el("div", { class: "progress", hidden: true }, el("i"));
    const status = el("div", { class: "pull-status" });
    const btn = el("button", { class: "btn btn-sm", type: "button" }, icon("hard-drive-download", "icon-sm"), "Download");
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      const ok = await pullModel(m.name, { progress, status });
      if (ok) await useModel(m.name);
      else btn.disabled = false;
    });
    return el("div", { class: "model-card" }, el("div", { class: "grow" }, el("strong", { text: m.name }), el("span", { text: m.note }), progress, status), btn);
  });
  const installedRow = missing && state.models.length
    ? el("div", { class: "section" },
        el("p", { style: "margin:0 0 8px", text: "Use one you already have:" }),
        el("div", { class: "setup-actions" }, ...state.models.slice(0, 6).map((m) =>
          el("button", { class: "btn btn-sm", type: "button", onclick: () => useModel(m.name) }, icon("cpu", "icon-sm"), m.name))),
      )
    : null;
  box.replaceChildren(...[
    el("div", { class: "setup-head" }, el("span", { class: "tool-icon" }, icon("hard-drive-download", "icon-sm")),
      el("div", {},
        el("h3", { text: missing ? `${h.model} isn't installed` : "Download your first model" }),
        el("p", { text: missing ? "Switch to an installed model, or download one." : "Ollama is running but has no models yet. Pick one to get started." }))),
    installedRow,
    state.canPull && cards.length ? el("div", { class: "model-cards" }, ...cards) : null,
    state.canPull ? null : el("p", { text: "Load a model in your model server, then check again." }),
    el("div", { class: "setup-actions" }, retry, openSettings),
  ].filter(Boolean));
}

async function useModel(name) {
  if (await savePrefs({ model: name })) {
    await Promise.all([loadModels(), refreshHealth()]);
    toast(`Using ${name}`);
  }
}

// Sidebar (mobile) -------------------------------------------------------------------------------

function openSidebar(focusSearch = false) {
  $("#app").classList.add("sidebar-open");
  if (focusSearch) $("#search").focus();
  else $("#sidebar-close").focus({ preventScroll: true });
}
function closeSidebar() {
  $("#app").classList.remove("sidebar-open");
}
$("#sidebar-open").addEventListener("click", () => openSidebar());
$("#sidebar-close").addEventListener("click", closeSidebar);
$("#scrim").addEventListener("click", closeSidebar);
$("#new-chat").addEventListener("click", newChat);
$("#settings-btn").addEventListener("click", () => settings.open());

// Shortcuts --------------------------------------------------------------------------------------

const SHORTCUTS = [
  ["New chat", "mod+shift+o"],
  ["Search chats", "mod+k"],
  ["Focus message box", "/"],
  ["Stop reply", "Esc"],
  ["Edit last message", "↑"],
  ["Copy last reply", "mod+shift+c"],
  ["Toggle Bagley panel", "mod+."],
  ["Settings", "mod+,"],
  ["Keyboard shortcuts", "?"],
];

function showShortcuts() {
  const dialog = $("#shortcuts-dialog");
  dialog.replaceChildren(
    el("div", { class: "dialog-head" }, el("h2", { id: "shortcuts-title", text: "Keyboard shortcuts" }),
      el("button", { class: "icon-btn", type: "button", "aria-label": "Close", onclick: () => dialog.close() }, icon("x"))),
    el("div", { class: "dialog-body" }, el("div", { class: "shortcut-list" },
      ...SHORTCUTS.flatMap(([label, combo]) => [el("span", { text: label }), el("span", {}, ...combo.split(" ").map((c) => el("kbd", { text: c.includes("+") ? kbdLabel(c) : c })))]),
    )),
  );
  dialog.showModal();
}
$("#shortcuts-btn").addEventListener("click", showShortcuts);

document.querySelectorAll("kbd[data-kbd]").forEach((k) => (k.textContent = kbdLabel(k.dataset.kbd)));

addEventListener("keydown", (e) => {
  const mod = isMac ? e.metaKey : e.ctrlKey;
  const typing = e.target.closest("input, textarea, select, [contenteditable]");
  const key = e.key.toLowerCase();
  if (mod && e.shiftKey && key === "o") { e.preventDefault(); newChat(); }
  else if (mod && !e.shiftKey && key === "k") { e.preventDefault(); if (innerWidth <= 860) openSidebar(true); $("#search").focus(); $("#search").select(); }
  else if (mod && key === ",") { e.preventDefault(); settings.open(); }
  else if (mod && key === ".") { e.preventDefault(); setUi("presence", !state.ui.presence); }
  else if (mod && e.shiftKey && key === "c" && !typing) {
    e.preventDefault();
    const text = chat.lastReplyText();
    if (text) copyText(text).then(() => toast("Copied last reply"));
  } else if (e.key === "Escape" && !document.querySelector("dialog[open]")) {
    if (state.run) { e.preventDefault(); chat.stop(); announce("Stopping."); }
    else if (voice.speaking) { voice.stop(); setStatus("idle"); }
    else closeSidebar();
  } else if (!typing && !mod && e.key === "/") { e.preventDefault(); input.focus(); }
  else if (!typing && !mod && e.key === "?") { e.preventDefault(); showShortcuts(); }
});

// Appearance -------------------------------------------------------------------------------------

const systemLight = matchMedia("(prefers-color-scheme: light)");
function applyAppearance() {
  const root = document.documentElement;
  const theme = state.ui.theme === "system" ? (systemLight.matches ? "light" : "dark") : state.ui.theme;
  root.dataset.theme = theme;
  root.style.setProperty("--accent-h", state.ui.accent);
  // Blue-violet hues are dark at equal lightness; lift them to keep text on them readable.
  root.style.setProperty("--accent-l", state.ui.accent >= 230 && state.ui.accent <= 290 ? "70%" : "62%");
  root.classList.toggle("reduce-motion", Boolean(state.ui.reduceMotion));
  document.querySelector('meta[name="theme-color"]').content = theme === "light" ? "#f7f8fa" : "#090b0f";
}
systemLight.addEventListener("change", applyAppearance);

bus.on("ui", (key) => {
  if (["theme", "accent", "reduceMotion"].includes(key)) applyAppearance();
  if (key === "presence") applyPresence();
  if (key === "speak") renderStats();
});

// Boot -------------------------------------------------------------------------------------------

async function boot() {
  applyAppearance();
  applyPresence();
  renderSuggestions();
  updateComposer();
  setStatus("idle");
  try {
    const [info] = await Promise.all([api.get("/api/info"), loadPrefs(), loadTools(), loadMemories(), sidebar.refresh()]);
    state.info = info;
  } catch (err) {
    toast(err.message, { type: "error" });
  }
  await route();
  await Promise.all([refreshHealth(), loadModels()]);
  addEventListener("focus", () => { if (!state.health?.ok) refreshHealth(); });
  if (innerWidth > 860) input.focus();
}

boot();
