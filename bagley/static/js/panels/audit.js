// Audit log: every tool call, who allowed it and how it went, as a ctOS terminal log.

import { api } from "../api.js";
import { el, icon } from "../util.js";
import { header, section } from "../settings-kit.js";

const pad = (n) => String(n).padStart(2, "0");

export function stamp(seconds) {
  const d = new Date(seconds * 1000);
  return `${pad(d.getDate())}${pad(d.getMonth() + 1)}${pad(d.getFullYear() % 100)}-${pad(d.getHours())}${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

function duration(ms) {
  if (ms === null || ms === undefined) return "";
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

const DECISION = { auto: "AUTO", approved: "APPROVED", denied: "DENIED", blocked: "BLOCKED", routine: "ROUTINE", unknown: "UNKNOWN" };

function line(entry) {
  const result = entry.ok === true ? el("span", { class: "ok", text: "OK  " }) : entry.ok === false ? el("span", { class: "bad", text: "FAIL" }) : el("span", { class: "g", text: "--  " });
  const decision = DECISION[entry.decision] || entry.decision.toUpperCase();
  const decisionCls = ["denied", "blocked"].includes(entry.decision) ? "bad" : entry.decision === "approved" ? "warn" : "g";
  const args = JSON.stringify(entry.arguments || {});
  return el("span", { class: "term-row", title: entry.detail || "" },
    el("span", { class: "g", text: `${stamp(entry.created_at)}  ` }),
    el("span", { class: "g", text: "EXEC  " }),
    el("span", { class: "w", text: `${entry.tool.padEnd(18)} ` }),
    el("span", { class: "g", text: `${entry.permission.toUpperCase()}>` }),
    el("span", { class: decisionCls, text: `${decision.padEnd(9)} ` }),
    result,
    el("span", { class: "g", text: `  ${duration(entry.duration_ms).padStart(6)}  ` }),
    entry.sandboxed ? el("span", { class: "ok", text: "BWRAP  " }) : null,
    entry.source && entry.source !== "web" ? el("span", { class: "g", text: `${entry.source.toUpperCase()}  ` }) : null,
    el("span", { text: args.length > 140 ? `${args.slice(0, 139)}…` : args }),
    "\n",
  );
}

export function render(panel) {
  const log = el("pre", { class: "term", "aria-live": "off" });
  const filter = el("input", { class: "input mono", placeholder: "Filter by tool, e.g. run_command", "aria-label": "Filter by tool", spellcheck: "false" });
  const status = el("div", { class: "help", style: "margin-top:6px" });
  let entries = [];
  let timer = 0;
  let stopped = false;

  const draw = () => {
    const shown = entries.filter((e) => !filter.value.trim() || e.tool.includes(filter.value.trim()));
    log.replaceChildren(...(shown.length ? shown.slice().reverse().map(line) : [el("span", { class: "g", text: "NO ENTRIES // tool calls appear here as they happen." })]));
    log.scrollTop = log.scrollHeight;
    const denied = entries.filter((e) => e.decision === "denied" || e.decision === "blocked").length;
    status.textContent = `${entries.length} entries · ${denied} denied or blocked · ${entries.filter((e) => e.sandboxed).length} sandboxed`;
  };
  const poll = async () => {
    if (stopped) return;
    const after = entries[0]?.id || 0;
    try {
      const fresh = await api.get(`/api/audit?limit=300&after=${after}`);
      if (fresh.length) {
        entries = [...fresh, ...entries].slice(0, 600);
        draw();
      }
    } catch {
      /* Try again on the next tick. */
    }
    timer = setTimeout(poll, 4000);
  };
  filter.addEventListener("input", draw);
  const exportBtn = el("button", { class: "btn btn-sm", type: "button", onclick: () => {
    const blob = new Blob([entries.slice().reverse().map((e) => JSON.stringify(e)).join("\n") + "\n"], { type: "application/x-ndjson" });
    const a = el("a", { href: URL.createObjectURL(blob), download: `bagley-audit-${stamp(Date.now() / 1000)}.jsonl` });
    a.click();
    setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  } }, icon("download", "icon-sm"), "Export");

  header(panel, "Audit log", "Every tool call Bagley made, from this window, the desktop overlay, the shell, the phone, automations and routines: whether it ran on its own, was approved or denied, how long it took and whether it ran in the sandbox. Also in the terminal: bagley audit --follow.");
  panel.append(section("", el("div", { class: "inline" }, filter, exportBtn), status), log);
  draw();
  poll();
  return () => {
    stopped = true;
    clearTimeout(timer);
  };
}
