// Building blocks shared by the settings panels: saving preferences, labelled fields, toggles,
// selects, focus-keeping redraws and the model download used by onboarding too.

import { api } from "./api.js";
import { bus, state } from "./state.js";
import { toast } from "./ui.js";
import { $, el, formatBytes, icon } from "./util.js";

export const ENV_NAMES = {
  provider: "BAGLEY_PROVIDER", base_url: "BAGLEY_BASE_URL", api_key: "BAGLEY_API_KEY", model: "BAGLEY_MODEL",
  temperature: "BAGLEY_TEMPERATURE", context_tokens: "BAGLEY_CONTEXT_TOKENS", max_steps: "BAGLEY_MAX_STEPS",
  tool_mode: "BAGLEY_TOOL_MODE", think: "BAGLEY_THINK", persona: "BAGLEY_PERSONA",
}; // prettier-ignore

export const RECOMMENDED_MODELS = [
  { name: "qwen3:4b", note: "Fast, good at tools · 2.5 GB" },
  { name: "llama3.2:3b", note: "Small and quick · 2.0 GB" },
  { name: "qwen3:8b", note: "Stronger reasoning · 5.2 GB" },
  { name: "gpt-oss:20b", note: "Best quality, needs 16 GB RAM · 14 GB" },
];

export const locked = (key) => state.prefs?.locked.includes(key);
export const lockedBy = (key) => state.prefs?.locked_by?.[key] || ENV_NAMES[key];
export const value = (key) => state.prefs?.values[key];

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

export function flashSaved() {
  const node = $("#save-state");
  if (!node) return;
  node.replaceChildren(icon("check", "icon-xs"), "Saved");
  clearTimeout(flashSaved.timer);
  flashSaved.timer = setTimeout(() => node.replaceChildren(), 1600);
}

let fieldSeq = 0;

export function field(label, control, { help, key, id } = {}) {
  const lockNote = key && locked(key) ? el("span", { class: "lock", title: `Locked by the ${lockedBy(key)} environment variable` }, icon("lock", "icon-xs"), lockedBy(key)) : null;
  if (key && locked(key)) control.disabled = true;
  // Label the form control itself, even when it sits in a wrapper next to a button.
  const target = control.matches("input, select, textarea") ? control : control.querySelector("input, select, textarea");
  if (target) target.id = id || target.id || `field-${++fieldSeq}`;
  return el("div", { class: "field" },
    el("label", { for: target?.id || null }, label, lockNote),
    control,
    help ? el("div", { class: "help" }, help) : null,
  );
}

/** Redraw `box` and give focus back to the same control if it had it. */
export function keepFocus(box, redraw) {
  const active = document.activeElement;
  const name = (n) => n.getAttribute("aria-label") || n.textContent.trim();
  const label = box.contains(active) && active !== box ? name(active) : null;
  redraw();
  if (label) [...box.querySelectorAll("button, input, select")].find((n) => name(n) === label && !n.disabled)?.focus();
}

export function toggleRow(title, help, checked, onChange, { key, label } = {}) {
  const input = el("input", { type: "checkbox", role: "switch", "aria-label": label || title });
  input.checked = Boolean(checked);
  if (key && locked(key)) input.disabled = true;
  input.addEventListener("change", () => onChange(input.checked));
  return el("div", { class: "toggle-row" },
    el("div", { class: "text" }, el("strong", { text: title }), help ? el("span", { class: "help", text: help }) : null),
    el("label", { class: "switch" }, input, el("span")),
  );
}

export function select(options, current, onChange, attrs = {}) {
  const node = el("select", { class: "select", ...attrs }, ...options.map(([v, label]) => el("option", { value: v, selected: String(v) === String(current) }, label)));
  node.addEventListener("change", () => onChange(node.value));
  return node;
}

export function section(title, ...children) {
  return el("div", { class: "section" }, title ? el("div", { class: "section-title", text: title }) : null, ...children);
}

export function header(panel, title, lead) {
  panel.append(el("h3", { text: title }), lead ? el("p", { class: "lead" }, ...(Array.isArray(lead) ? lead : [lead])) : null);
}

export function notice(text, iconName = "info") {
  return el("div", { class: "notice", style: "margin-bottom:8px" }, icon(iconName, "icon-sm"), el("span", { text }));
}

/** A list of strings (folders, languages) edited as a text input plus removable rows. */
export function stringList({ items, placeholder, addLabel = "Add", onChange, mono = true, validate }) {
  const box = el("div");
  const draw = () => {
    const input = el("input", { class: `input${mono ? " mono" : ""}`, placeholder, spellcheck: "false", "aria-label": placeholder });
    const add = el("button", { class: "btn", type: "button" }, icon("plus", "icon-sm"), addLabel);
    const submit = async () => {
      const text = input.value.trim();
      if (!text) return input.focus();
      const problem = validate?.(text);
      if (problem) return toast(problem, { type: "error" });
      if (items.includes(text)) return toast("Already in the list.");
      const next = [...items, text];
      if (await onChange(next)) {
        items = next;
        draw();
      }
    };
    add.addEventListener("click", submit);
    input.addEventListener("keydown", (e) => e.key === "Enter" && submit());
    box.replaceChildren(
      items.length
        ? el("div", { class: "list", style: "margin-bottom:10px" }, ...items.map((item) => el("div", { class: "list-item", style: "align-items:center" },
            el("div", { class: "grow" }, el("div", { class: "name", text: item })),
            el("button", { class: "icon-btn icon-btn-sm", type: "button", "aria-label": `Remove ${item}`, title: "Remove", onclick: async () => {
              const next = items.filter((x) => x !== item);
              if (await onChange(next)) {
                items = next;
                draw();
              }
            } }, icon("x", "icon-sm")))))
        : null,
      el("div", { class: "inline" }, input, add),
    );
  };
  draw();
  return box;
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
