// Appearance: the ctOS palette as editable tokens, presets, type scale and chrome switches.
// Colours are kept per theme (dark and light) and applied as --c-* custom properties on the
// root, which every component reads through the semantic tokens in app.css. The look is
// saved in this browser and synced to the server so other devices and the overlay match.

import { api } from "./api.js";
import { bus, state } from "./state.js";
import { debounce, storage } from "./util.js";

export const TOKENS = [
  { key: "c-ground", label: "Ground", help: "Page background" },
  { key: "c-surface", label: "Surface", help: "Messages, code, terminals" },
  { key: "c-raised", label: "Raised", help: "Hover and selected rows" },
  { key: "c-line", label: "Lines", help: "Subtle separators, tracks" },
  { key: "c-muted", label: "Muted text", help: "Labels and dividers" },
  { key: "c-dim", label: "Dim text", help: "Secondary text" },
  { key: "c-body", label: "Body text", help: "Message text" },
  { key: "c-white", label: "Bright text", help: "Values, titles, corner brackets" },
  { key: "c-chrome", label: "Chrome", help: "Bar outline, frames, field border" },
  { key: "c-accent", label: "Accent", help: "Buttons, focus, selection, meters" },
  { key: "c-ok", label: "Success", help: "Online, OK, done" },
  { key: "c-err", label: "Error", help: "Failed, critical, denied" },
  { key: "c-await", label: "Await", help: "Approvals waiting" },
  { key: "c-string", label: "Strings", help: "Inline code" },
  { key: "c-avatar", label: "Avatar", help: "Bagley's graph" },
  { key: "c-face", label: "Avatar viewport", help: "Behind the graph" },
];

// Mirrors the defaults in app.css.
export const DEFAULTS = {
  dark: {
    "c-ground": "#0e0e0e", "c-surface": "#121212", "c-raised": "#202020", "c-line": "#2a2a2a",
    "c-muted": "#7a7a7a", "c-dim": "#c3c3c3", "c-body": "#cacaca", "c-white": "#ffffff",
    "c-chrome": "#d9d9d9", "c-accent": "#d9d9d9", "c-ok": "#00fa9a", "c-err": "#fc3e38",
    "c-await": "#ffffff", "c-string": "#a6ffc9", "c-avatar": "#d9d9d9", "c-face": "#0e0e0e",
  },
  light: {
    "c-ground": "#e6e6e6", "c-surface": "#f1f1f1", "c-raised": "#d9d9d9", "c-line": "#cacaca",
    "c-muted": "#4a4a4a", "c-dim": "#2a2a2a", "c-body": "#202020", "c-white": "#0e0e0e",
    "c-chrome": "#202020", "c-accent": "#202020", "c-ok": "#00704a", "c-err": "#c42a24",
    "c-await": "#0e0e0e", "c-string": "#00704a", "c-avatar": "#d9d9d9", "c-face": "#0e0e0e",
  },
}; // prettier-ignore

export const PRESETS = [
  { id: "ctos", name: "ctOS // b1t", note: "Gray on black. Green means OK.", dark: {}, light: {} },
  { id: "upstream", name: "ctOS upstream", note: "The original TSM-061 green.", dark: { "c-ok": "#1bfd9c" }, light: {} },
  {
    id: "monoglow", name: "Mono Glow", note: "The kitty theme: softer gray, mint strings.",
    dark: { "c-ground": "#121212", "c-surface": "#181818", "c-raised": "#2a2a2a", "c-line": "#333333", "c-body": "#cccccc", "c-dim": "#b4b4b4", "c-chrome": "#dddddd", "c-accent": "#dddddd", "c-face": "#121212" },
    light: {},
  },
  {
    id: "white", name: "White chrome", note: "Brighter chrome and frames.",
    dark: { "c-chrome": "#ffffff", "c-accent": "#ffffff", "c-avatar": "#ffffff", "c-string": "#f1f1f1" },
    light: {},
  },
]; // prettier-ignore

// Earlier versions offered coloured presets; ctOS has two hues, for state only.
const RETIRED = new Set(["signal", "amber", "phosphor", "dedsec"]);

// Accent choices stay in the ctOS grays.
export const ACCENTS = [
  { color: "#d9d9d9", name: "ctOS gray" },
  { color: "#ffffff", name: "White" },
  { color: "#cacaca", name: "Dim" },
  { color: "#7a7a7a", name: "Secondary" },
];

const BASE = { preset: "ctos", colors: { dark: {}, light: {} }, scale: 14, reading: "mono", brackets: true, grid: true, caps: true, updated: 0 };
const HEX = /^#[0-9a-f]{6}$/i;

export function currentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

/** Clean an appearance object from storage, the server or an import. */
export function normalize(raw) {
  const look = { ...BASE, ...(raw && typeof raw === "object" ? raw : {}) };
  const colors = { dark: {}, light: {} };
  for (const theme of ["dark", "light"]) {
    for (const [key, value] of Object.entries(look.colors?.[theme] || {})) {
      if (TOKENS.some((t) => t.key === key) && HEX.test(String(value))) colors[theme][key] = String(value).toLowerCase();
    }
  }
  look.colors = colors;
  look.scale = Math.min(18, Math.max(12, Number(look.scale) || 14));
  look.reading = look.reading === "sans" ? "sans" : "mono";
  for (const key of ["brackets", "grid", "caps"]) look[key] = look[key] !== false;
  if (RETIRED.has(look.preset)) {
    look.preset = "ctos";
    look.colors = { dark: {}, light: {} };
  }
  look.preset = PRESETS.some((p) => p.id === look.preset) ? look.preset : "custom";
  look.updated = Number(look.updated) || 0;
  return look;
}

let look = normalize(storage.get("appearance", null));

export function appearance() {
  return look;
}

/** The colour a token has right now in ``theme`` (an override or the default). */
export function tokenValue(key, theme = currentTheme()) {
  return look.colors[theme][key] || DEFAULTS[theme][key];
}

export function applyAppearance() {
  const root = document.documentElement;
  const overrides = look.colors[currentTheme()];
  for (const { key } of TOKENS) {
    if (overrides[key]) root.style.setProperty(`--${key}`, overrides[key]);
    else root.style.removeProperty(`--${key}`);
  }
  root.style.setProperty("--fs", `${look.scale}px`);
  if (look.reading === "sans") root.style.setProperty("--font-reading", "var(--font-sans)");
  else root.style.removeProperty("--font-reading");
  root.classList.toggle("no-brackets", !look.brackets);
  root.classList.toggle("no-grid", !look.grid);
  root.classList.toggle("no-caps", !look.caps);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = tokenValue("c-ground");
}

const pushToServer = debounce(async () => {
  if (!state.prefs) return;
  try {
    await api.put("/api/preferences", { appearance: look });
  } catch {
    /* Offline: the next change or load syncs it. */
  }
}, 900);

/** Change the look; ``sync`` false keeps it in this browser only (used when adopting). */
export function updateAppearance(changes, { sync = true } = {}) {
  look = normalize({ ...look, ...changes, updated: Date.now() });
  storage.set("appearance", look);
  applyAppearance();
  bus.emit("appearance");
  if (sync) pushToServer();
}

export function setToken(key, value, theme = currentTheme()) {
  const colors = { dark: { ...look.colors.dark }, light: { ...look.colors.light } };
  if (value && HEX.test(value)) colors[theme][key] = value.toLowerCase();
  else delete colors[theme][key];
  updateAppearance({ colors, preset: "custom" });
}

export function resetToken(key, theme = currentTheme()) {
  const preset = PRESETS.find((p) => p.id === look.preset) || PRESETS[0];
  setToken(key, preset[theme]?.[key] || "", theme);
}

export function usePreset(id) {
  const preset = PRESETS.find((p) => p.id === id);
  if (!preset) return;
  updateAppearance({ preset: id, colors: { dark: { ...preset.dark }, light: { ...preset.light } } });
}

/** Adopt the server's copy when it is newer (another device changed it), else upload ours. */
export function syncFromServer(remote) {
  const theirs = normalize(remote);
  if (theirs.updated > look.updated) {
    look = theirs;
    storage.set("appearance", look);
    applyAppearance();
    bus.emit("appearance");
  } else if (look.updated > theirs.updated) {
    pushToServer();
  }
}

export function exportAppearance() {
  const { preset, colors, scale, reading, brackets, grid, caps } = look;
  return JSON.stringify({ bagley_theme: 1, preset, colors, scale, reading, brackets, grid, caps }, null, 2);
}

export function importAppearance(text) {
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    throw new Error("That isn't valid JSON.");
  }
  if (!data || typeof data !== "object" || !data.colors) throw new Error("No colours found in that theme.");
  updateAppearance({ ...normalize(data), preset: data.preset && PRESETS.some((p) => p.id === data.preset) ? data.preset : "custom" });
}

// WCAG contrast, for the warnings in the editor.
function luminance(hex) {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255).map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4));
  return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2];
}

export function contrast(a, b) {
  const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p);
  return (x + 0.05) / (y + 0.05);
}
