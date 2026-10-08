// Settings dialog: a rofi-style window with grouped tabs on the left and one panel module per
// tab. Server preferences save immediately; appearance and browser voice live in the browser.

import { $, el, icon } from "./util.js";
import { keepToasts } from "./ui.js";
import { voice } from "./voice.js";
import * as appearance from "./panels/appearance.js";
import * as audit from "./panels/audit.js";
import "./panels/bench.js";
import * as automations from "./panels/automations.js";
import * as devices from "./panels/devices.js";
import * as general from "./panels/general.js";
import * as knowledge from "./panels/knowledge.js";
import * as life from "./panels/life.js";
import * as memory from "./panels/memory.js";
import * as model from "./panels/model.js";
import * as notifications from "./panels/notifications.js";
import * as routines from "./panels/routines.js";
import * as study from "./panels/study.js";
import * as tools from "./panels/tools.js";
import * as voicePanel from "./panels/voice.js";

export { pullModel, RECOMMENDED_MODELS, savePrefs } from "./settings-kit.js";

const TABS = [
  { group: "Assistant" },
  { id: "general", label: "General", icon: "sparkles", panel: general },
  { id: "model", label: "Model & machines", icon: "cpu", panel: model },
  { id: "memory", label: "Memory", icon: "bookmark", panel: memory },
  { id: "knowledge", label: "Knowledge", icon: "library", panel: knowledge },
  { id: "life", label: "Your life", icon: "history", panel: life },
  { id: "study", label: "Study", icon: "graduation-cap", panel: study },
  { id: "voice", label: "Voice", icon: "volume-2", panel: voicePanel },
  { group: "Automation" },
  { id: "automations", label: "Automations", icon: "calendar-clock", panel: automations },
  { id: "routines", label: "Routines", icon: "workflow", panel: routines },
  { group: "Security" },
  { id: "tools", label: "Tools & permissions", icon: "shield-check", panel: tools },
  { id: "audit", label: "Audit log", icon: "list", panel: audit },
  { group: "Devices" },
  { id: "notifications", label: "Notifications", icon: "bell", panel: notifications },
  { id: "devices", label: "Desktop & phone", icon: "monitor", panel: devices },
  { group: "Look" },
  { id: "appearance", label: "Appearance", icon: "sun", panel: appearance },
];

/** Add a tab from a feature module, before the "Look" group. */
export function addTab(tab, { after } = {}) {
  if (TABS.some((t) => t.id === tab.id)) return;
  const index = after ? TABS.findIndex((t) => t.id === after) + 1 : TABS.findIndex((t) => t.group === "Look");
  TABS.splice(index > 0 ? index : TABS.length, 0, tab);
}

const tabs = () => TABS.filter((t) => t.id);

export class Settings {
  constructor({ onModelsChanged, onMemoriesChanged, onAutomationsChanged, openChat }) {
    this.dialog = $("#settings-dialog");
    this.onModelsChanged = onModelsChanged;
    this.onMemoriesChanged = onMemoriesChanged;
    this.onAutomationsChanged = onAutomationsChanged;
    this.openChat = openChat;
    this.tab = "general";
    this.cleanup = null;
    this.dialog.addEventListener("close", () => {
      this.isOpen = false;
      this.cleanup?.();
      this.cleanup = null;
    });
    voice.onVoicesChanged(() => this.fillVoices?.());
  }

  open(tab = this.tab) {
    this.tab = tabs().some((t) => t.id === tab) ? tab : "general";
    if (!this.dialog.open) this.dialog.showModal();
    this.isOpen = true;
    this.renderFrame();
  }

  renderFrame() {
    this.cleanup?.();
    this.cleanup = null;
    const list = el("div", { class: "tabs", role: "tablist", "aria-orientation": "vertical" },
      ...TABS.map((t) => t.group
        ? el("div", { class: "tab-group", "aria-hidden": "true", text: t.group })
        : el("button", {
            class: "tab", role: "tab", type: "button", id: `tab-${t.id}`,
            "aria-selected": String(t.id === this.tab), "aria-controls": "settings-panel",
            tabindex: t.id === this.tab ? "0" : "-1",
            onclick: () => { this.tab = t.id; this.renderFrame(); $(`#tab-${t.id}`)?.focus(); },
          }, icon(t.icon, "icon-sm"), t.label)),
    );
    list.addEventListener("keydown", (e) => {
      if (!["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight"].includes(e.key)) return;
      e.preventDefault();
      const all = tabs();
      const i = all.findIndex((t) => t.id === this.tab);
      const next = all[(i + (e.key === "ArrowDown" || e.key === "ArrowRight" ? 1 : all.length - 1)) % all.length];
      this.tab = next.id;
      this.renderFrame();
      $(`#tab-${next.id}`)?.focus();
    });
    const panel = el("section", { class: "panel", id: "settings-panel", role: "tabpanel", "aria-labelledby": `tab-${this.tab}` });
    this.dialog.replaceChildren(
      el("div", { class: "dialog-head" },
        el("h2", { id: "settings-title" }, el("span", { class: "barcode", "aria-hidden": "true" }), "Settings"),
        el("div", { class: "inline" },
          el("span", { class: "save-state", id: "save-state", "aria-live": "polite" }),
          el("button", { class: "icon-btn", type: "button", "aria-label": "Close settings", onclick: () => this.dialog.close() }, icon("x")),
        ),
      ),
      el("div", { class: "settings" }, list, panel),
    );
    keepToasts();
    const current = tabs().find((t) => t.id === this.tab);
    const result = current.panel.render(panel, this);
    if (typeof result === "function") this.cleanup = result;
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

  /** Redraw only the automation list (background updates never wipe a half-filled form). */
  renderAutomationList() {
    automations.renderList(this);
  }

  renderKnowledge() {
    knowledge.renderStatus(this);
  }
}
