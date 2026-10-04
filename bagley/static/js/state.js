// Shared client state. Modules read `state` directly and listen on `bus` for changes.

import { Emitter, storage } from "./util.js";

export const bus = new Emitter();

export const state = {
  conversations: [],
  activeId: null,
  query: "",
  run: null, // { conversationId, status }
  health: null,
  prefs: null, // { values, locked, personas }
  tools: [],
  toolsInfo: null,
  memories: [],
  skills: [],
  automations: [],
  knowledge: null,
  models: [],
  info: null,
  lastStats: null,
  connected: false,
  ui: {
    presence: storage.get("presence", true),
    speak: storage.get("speak", false),
    voice: storage.get("voice", ""),
    rate: storage.get("rate", 1),
    theme: storage.get("theme", "system"),
    accent: storage.get("accent", 188),
    reduceMotion: storage.get("reduceMotion", false),
    desktopNotify: storage.get("desktopNotify", false),
  },
};

export function setUi(key, value) {
  state.ui[key] = value;
  storage.set(key, value);
  bus.emit("ui", key);
}

export const STATUS_TEXT = {
  idle: "Ready",
  listening: "Listening…",
  thinking: "Thinking…",
  reasoning: "Reasoning…",
  writing: "Writing…",
  tool: "Working…",
  approval: "Needs your approval",
  speaking: "Speaking",
  happy: "Done",
  error: "Something went wrong",
  offline: "Offline",
};

export function toolInfo(name) {
  return state.tools.find((t) => t.name === name);
}

const TOOL_ICONS = {
  get_weather: "cloud-sun",
  get_current_time: "clock",
  calculate: "calculator",
  web_search: "globe",
  fetch_webpage: "book-open",
  list_files: "folder-open",
  read_file: "file-text",
  search_files: "text-search",
  write_file: "file-pen-line",
  edit_file: "pencil",
  move_file: "folder-input",
  delete_file: "trash-2",
  make_directory: "folder-plus",
  system_status: "activity",
  open_on_computer: "monitor-up",
  notify_user: "bell",
  set_reminder: "alarm-clock",
  schedule_task: "calendar-clock",
  watch_webpage: "eye",
  list_automations: "list-checks",
  cancel_automation: "timer",
  search_knowledge: "library",
  read_document: "book-open",
  run_python: "code",
  read_skill: "graduation-cap",
  save_skill: "graduation-cap",
  delete_skill: "trash-2",
  load_tools: "plug",
  remember: "bookmark",
  forget: "eraser",
  run_command: "terminal",
};
const CATEGORY_ICONS = { web: "globe", files: "file-text", memory: "bookmark", system: "terminal", mcp: "plug", utility: "wrench", automation: "calendar-clock", knowledge: "library", skills: "graduation-cap" };

export function toolIcon(name, category) {
  return TOOL_ICONS[name] || CATEGORY_ICONS[category || toolInfo(name)?.category] || "wrench";
}

/** Fill a summary template such as "Search the web for “{query}”" from call arguments. */
export function toolSummary(name, args = {}, template) {
  const tpl = template ?? toolInfo(name)?.summary ?? "";
  if (!tpl) return name.replace(/_/g, " ").replace(/^\w/, (c) => c.toUpperCase());
  const short = (v) => {
    const s = typeof v === "string" ? v : Array.isArray(v) ? v.join(", ") : JSON.stringify(v);
    return s.length > 64 ? `${s.slice(0, 61)}…` : s;
  };
  return tpl
    .replace(/(\s+(?:in|for|of|on|to|at))?(\s*)\{(\w+)\}/g, (_, prep, space, key) =>
      args[key] !== undefined && args[key] !== "" ? `${prep || ""}${space}${short(args[key])}` : "",
    )
    .trim();
}
