// System watchdog: the last system and security report, checks on demand, the morning briefing
// and the network baseline (known devices and open ports).

import { api } from "../api.js";
import { addKind } from "./automations.js";
import { state } from "../state.js";
import { confirmDialog, toast } from "../ui.js";
import { el, icon } from "../util.js";
import { header, section } from "../settings-kit.js";
import { reportView } from "../watchdog.js";

addKind({
  id: "briefing", icon: "radar", label: "Briefing",
  help: "A system and security report: failed services, journal errors, disks, battery, updates, new devices and ports, SSH logins, known vulnerabilities. Sent to the desktop and your phone.",
  when: "daily at 07:30", name: "e.g. Morning briefing", defaultName: "Morning briefing",
  prompt: "Instructions (optional)", placeholder: "Optional: Bagley writes a short briefing from the report, e.g. lead with anything I need to fix today.",
  target: { label: "Sections (empty for all)", placeholder: "services, journal, disks, battery, updates, network, ports, ssh, vulns" },
  note: "No model needed unless you add instructions.",
}); // prettier-ignore

const RANK = { unavailable: 0, ok: 1, info: 2, warn: 3, crit: 4 };

/** Fold a fresh single-section result into the shown report and recount. */
function merge(report, fresh) {
  const sections = report.sections.map((s) => fresh.sections.find((f) => f.id === s.id) || s);
  const counts = { ok: 0, info: 0, warn: 0, crit: 0, unavailable: 0 };
  for (const s of sections) counts[s.status] = (counts[s.status] || 0) + 1;
  const status = sections.reduce((worst, s) => (RANK[s.status] > RANK[worst] ? s.status : worst), "ok");
  return { ...report, sections, counts, status: status === "info" ? "ok" : status };
}

export function render(panel, ctx) {
  const view = el("div", { class: "wd-view" });
  const line = el("div", { class: "status-line", "aria-live": "polite" });
  const runAll = el("button", { class: "btn btn-primary", type: "button" }, icon("scan", "icon-sm"), "Run check");
  const hasBriefing = state.automations.some((a) => a.kind === "briefing");
  const briefing = el("button", { class: "btn", type: "button", disabled: hasBriefing },
    icon(hasBriefing ? "check" : "calendar-clock", "icon-sm"), hasBriefing ? "Briefing scheduled" : "Enable morning briefing");
  const reset = el("button", { class: "btn btn-ghost", type: "button", title: "Accept every device and port seen so far as known" }, icon("refresh-cw", "icon-sm"), "Reset baseline");

  const draw = () => {
    const report = ctx.watchdogReport;
    if (!report) return;
    view.replaceChildren(reportView(report, { onRun: runSection }));
  };

  const busy = (on, text = "") => {
    runAll.disabled = on;
    line.replaceChildren(...(on ? [el("span", { class: "spinner" })] : []), text);
  };

  async function runSection(id, button) {
    button.disabled = true;
    button.textContent = "…";
    try {
      const fresh = await api.get(`/api/watchdog/report?sections=${encodeURIComponent(id)}`);
      ctx.watchdogReport = ctx.watchdogReport ? merge(ctx.watchdogReport, fresh) : fresh;
      draw();
    } catch (err) {
      toast(err.message, { type: "error" });
      button.disabled = false;
      button.textContent = "Run";
    }
  }

  runAll.addEventListener("click", async () => {
    busy(true, "Checking services, journal, disks, updates, network and packages. Updates can take a minute.");
    try {
      ctx.watchdogReport = await api.get("/api/watchdog/report");
      busy(false);
      draw();
    } catch (err) {
      busy(false, err.message);
    }
  });

  briefing.addEventListener("click", async () => {
    briefing.disabled = true;
    try {
      const res = await api.post("/api/watchdog/briefing");
      toast(res.created ? `${res.name}: ${res.schedule_text}` : "You already have a briefing");
      await ctx.onAutomationsChanged();
      ctx.refresh();
    } catch (err) {
      briefing.disabled = false;
      toast(err.message, { type: "error" });
    }
  });

  reset.addEventListener("click", async () => {
    const ok = await confirmDialog({
      title: "Reset the baseline?",
      message: "Bagley forgets the devices, open ports and tailnet peers it knows. The next check records what it sees as the new normal, so nothing already there is reported as new.",
      confirm: "Reset",
    });
    if (!ok) return;
    try {
      const res = await api.post("/api/watchdog/baseline/reset", {});
      toast(`Baseline cleared: ${res.removed} entr${res.removed === 1 ? "y" : "ies"}`);
    } catch (err) {
      toast(err.message, { type: "error" });
    }
  });

  header(panel, "System watchdog", [
    "Bagley checks this computer the way a sysadmin would: failed systemd services, journal errors, disk and battery health, pending updates, new devices on your network, newly opened ports, failed SSH logins and known vulnerabilities in installed packages. Ask in chat too: ",
    el("em", { text: "“anything wrong with my system?”" }), ". In the terminal: bagley briefing.",
  ]);
  panel.append(
    section("Report", el("div", { class: "inline", style: "flex-wrap:wrap;margin-bottom:10px" }, runAll, briefing, reset), line, view),
  );

  if (ctx.watchdogReport) {
    draw();
    return;
  }
  view.append(el("div", { class: "status-line" }, el("span", { class: "spinner" }), "Loading the last report"));
  api.get("/api/watchdog/last").then((report) => {
    ctx.watchdogReport = report;
    draw();
  }).catch(async () => {
    let sections = [];
    try {
      sections = await api.get("/api/watchdog/sections");
    } catch {
      view.replaceChildren(el("div", { class: "list-empty", text: "The watchdog is not available on this server. It runs on Linux." }));
      runAll.disabled = briefing.disabled = reset.disabled = true;
      return;
    }
    view.replaceChildren(
      el("p", { class: "help", text: "No report yet. Run a check to see where things stand." }),
      el("div", { class: "list" }, ...sections.map((s) => el("div", { class: "list-item" },
        el("span", { class: "tool-icon" }, icon(s.baseline ? "radar" : "shield-check", "icon-sm")),
        el("div", { class: "grow" }, el("div", { class: "name", text: s.title }), el("div", { class: "desc", text: s.description })),
      ))),
    );
  });
}
