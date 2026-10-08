// The ctOS bar across the top: Bagley's state with a mini avatar, the mode cells, the model,
// the machine answering, CPU and MEM meters, battery and the clock readout.

import { api } from "./api.js";
import { state } from "./state.js";
import { $, el } from "./util.js";

const CODES = {
  idle: "IDLE", listening: "INPUT", thinking: "THINK", reasoning: "REASON", writing: "TX", tool: "EXEC",
  approval: "AWAIT", speaking: "VOICE", happy: "DONE", error: "ERROR", offline: "NO SIGNAL",
}; // prettier-ignore

const pad = (n, size = 2) => String(Math.max(0, Math.round(n))).padStart(size, "0");

/** ddMMyy, -hhmm-, then TZ-HOST- as on the ctOS status segment. */
export function clockParts(date = new Date(), host = "", tz = "") {
  const day = `${pad(date.getDate())}${pad(date.getMonth() + 1)}${pad(date.getFullYear() % 100)}`;
  const time = `-${pad(date.getHours())}${pad(date.getMinutes())}-`;
  const zone = tz || new Intl.DateTimeFormat([], { timeZoneName: "short" }).formatToParts(date).find((p) => p.type === "timeZoneName")?.value || "";
  return { day, time, rest: `${zone.toUpperCase()}${zone ? "-" : ""}${(host || "").toUpperCase()}${host ? "-" : ""}` };
}

export class Bar {
  constructor({ onMode, onNode }) {
    this.onMode = onMode;
    this.onNode = onNode;
    this.stats = null;
    this.statsTimer = 0;
    $("#node-btn").addEventListener("click", () => this.onNode?.());
    this.tickClock();
    setInterval(() => this.tickClock(), 15_000);
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) this.pollStats();
    });
  }

  setState(name, tool = "") {
    const seg = $("#agent-seg");
    seg.dataset.state = name;
    const code = CODES[name] || name.toUpperCase();
    $("#agent-code").textContent = name === "tool" && tool ? `${code} ${tool}` : code;
  }

  /** Mode cells: a framed cell per mode, a crosshair on the active one. */
  renderModes() {
    const box = $("#modes");
    const modes = state.prefs?.modes || [];
    const current = currentMode();
    box.replaceChildren(...modes.map((m) => el("button", {
      class: "mode-cell", type: "button", title: `${m.label}: ${m.description}`,
      "aria-pressed": String(m.id === current), "aria-label": `${m.label} mode`,
      onclick: () => this.onMode?.(m.id),
    }, el("span", { class: "cross", "aria-hidden": "true" }), m.code)));
    const mode = modes.find((m) => m.id === current);
    $("#composer-mode").textContent = mode && mode.id !== "default" ? `// ${mode.code}` : "";
  }

  setNode(name, ok) {
    $("#node-name").textContent = (name || "----").toUpperCase();
    $("#node-dot").className = `node-dot${ok === true ? " ok" : ok === false ? " bad" : ""}`;
  }

  tickClock() {
    const { day, time, rest } = clockParts(new Date(), this.stats?.host || state.info?.machine || "", "");
    $("#clock").replaceChildren(el("span", { class: "g", text: day }), el("span", { class: "w", text: time }), el("span", { class: "g tz", text: rest }));
    const clock = $("#greeter-clock");
    if (clock) {
      const now = new Date();
      clock.textContent = `${pad(now.getHours())}:${pad(now.getMinutes())}`;
      $("#greeter-date").textContent = now.toLocaleDateString([], { weekday: "long", day: "2-digit", month: "long" });
    }
  }

  async pollStats() {
    clearTimeout(this.statsTimer);
    if (document.hidden) return;
    try {
      this.stats = await api.get("/api/system/stats");
      this.renderStats();
    } catch {
      /* Offline: keep the last readout. */
    }
    this.statsTimer = setTimeout(() => this.pollStats(), innerWidth > 1180 ? 2000 : 10000);
  }

  renderStats() {
    const s = this.stats;
    if (!s) return;
    const cpu = $("#cpu-meter");
    cpu.querySelector(".val").textContent = `${pad(s.cpu, 3)}%`;
    cpu.querySelector(".track i").style.width = `${Math.min(100, s.cpu)}%`;
    cpu.classList.toggle("hot", s.cpu >= 90);
    const mem = $("#mem-meter");
    mem.querySelector(".val").textContent = `${(s.mem_used / 1024 ** 3).toFixed(1)}G`;
    mem.querySelector(".track i").style.width = `${Math.min(100, s.mem_percent)}%`;
    mem.classList.toggle("hot", s.mem_percent >= 92);
    const bat = s.battery;
    $("#bat-seg").hidden = $("#bat-div").hidden = !bat;
    if (bat) {
      const charging = bat.plugged && bat.percent < 99;
      $("#bat-label").textContent = bat.plugged ? (charging ? "BAT+" : "BAT=") : "BAT-";
      const value = $("#bat-value");
      value.textContent = pad(bat.percent, 3);
      value.className = `v${charging ? " ok" : !bat.plugged && bat.percent <= 15 ? " bad" : ""}`;
    }
    this.tickClock();
  }
}

/** The mode of the open chat, or the one the next new chat will start in. */
export function currentMode() {
  if (state.activeId) return state.activeMode || "default";
  return state.nextMode || state.prefs?.values.default_mode || "default";
}
