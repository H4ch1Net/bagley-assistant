// Your life: what you did by day, from your git commits, Obsidian notes and chats, and the
// sources it reads. Everything is read on this machine.

import { api } from "../api.js";
import { addKind } from "./automations.js";
import { state } from "../state.js";
import { toast } from "../ui.js";
import { el, icon } from "../util.js";
import { header, savePrefs, section, stringList, value } from "../settings-kit.js";

addKind({
  id: "recap", icon: "history", label: "Recap", help: "Bagley writes a recap of the past 7 days from your commits, notes and chats.",
  when: "fridays at 17:00", name: "e.g. Weekly recap", defaultName: "Weekly recap", prompt: "Extra instructions (optional)", placeholder: "e.g. Group it by project and end with what to pick up on Monday.",
}); // prettier-ignore

const PERIODS = ["today", "yesterday", "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday", "this week", "last week", "past 7 days"];

export function render(panel, ctx) {
  ctx.lifePeriod ||= "today";
  const view = el("div");
  const custom = el("input", { class: "input", placeholder: "Or type: 6 oct, last month, 2026-10-06", "aria-label": "Period" });
  const chips = el("div", { class: "segmented", role: "group", "aria-label": "Period", style: "flex-wrap:wrap;margin-bottom:8px" },
    ...PERIODS.map((p) => el("button", { type: "button", "aria-pressed": String(ctx.lifePeriod === p), onclick: () => { ctx.lifePeriod = p; ctx.refresh(); } }, p)));
  custom.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && custom.value.trim()) {
      ctx.lifePeriod = custom.value.trim();
      ctx.refresh();
    }
  });

  header(panel, "Your life", ["What you worked on, read from your git commits, Obsidian notes and chats. Ask in chat too: ", el("em", { text: "“what was I working on Tuesday?”" }), " or ", el("em", { text: "“write my weekly recap”" }), "."]);
  panel.append(section("Activity", chips, custom, view), sources(ctx));
  load(view, ctx.lifePeriod);
}

async function load(view, period) {
  view.replaceChildren(el("div", { class: "status-line" }, el("span", { class: "spinner" }), "Reading your activity"));
  let data;
  try {
    data = await api.get(`/api/life/activity?period=${encodeURIComponent(period)}`);
  } catch (err) {
    view.replaceChildren(el("div", { class: "status-line error-text", text: err.message }));
    return;
  }
  const t = data.totals;
  const readout = (k, v) => el("div", { class: "stat" }, el("span", { class: "k", text: k }), el("span", { class: "v", text: String(v) }));
  const term = el("pre", { class: "term", style: "max-height:none" });
  const line = (parts) => term.append(el("span", { class: "term-row" }, ...parts.map(([cls, text]) => el("span", { class: cls, text })), "\n"));
  line([["g", "» ACTIVITY // "], ["w", data.period.label.toUpperCase()], ["g", ` // ${data.period.days} DAY${data.period.days === 1 ? "" : "S"}`]]);
  for (const repo of data.repos) {
    line([["g", "[GIT]   "], ["w", repo.name.padEnd(22)], ["ok", `+${repo.added}`], ["g", " "], ["bad", `-${repo.removed}`], ["g", `  ${repo.commits} commit${repo.commits === 1 ? "" : "s"}${repo.branches.length ? ` · ${repo.branches.join(", ")}` : ""}`]]);
    for (const c of repo.log.slice(0, 6)) line([["g", `        ${c.time.slice(5).replace("T", " ")}  `], ["", c.subject]]);
    if (repo.more) line([["g", `        … ${repo.more} more`]]);
  }
  for (const d of data.daily_notes) {
    line([["g", "[DAILY] "], ["w", d.date], ["g", `  ${d.vault}/${d.path}`]]);
    if (d.excerpt) line([["g", "        "], ["", d.excerpt.slice(0, 160)]]);
    for (const task of (d.open_tasks || []).slice(0, 4)) line([["g", "        [ ] "], ["", task]]);
  }
  for (const n of data.notes.slice(0, 20)) line([["g", `[NOTE]  ${n.time.slice(5).replace("T", " ")}  `], [n.status === "created" ? "ok" : "", n.status.toUpperCase().padEnd(9)], ["w", `${n.vault}/${n.path}`], ["g", n.tags?.length ? `  #${n.tags.join(" #")}` : ""]]);
  for (const c of data.chats.slice(0, 20)) line([["g", `[CHAT]  ${c.time.slice(5).replace("T", " ")}  `], ["w", c.title], ["g", c.mode && c.mode !== "default" ? `  // ${c.mode.toUpperCase()}` : ""]]);
  for (const a of data.automations) line([["g", `[AUTO]  ${a.time.slice(5).replace("T", " ")}  `], ["", a.name], ["g", `  ${(a.status || "").toUpperCase()}`]]);
  for (const w of data.warnings || []) line([["warn", "[WARN]  "], ["", w]]);
  if (!data.repos.length && !data.notes.length && !data.chats.length && !data.daily_notes.length) line([["g", "NO ACTIVITY RECORDED IN THIS PERIOD."]]);

  const write = el("button", { class: "btn btn-sm", type: "button", onclick: () => {
    document.querySelector("#settings-dialog").close();
    const input = document.querySelector("#composer-input");
    input.value = `Write a recap of what I did ${data.period.label}, using what_was_i_doing.`;
    input.dispatchEvent(new Event("input"));
    input.focus();
  } }, icon("pencil", "icon-sm"), "Ask Bagley to write it up");
  view.replaceChildren(
    el("div", { class: "field-row-3", style: "margin:4px 0 10px" }, readout("Commits", `${t.commits} in ${t.repos} repos`), readout("Notes", `${t.notes} · ${t.daily_notes} daily`), readout("Chats", t.chats)),
    term,
    el("div", { class: "inline", style: "margin-top:10px" }, write),
  );
}

function sources(ctx) {
  const box = el("div", { class: "section" });
  const readout = el("div", { class: "help", style: "margin-top:8px" });
  const recap = el("button", { class: "btn btn-sm", type: "button" }, icon("calendar-clock", "icon-sm"), "Schedule a weekly recap");
  const hasRecap = state.automations.some((a) => a.kind === "recap");
  recap.disabled = hasRecap;
  if (hasRecap) recap.replaceChildren(icon("check", "icon-sm"), "Weekly recap scheduled");
  recap.addEventListener("click", async () => {
    try {
      const res = await api.post("/api/life/recap-automation");
      toast(res.created ? "Weekly recap every Friday at 17:00" : "You already have a weekly recap");
      await ctx.onAutomationsChanged();
      ctx.refresh();
    } catch (err) {
      toast(err.message, { type: "error" });
    }
  });
  const save = (key) => async (items) => {
    const ok = await savePrefs({ [key]: items });
    if (ok) {
      if (key === "vaults") api.post("/api/knowledge/reindex").catch(() => {});
      showSources();
    }
    return ok;
  };
  const showSources = () => api.get("/api/life/sources").then((s) => {
    const vaults = s.vaults.map((v) => `${v.path}: ${v.exists ? `${v.notes} notes` : "MISSING"}`);
    readout.textContent = `${s.repos.length} git repositories found${s.repos.length ? ` (${s.repos.slice(0, 6).map((r) => r.name).join(", ")}${s.repos.length > 6 ? "…" : ""})` : ""}. ${vaults.join(" · ")}`;
  }).catch(() => {});
  box.append(
    el("div", { class: "section-title", text: "Sources" }),
    el("div", { class: "field-row" },
      el("div", { class: "field" }, el("span", { class: "field-label", text: "Obsidian vaults" }),
        stringList({ items: [...(value("vaults") || [])], placeholder: "~/Documents/Obsidian/Main", onChange: save("vaults") })),
      el("div", { class: "field" }, el("span", { class: "field-label", text: "Code folders" }),
        stringList({ items: [...(value("code_folders") || [])], placeholder: "~/dev", onChange: save("code_folders") })),
    ),
    readout,
    el("p", { class: "help", text: "Vaults are also indexed for search. Code folders are scanned for git repositories; only commits whose author email matches the repository's git config are counted, and the code itself is not read." }),
    el("div", { class: "inline", style: "margin-top:8px" }, recap),
  );
  showSources();
  return box;
}
