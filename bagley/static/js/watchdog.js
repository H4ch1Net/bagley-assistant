// The watchdog report as a ctOS readout: headline, counts, one row per section with its
// findings. Used by the System watchdog settings panel and by briefing messages in the chat.

import { clockTime, el } from "./util.js";

export const STATUS_LABEL = { ok: "OK", info: "INFO", warn: "WARN", crit: "CRIT", unavailable: "N/A" };

const pad = (n) => String(n).padStart(2, "0");

/** "OK 05 // WARN 01 // CRIT 00 // N/A 01" from the report's counts. */
export function countsLine(counts = {}) {
  return ["ok", "warn", "crit", "unavailable"].map((k) => `${STATUS_LABEL[k]} ${pad(counts[k] || 0)}`).join(" // ");
}

function finding(f) {
  return el("li", { class: `wd-finding ${f.severity}` },
    el("span", { class: "wd-sev", text: `[${STATUS_LABEL[f.severity] || f.severity.toUpperCase()}]` }),
    el("span", { class: "wd-text", text: f.text }),
    f.detail ? el("span", { class: "wd-detail", text: f.detail }) : null,
  );
}

function sectionRow(s, { onRun } = {}) {
  const head = [
    el("span", { class: `wd-status ${s.status}`, text: STATUS_LABEL[s.status] || s.status.toUpperCase() }),
    el("span", { class: "wd-title", text: s.title }),
    el("span", { class: "wd-summary", text: s.summary || "" }),
  ];
  const run = onRun
    ? el("button", { class: "btn btn-sm btn-ghost wd-run", type: "button", title: `Check ${s.title} again`, "aria-label": `Check ${s.title} again`, onclick: (e) => { e.preventDefault(); onRun(s.id, e.currentTarget); } }, "Run")
    : null;
  if (!s.findings?.length) return el("div", { class: "wd-row", dataset: { section: s.id } }, el("div", { class: "wd-head" }, ...head, run));
  return el("details", { class: "wd-row", dataset: { section: s.id }, open: s.status === "crit" || s.status === "warn" },
    el("summary", { class: "wd-head" }, ...head, run),
    el("ul", { class: "wd-findings" }, ...s.findings.map(finding)),
  );
}

/** The whole report. `onRun(sectionId, button)` adds a per-section re-run button. */
export function reportView(report, { onRun, compact = false } = {}) {
  const when = report.created_at ? `${clockTime(report.created_at)} // ${new Date(report.created_at * 1000).toLocaleDateString()}` : "";
  return el("section", { class: `wd cf cf-muted${compact ? " compact" : ""} ${report.status}`, "aria-label": report.title_line || "System report" },
    el("div", { class: "wd-top" },
      el("div", { class: "wd-line" },
        el("span", { class: "wd-mark", text: "»" }),
        el("span", { class: "wd-name", text: report.title_line || report.title || "SYSTEM REPORT" }),
        el("span", { class: "wd-when", text: when })),
      el("div", { class: `wd-headline ${report.status}`, text: report.headline || "" }),
      el("div", { class: "wd-counts", text: countsLine(report.counts) }),
    ),
    el("div", { class: "wd-rows" }, ...(report.sections || []).map((s) => sectionRow(s, { onRun }))),
  );
}
