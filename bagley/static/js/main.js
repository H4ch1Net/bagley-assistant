// Entry point: wires state, socket, bar, avatar, thread, sidebar, settings and shortcuts together.

import { api, ChatSocket } from "./api.js";
import { Approvals } from "./approvals.js";
import { Attachments } from "./attachments.js";
import { mountAvatars } from "./avatar.js";
import { Bar, currentMode } from "./bar.js";
import { Chat } from "./chat.js";
import "./pickers.js";
import { loadRoutines, saveFromChat } from "./routines.js";
import { pullModel, RECOMMENDED_MODELS, savePrefs, Settings } from "./settings.js";
import { Sidebar } from "./sidebar.js";
import { bus, setUi, state, STATUS_TEXT } from "./state.js";
import { applyAppearance, syncFromServer } from "./theme.js";
import { announce, closeMenus, openMenu, toast } from "./ui.js";
import { $, copyText, el, formatBytes, formatTokens, icon, isMac, kbdLabel, relTime, timeOfDay } from "./util.js";
import { voice } from "./voice.js";

const avatars = mountAvatars();
const socket = new ChatSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/api/ws`);
const input = $("#composer-input");

// Status -----------------------------------------------------------------------------------------

const BUSY = ["thinking", "reasoning", "tool", "writing"];
let settleTimer;
let currentStatus = "idle";

/** Another machine answers (a GPU desktop, Claude) while this one's model server doesn't. */
const routedElsewhere = () => !!state.route?.ok && state.route.machine_id !== "local";

function setStatus(name, label, tool = "") {
  clearTimeout(settleTimer);
  const offline = !state.connected || (state.health && !state.health.ok && !routedElsewhere());
  if (name === "idle" && offline) name = "offline";
  if (name === "idle" && !state.run && state.activity && state.activity.state !== "idle") {
    // Another client (the desktop overlay, the phone, an automation) has Bagley busy.
    name = state.activity.state;
    tool = state.activity.tool;
  }
  currentStatus = name;
  const text = label || (name === "offline" ? offlineText() : STATUS_TEXT[name] || name);
  avatars.setState(name);
  if (tool) avatars.setTag(tool);
  bar.setState(name, tool);
  const pill = $("#presence-state");
  pill.dataset.state = name;
  pill.replaceChildren(...(BUSY.includes(name) ? [el("span", { class: "spinner" })] : []), el("span", { class: BUSY.includes(name) ? "dots" : "", text }));
  $("#tb-status").textContent = text;
  if (name === "happy") settleTimer = setTimeout(() => setStatus("idle"), 1600);
  if (name === "error") settleTimer = setTimeout(() => setStatus("idle"), 3200);
}

function offlineText() {
  if (!state.connected) return "Reconnecting";
  return state.health?.error ? "Model server offline" : "No signal";
}

// Bar, chat, sidebar, settings -----------------------------------------------------------------

const bar = new Bar({
  onMode: (id) => setMode(id),
  onNode: () => settings.open("model"),
});

const chat = new Chat({
  socket,
  avatars,
  voice,
  setStatus,
  onRunBegin: () => {
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
    await Promise.all([loadModels(), refreshHealth(), loadMachines()]);
  },
  onMemoriesChanged: loadMemories,
  onAutomationsChanged: () => loadAutomations(),
  openChat: (id) => navigate(id),
});

const approvals = new Approvals({
  isShownInThread: (id) => chat.hasCard(id),
  openChat: (id) => navigate(id),
});

// Data -------------------------------------------------------------------------------------------

async function loadPrefs() {
  state.prefs = await api.get("/api/preferences");
  bus.emit("prefs");
  syncFromServer(state.prefs.values.appearance);
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

async function loadKnowledge() {
  try {
    state.knowledge = await api.get("/api/knowledge");
  } catch {
    return;
  }
  renderStats();
  renderBootlog();
}

async function loadAutomations() {
  try {
    state.automations = await api.get("/api/automations");
  } catch {
    return;
  }
  renderStats();
  renderBootlog();
  if (settings.dialog.open && settings.tab === "automations") settings.renderAutomationList();
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

let machinesTimer;
async function loadMachines() {
  clearTimeout(machinesTimer);
  try {
    state.machines = await api.get("/api/machines");
    state.route = await api.get("/api/machines/route");
  } catch {
    state.route = null;
  }
  renderNode();
  renderStats();
  if (state.health && !state.health.ok) bus.emit("health"); // The route may cover for it.
  machinesTimer = setTimeout(loadMachines, 45000);
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
  renderBootlog();
  if (!state.run) setStatus("idle");
});

bus.on("prefs", () => {
  renderModelButton();
  renderStats();
  renderPrivacy();
  bar.renderModes();
  renderSuggestions();
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
  if (location.hash === "#/approvals") {
    history.replaceState(null, "", "#/");
    approvals.load();
  }
  if (!id) {
    state.activeId = null;
    state.activeMode = "default";
    chat.render([]);
    setTitle(null);
    sidebar.render();
    afterRoute();
    return;
  }
  try {
    const data = await api.get(`/api/conversations/${id}`);
    state.activeId = id;
    state.activeMode = data.conversation.mode || "default";
    sidebar.update({ id, unread: 0 });
    chat.render(data.messages);
    setTitle(data.conversation.title);
    const last = [...data.messages].reverse().find((m) => m.role === "assistant" && m.meta?.model);
    state.lastStats = last ? { tokens_per_second: last.meta.tokens_per_second, prompt_tokens: last.meta.prompt_tokens, machine: last.meta.machine, model: last.meta.model } : null;
  } catch (err) {
    toast(err.status === 404 ? "That chat no longer exists." : err.message, { type: "error" });
    history.replaceState(null, "", "#/");
    state.activeId = null;
    state.activeMode = "default";
    chat.render([]);
    setTitle(null);
  }
  sidebar.render();
  afterRoute();
}

function afterRoute() {
  renderStats();
  bar.renderModes();
  renderSuggestions();
  approvals.render();
}

addEventListener("popstate", route);
document.addEventListener("visibilitychange", () => {
  if (!document.hidden) document.title = document.title.replace(/^● /, "");
});

const titleBtn = $("#title-btn");

function setTitle(title) {
  const btn = titleBtn;
  btn.textContent = title || "New chat";
  btn.disabled = !state.activeId;
  $("#export-btn").disabled = !state.activeId;
  $("#routine-btn").hidden = !state.activeId;
  document.title = title ? `${title} · Bagley` : "Bagley";
  document.dispatchEvent(new CustomEvent("bagley:chat", { detail: { id: state.activeId } }));
}

function newChat() {
  navigate(null);
  input.focus();
}

// Modes ------------------------------------------------------------------------------------------

async function setMode(id) {
  const mode = state.prefs?.modes.find((m) => m.id === id);
  if (!mode) return;
  if (state.activeId) {
    try {
      const conv = await api.patch(`/api/conversations/${state.activeId}`, { mode: id });
      state.activeMode = conv.mode;
      sidebar.upsert(conv);
    } catch (err) {
      toast(err.message, { type: "error" });
      return;
    }
  } else {
    state.nextMode = id;
  }
  bar.renderModes();
  renderSuggestions();
  announce(`${mode.label} mode`);
  toast(`${mode.label} mode${state.activeId ? " for this chat" : " for the next chat"}`, { duration: 1800 });
}

// Socket events ----------------------------------------------------------------------------------

socket.on("open", () => {
  state.connected = true;
  refreshHealth();
  updateComposer();
  approvals.load();
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
    state.activeMode = ev.conversation.mode || "default";
    state.nextMode = null;
    history.replaceState(null, "", `#/c/${ev.conversation.id}`);
    setTitle(ev.conversation.title);
    sidebar.render();
    bar.renderModes();
  }
});

socket.on("notification", (ev) => {
  const open = ev.conversation_id ? { label: "Open", run: () => navigate(ev.conversation_id) } : undefined;
  toast(ev.body ? `${ev.title}: ${ev.body}` : ev.title, { action: open, type: ev.level === "error" || ev.level === "critical" ? "error" : "info", duration: 9000 });
  avatars.pulse(1);
  announce(`${ev.title}. ${ev.body || ""}`);
  if (state.ui.desktopNotify && "Notification" in window && Notification.permission === "granted" && document.hidden) showSystemNotification(ev);
  sidebar.refresh();
});

/** Through the service worker when there is one (Android Chrome only allows that), else directly. */
async function showSystemNotification(ev) {
  const url = ev.conversation_id ? `/#/c/${ev.conversation_id}` : "/";
  const options = { body: ev.body || "", icon: "/static/icons/icon-192.png", tag: ev.conversation_id || undefined, data: { url } };
  try {
    const registration = await navigator.serviceWorker?.getRegistration?.();
    if (registration) return registration.showNotification(ev.title, options);
  } catch {
    /* Fall through to a page notification. */
  }
  const n = new Notification(ev.title, options);
  n.onclick = () => {
    window.focus();
    if (ev.conversation_id) navigate(ev.conversation_id);
    n.close();
  };
}

navigator.serviceWorker?.addEventListener?.("message", (e) => {
  if (e.data?.type === "navigate" && typeof e.data.url === "string" && e.data.url.startsWith("/")) {
    location.hash = new URL(e.data.url, location.origin).hash || "#/";
  }
});

socket.on("conversations.changed", () => sidebar.refresh());
socket.on("automations.changed", () => loadAutomations());
socket.on("knowledge.changed", (ev) => {
  state.knowledge = ev.status;
  renderStats();
  settings.renderKnowledge();
});

socket.on("title", (ev) => {
  sidebar.update({ id: ev.conversation_id, title: ev.title });
  if (ev.conversation_id === state.activeId) setTitle(ev.title);
});

socket.on("model", (ev) => {
  state.runModel = ev.model;
  state.runMachine = ev.machine || "";
  renderNode();
  renderStats();
});

socket.on("activity", (ev) => {
  state.activity = ev;
  if (!state.run && ["idle", "offline", "happy", "error"].includes(currentStatus)) setStatus(ev.state === "idle" ? "idle" : ev.state, undefined, ev.tool);
  else if (!state.run && ev.state === "idle") setStatus("idle");
  if (ev.machine && !state.run) {
    state.runMachine = ev.machine;
    renderNode();
  }
});

socket.on("approval.pending", (ev) => approvals.add(ev));
socket.on("routines.changed", () => loadRoutines());
socket.on("approval.resolved", (ev) => approvals.remove(ev.id));

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
  $("#composer").classList.toggle("running", running);
  btn.replaceChildren(...(running ? [el("span", { class: "spinner" }), "Stop"] : ["Send"]));
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
  const images = attachments.images();
  const typed = input.value.trim() || (images.length ? "What's in this image?" : attachments.ready.length ? "Take a look at the attached file." : "");
  if (!typed) return;
  if (chat.send(typed + attachments.note(), { images, mode: state.activeId ? null : currentMode() })) {
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

// Voice input: the browser's recognition, or whisper.cpp on the server when chosen.
{
  const mic = $("#mic-btn");
  mic.hidden = !voice.canListen;
  bus.on("prefs", () => (mic.hidden = !voice.canListen));
  let stopListening = null;
  mic.addEventListener("click", () => {
    if (stopListening) {
      stopListening();
      return;
    }
    const before = input.value ? `${input.value.trimEnd()} ` : "";
    mic.classList.add("recording");
    mic.setAttribute("aria-pressed", "true");
    setStatus("listening", "Listening");
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

/** Embedding models can't chat; keep them out of the chat model pickers. */
function chatModels() {
  return state.models.filter((m) => !/embed|minilm|bge-|e5-|gte-/i.test(m.name));
}

function currentModel() {
  return state.prefs?.values.model || state.health?.model || "";
}

function renderModelButton() {
  const name = (state.run && state.runModel) || state.route?.model || currentModel();
  $("#model-name").textContent = name || (state.health?.ok ? "No model" : "Model");
  const btn = $("#model-btn");
  btn.title = state.prefs?.locked.includes("model") ? "Model set by BAGLEY_MODEL" : "Switch model";
}

$("#model-btn").addEventListener("click", () => {
  const picker = $("#model-picker");
  if (picker.querySelector(".menu")) return closeMenus();
  loadModels();
  openMenu(picker, (menu) => {
    menu.classList.add("menu-left");
    const lockedModel = state.prefs?.locked.includes("model");
    menu.append(el("div", { class: "menu-label", text: `Model // ${state.prefs?.machine || "this machine"}` }));
    if (!state.models.length) {
      menu.append(el("div", { class: "menu-note", text: state.health?.ok ? "No models installed yet." : "Model server is not reachable." }));
    }
    const current = currentModel();
    for (const m of chatModels()) {
      menu.append(el("button", {
        class: "menu-item", type: "button", role: "option", "aria-selected": String(m.name === current), disabled: lockedModel,
        onclick: async () => {
          menu.close();
          if (m.name !== current && (await savePrefs({ model: m.name }))) {
            await Promise.all([refreshHealth(), loadMachines()]);
            toast(`Switched to ${m.name}`);
          }
        },
      },
      icon(m.name === current ? "check" : "cpu", "icon-sm"),
      el("span", { class: "grow mono", text: m.name }),
      el("span", { class: "subtle", style: "font-size:.72rem", text: [m.parameter_size, formatBytes(m.size)].filter(Boolean).join(" · ") })));
    }
    const machines = state.prefs?.values.machines || [];
    if (machines.length) {
      menu.append(el("div", { class: "menu-sep" }), el("div", { class: "menu-label", text: "Routing" }));
      const routes = [["auto", "Automatic", "GPU first, then here, then cloud"], ["local", state.prefs.machine, "Only this machine"], ...machines.map((m) => [m.id, m.name, `${m.role.toUpperCase()} · ${m.model || "auto model"}`])];
      for (const [id, label, note] of routes) {
        const selected = (state.prefs.values.routing || "auto") === id;
        menu.append(el("button", {
          class: "menu-item", type: "button", role: "option", "aria-selected": String(selected),
          onclick: async () => {
            menu.close();
            if (await savePrefs({ routing: id })) {
              await loadMachines();
              toast(`Routing: ${label}`);
            }
          },
        }, icon(selected ? "check" : id === "auto" ? "zap" : "hard-drive", "icon-sm"), el("span", { class: "grow", text: label }), el("span", { class: "subtle", style: "font-size:.72rem", text: note })));
      }
    }
    menu.append(el("div", { class: "menu-sep" }),
      el("button", { class: "menu-item", type: "button", onclick: () => { menu.close(); settings.open("model"); } }, icon("settings", "icon-sm"), el("span", { class: "grow", text: "Model and machines…" })));
  });
});

// Title rename -----------------------------------------------------------------------------------

titleBtn.addEventListener("click", () => {
  const btn = titleBtn;
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

$("#routine-btn").addEventListener("click", () => state.activeId && saveFromChat(state.activeId));
$("#export-btn").addEventListener("click", () => {
  if (state.activeId) location.href = `/api/conversations/${state.activeId}/export`;
});
$("#home-link").addEventListener("click", (e) => {
  e.preventDefault();
  newChat();
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
    el("span", { class: "k", text: key }), valueNode, extra);
}

function machineName() {
  return (state.run && state.runMachine) || state.lastStats?.machine || state.route?.machine || state.prefs?.machine || "";
}

function renderNode() {
  const name = machineName();
  const routed = state.route;
  bar.setNode(name, routed ? Boolean(routed.ok) : state.health ? Boolean(state.health.ok) : undefined);
  const role = routed?.role ? ` // ${routed.role.toUpperCase()}` : "";
  $("#presence-node").textContent = name ? `Node ${name}${role}` : "";
  renderModelButton();
}

function renderStats() {
  const stats = $("#stats");
  if (!stats) return;
  const modelName = state.runModel && state.run ? state.runModel : state.lastStats?.model || state.route?.model || currentModel();
  const info = state.models.find((m) => m.name === modelName);
  const s = state.lastStats || {};
  const ctxLimit = state.prefs?.values.context_tokens || 8192;
  const used = s.prompt_tokens || 0;
  const pct = Math.min(100, (used / ctxLimit) * 100);
  const enabledTools = state.tools.filter((t) => t.enabled).length;
  const asking = state.tools.filter((t) => t.permission === "ask").length;
  const mode = state.prefs?.modes.find((m) => m.id === currentMode());
  const ctxBar = el("div", { class: "ctx-bar", title: `${used} of ${ctxLimit} tokens` }, el("i", { style: `width:${pct}%` }));
  const machines = state.machines?.machines || [];
  const online = machines.filter((m) => m.ok).length;
  stats.replaceChildren(
    stat("cpu", "Model", el("span", { class: "v mono", text: modelName ? `${modelName}${info?.parameter_size ? ` · ${info.parameter_size}` : ""}` : "–" }), () => settings.open("model")),
    stat("hard-drive", "Node", el("span", { class: `v${state.route && !state.route.ok ? " bad" : ""}`, text: machineName() ? `${machineName()}${machines.length > 1 ? ` · ${online}/${machines.length} online` : ""}` : "–" }), () => settings.open("model")),
    stat("sparkles", "Mode", el("span", { class: "v", text: mode ? mode.label : "–" })),
    stat("gauge", "Speed", el("span", { class: "v", text: s.tokens_per_second ? `${Math.round(s.tokens_per_second)} tok/s` : "–" })),
    el("div", { class: "stat", style: "flex-direction:column;align-items:stretch;gap:0" },
      el("div", { style: "display:flex;gap:10px;align-items:center" }, el("span", { class: "k", text: "Context" }),
        el("span", { class: "v", text: used ? `${formatTokens(used)} / ${formatTokens(ctxLimit)}` : `${formatTokens(ctxLimit)} window` })),
      used ? ctxBar : null,
    ),
    stat("wrench", "Tools", el("span", { class: "v", text: `${enabledTools} enabled${asking ? ` · ${asking} ask` : ""}` }), () => settings.open("tools")),
    stat("calendar-clock", "Automations", el("span", { class: "v", text: automationText() }), () => settings.open("automations")),
    stat("library", "Knowledge", el("span", { class: "v", text: state.knowledge ? (state.knowledge.state === "indexing" ? "Indexing…" : `${state.knowledge.files} files`) : "–" }), () => settings.open("knowledge")),
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

function automationText() {
  const active = state.automations.filter((a) => a.enabled && a.next_run);
  if (!active.length) return "None";
  const next = Math.min(...active.map((a) => a.next_run));
  return `${active.length} · ${relTime(next)}`;
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
  const role = state.route?.role;
  const cloud = role === "cloud" || (!role && !isLocalServer());
  const gpu = role === "gpu";
  const text = cloud ? "Hosted model · leaves your network" : gpu ? `Your GPU · ${machineName()} over your network` : "Local model · stays on this machine";
  $("#presence-foot").replaceChildren(icon(cloud ? "globe" : "lock", "icon-xs"), text);
  $("#privacy").replaceChildren(icon(cloud ? "globe" : "lock", "icon-xs"), cloud ? "Hosted model" : gpu ? `Node ${machineName()}` : "Runs locally");
}

function renderConnection() {
  const dot = $("#conn-dot");
  const label = $("#conn-label");
  const h = state.health;
  if (!state.connected) {
    dot.className = "dot warn";
    label.textContent = "Reconnecting";
  } else if (!h) {
    dot.className = "dot";
    label.textContent = "Checking model server";
  } else if (h.ok && h.models) {
    dot.className = "dot ok";
    label.textContent = `Online // ${h.models} model${h.models === 1 ? "" : "s"}`;
  } else if (h.ok) {
    dot.className = "dot warn";
    label.textContent = "No models installed";
  } else if (routedElsewhere()) {
    dot.className = "dot ok";
    label.textContent = `Online // ${state.route.machine}`;
  } else {
    dot.className = "dot bad";
    label.textContent = "Model server offline";
  }
  $("#conn-status").title = h?.error ? `${h.error} ${h.hint || ""}` : `Model server: ${state.prefs?.values.base_url || ""}`;
}

$("#conn-status").addEventListener("click", () => settings.open("model"));

// Empty state: greeter, suggestions, boot log and onboarding -------------------------------------

const SUGGESTIONS = [
  { icon: "cloud-sun", title: "Weekend weather", text: "What's the weather in Lisbon this weekend?" },
  { icon: "calculator", title: "Split a bill", text: "Split €128.68 four ways with a 12% tip" },
  { icon: "globe", title: "Catch up", text: "Search the web for today's top tech headlines" },
  { icon: "bookmark", title: "Teach me", text: "Remember that I prefer metric units and Python examples" },
];
const STARTER_ICONS = ["sparkles", "list-checks", "book-open", "zap"];

function renderSuggestions() {
  $("#greeting").textContent = `${timeOfDay()}${state.info?.user ? `, ${state.info.user}` : ""}.`;
  const mode = state.prefs?.modes.find((m) => m.id === currentMode());
  const starters = mode && mode.id !== "default" && mode.starters?.length
    ? mode.starters.map((s, i) => ({ icon: STARTER_ICONS[i % STARTER_ICONS.length], title: s.title, text: s.text }))
    : SUGGESTIONS;
  if (mode && mode.id !== "default") $("#empty-lead").textContent = `${mode.label} mode. ${mode.description}`;
  else $("#empty-lead").textContent = "Ask anything. Bagley can search the web, read pages, check the weather, do exact maths, work with files in your workspace and remember what matters to you.";
  $("#suggestions").replaceChildren(...starters.map((s) => el("button", {
    class: "suggestion", type: "button",
    onclick: () => {
      input.value = s.text;
      autosize();
      updateComposer();
      if (s.text.endsWith(": ") || s.text.endsWith(" ")) input.focus();
      else submit();
    },
  }, el("span", { class: "tool-icon" }, icon(s.icon, "icon-sm")), el("div", {}, el("strong", { text: s.title }), el("span", { text: s.text })))));
}

/** The greeter's log, in the ctOS voice, from real state. */
function renderBootlog() {
  const box = $("#bootlog");
  const h = state.health;
  const lines = [];
  const machine = state.prefs?.machine || state.info?.machine || "LOCAL";
  lines.push(["REGION_LINK_ESTABLISHED : ", machine, ""]);
  if (h?.ok) lines.push(["MODEL_SERVER // ", `${(h.provider || "").toUpperCase()} ${h.version || ""}`.trim(), " OK", "ok"]);
  else if (h) lines.push(["MODEL_SERVER // ", "NO SIGNAL", "", "bad"]);
  if (h?.model) lines.push(["MODEL_LOADED // ", h.model, ""]);
  const machines = state.machines?.machines || [];
  for (const m of machines.filter((x) => x.id !== "local")) lines.push([`NODE ${m.name} <-> `, m.ok ? `${m.latency_ms ?? "?"}MS` : "OFFLINE", "", m.ok ? "ok" : "bad"]);
  if (state.knowledge) lines.push(["KNOWLEDGE_INDEX // ", `${state.knowledge.files} FILES`, ""]);
  const armed = state.automations.filter((a) => a.enabled).length;
  lines.push(["AUTOMATIONS // ", `${armed} ARMED`, ""]);
  lines.push(["◈ [BAGLEY] ", "using Protocol::", "BAGLEY_ASSIST"]);
  box.replaceChildren(...lines.map(([label, value, tail, cls]) => el("li", {}, el("span", { class: "g", text: label }), el("span", { class: cls || "", text: value }), tail ? el("span", { class: "g", text: tail }) : null)));
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
  if (!h || (h.ok && h.models && h.installed !== false) || (!h.ok && routedElsewhere())) {
    box.hidden = true;
    suggestions.hidden = false;
    return;
  }
  suggestions.hidden = true;
  box.hidden = false;
  const retry = el("button", { class: "btn btn-primary", type: "button", onclick: async () => { retry.disabled = true; await refreshHealth(); await loadModels(); retry.disabled = false; } }, icon("refresh-cw", "icon-sm"), "Check again");
  const openSettings = el("button", { class: "btn", type: "button", onclick: () => settings.open("model") }, icon("settings", "icon-sm"), "Model settings");
  const useKey = el("button", { class: "btn", type: "button", onclick: () => {
    settings.open("model");
    $("#settings-panel .hosted")?.scrollIntoView({ block: "center" });
  } }, icon("key-round", "icon-sm"), "Use an API key");

  if (!h.ok) {
    box.replaceChildren(
      el("div", { class: "setup-head" }, el("span", { class: "tool-icon" }, icon("plug", "icon-sm")),
        el("div", {}, el("h3", { text: "Connect a model" }), el("p", { text: h.error || "Bagley can't reach a model server." }))),
      el("ol", { class: "steps" },
        el("li", {}, "Install ", el("a", { href: "https://ollama.com/download", target: "_blank", rel: "noopener", text: "Ollama" }), " (or start LM Studio, llama.cpp or any OpenAI-compatible server)."),
        el("li", {}, "Download a model with tool support:", cmdLine("ollama pull qwen3:4b")),
        el("li", {}, "Make sure the server is running, then check again. Using another server, port or your GPU machine? Change it in model settings."),
        el("li", {}, "No local model? Use Claude or another hosted API with your own key."),
      ),
      el("div", { class: "setup-actions" }, retry, openSettings, useKey),
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
    await Promise.all([loadModels(), refreshHealth(), loadMachines()]);
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
$("#automations-btn").addEventListener("click", () => settings.open("automations"));

// Shortcuts --------------------------------------------------------------------------------------

const SHORTCUTS = [
  ["New chat", "mod+shift+o"],
  ["Search chats", "mod+k"],
  ["Focus message box", "/"],
  ["Stop reply", "Esc"],
  ["Edit last message", "↑"],
  ["Copy last reply", "mod+shift+c"],
  ["Next mode", "mod+shift+m"],
  ["Toggle Bagley panel", "mod+."],
  ["Settings", "mod+,"],
  ["Keyboard shortcuts", "?"],
];

function showShortcuts() {
  const dialog = $("#shortcuts-dialog");
  dialog.replaceChildren(
    el("div", { class: "dialog-head" }, el("h2", { id: "shortcuts-title" }, el("span", { class: "barcode" }), "Keyboard shortcuts"),
      el("button", { class: "icon-btn", type: "button", "aria-label": "Close", onclick: () => dialog.close() }, icon("x"))),
    el("div", { class: "dialog-body" }, el("div", { class: "shortcut-list" },
      ...SHORTCUTS.flatMap(([label, combo]) => [el("span", { text: label }), el("span", {}, ...combo.split(" ").map((c) => el("kbd", { text: c.includes("+") ? kbdLabel(c) : c })))]),
    )),
  );
  dialog.showModal();
}

document.querySelectorAll("kbd[data-kbd]").forEach((k) => (k.textContent = kbdLabel(k.dataset.kbd)));

addEventListener("keydown", (e) => {
  const mod = isMac ? e.metaKey : e.ctrlKey;
  const typing = e.target.closest("input, textarea, select, [contenteditable]");
  const key = e.key.toLowerCase();
  if (mod && e.shiftKey && key === "o") { e.preventDefault(); newChat(); }
  else if (mod && !e.shiftKey && key === "k") { e.preventDefault(); if (innerWidth <= 860) openSidebar(true); $("#search").focus(); $("#search").select(); }
  else if (mod && key === ",") { e.preventDefault(); settings.open(); }
  else if (mod && key === ".") { e.preventDefault(); setUi("presence", !state.ui.presence); }
  else if (mod && e.shiftKey && key === "m") {
    e.preventDefault();
    const modes = state.prefs?.modes || [];
    const i = modes.findIndex((m) => m.id === currentMode());
    if (modes.length) setMode(modes[(i + 1) % modes.length].id);
  } else if (mod && e.shiftKey && key === "c" && !typing) {
    e.preventDefault();
    const text = chat.lastReplyText();
    if (text) copyText(text).then(() => toast("Copied last reply"));
  } else if (e.key === "Escape" && !document.querySelector("body > dialog[open]")) {
    if (state.run) { e.preventDefault(); chat.stop(); announce("Stopping."); }
    else if (voice.speaking) { voice.stop(); setStatus("idle"); }
    else closeSidebar();
  } else if (!typing && !mod && e.key === "/") { e.preventDefault(); input.focus(); }
  else if (!typing && !mod && e.key === "?") { e.preventDefault(); showShortcuts(); }
});

// Appearance -------------------------------------------------------------------------------------

const systemLight = matchMedia("(prefers-color-scheme: light)");
function applyTheme() {
  const root = document.documentElement;
  const theme = state.ui.theme === "system" ? (systemLight.matches ? "light" : "dark") : state.ui.theme;
  root.dataset.theme = theme;
  root.classList.toggle("reduce-motion", Boolean(state.ui.reduceMotion));
  applyAppearance();
}
systemLight.addEventListener("change", applyTheme);

bus.on("ui", (key) => {
  if (["theme", "reduceMotion"].includes(key)) applyTheme();
  if (key === "presence") applyPresence();
  if (key === "speak") renderStats();
});

// Installable app (phone over Tailscale) -------------------------------------------------------

if ("serviceWorker" in navigator && (location.protocol === "https:" || location.hostname === "localhost")) {
  navigator.serviceWorker.register("/sw.js", { scope: "/" }).catch(() => {});
}

// Boot -------------------------------------------------------------------------------------------

async function boot() {
  applyTheme();
  applyPresence();
  renderSuggestions();
  renderBootlog();
  updateComposer();
  setStatus("idle");
  bar.pollStats();
  try {
    const [info] = await Promise.all([api.get("/api/info"), loadPrefs(), loadTools(), loadMemories(), loadAutomations(), loadKnowledge(), loadRoutines(), sidebar.refresh()]);
    state.info = info;
    document.documentElement.style.setProperty("--who", JSON.stringify((info.user || "operator").toUpperCase()));
    renderSuggestions();
  } catch (err) {
    toast(err.message, { type: "error" });
  }
  await route();
  await Promise.all([refreshHealth(), loadModels(), loadMachines()]);
  addEventListener("focus", () => { if (!state.health?.ok) refreshHealth(); });
  if (innerWidth > 860) input.focus();
}

boot();
