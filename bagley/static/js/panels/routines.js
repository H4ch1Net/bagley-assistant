// Routines: saved sequences of approved actions. Run, schedule, rename or delete them.

import { api } from "../api.js";
import { loadRoutines, routineMeta, runRoutine, stepList } from "../routines.js";
import { state } from "../state.js";
import { confirmDialog, toast } from "../ui.js";
import { el, icon } from "../util.js";
import { header, section } from "../settings-kit.js";

export function render(panel, ctx) {
  const list = el("div", { class: "list" });
  const draw = () => {
    const routines = state.routines || [];
    list.replaceChildren(...(routines.length ? routines.map((r) => row(ctx, r, draw)) : [el("div", { class: "list-empty", text: "No routines yet. In a chat, use the save-as-routine button in the title bar to keep the actions Bagley just did." })]));
  };
  header(panel, "Routines", ["Named sequences of actions you approved once: \"coding mode\" opens kitty, Zed and lazygit on workspace 2. Run them from here, by asking (", el("em", { text: "“run my coding mode”" }), "), from the terminal (bagley routine run NAME) or on a schedule."]);
  panel.append(section("Your routines", list));
  draw();
  loadRoutines().then(draw);
}

function row(ctx, r, draw) {
  const detail = el("div", { hidden: true, style: "margin-top:10px" }, stepList(r.steps));
  const toggle = el("button", { class: "icon-btn icon-btn-sm", type: "button", title: "Show steps", "aria-label": `Show steps of ${r.name}`, "aria-expanded": "false" }, icon("chevron-down", "icon-sm"));
  toggle.addEventListener("click", () => {
    detail.hidden = !detail.hidden;
    toggle.setAttribute("aria-expanded", String(!detail.hidden));
  });
  const run = el("button", { class: "btn btn-sm btn-primary", type: "button", "aria-label": `Run ${r.name}`, onclick: () => runRoutine(r, { onDone: draw }) }, icon("play", "icon-xs"), "Run");
  const schedule = el("button", { class: "icon-btn icon-btn-sm", type: "button", title: "Schedule", "aria-label": `Schedule ${r.name}`, onclick: () => {
    ctx.newKind = "routine";
    ctx.automationDraft = { name: r.name, target: String(r.id) };
    ctx.open("automations");
  } }, icon("calendar-clock", "icon-sm"));
  const rename = el("button", { class: "icon-btn icon-btn-sm", type: "button", title: "Rename", "aria-label": `Rename ${r.name}` }, icon("pencil", "icon-sm"));
  rename.addEventListener("click", async () => {
    const name = prompt("Routine name", r.name);
    if (!name || name.trim() === r.name) return;
    try {
      await api.patch(`/api/routines/${r.id}`, { name: name.trim() });
      await loadRoutines();
      draw();
    } catch (err) {
      toast(err.message, { type: "error" });
    }
  });
  const remove = el("button", { class: "icon-btn icon-btn-sm", type: "button", title: "Delete", "aria-label": `Delete ${r.name}` }, icon("trash-2", "icon-sm"));
  remove.addEventListener("click", async () => {
    if (!(await confirmDialog({ title: `Delete “${r.name}”?`, message: "Schedules that run it are paused.", confirm: "Delete", danger: true }))) return;
    await api.del(`/api/routines/${r.id}`).catch((err) => toast(err.message, { type: "error" }));
    await loadRoutines();
    await ctx.onAutomationsChanged();
    draw();
  });
  const blocked = r.steps.some((s) => s.available === false);
  return el("div", { class: "list-item", dataset: { id: r.id } },
    el("span", { class: "tool-icon" }, r.running ? el("span", { class: "spinner" }) : icon("workflow", "icon-sm")),
    el("div", { class: "grow" },
      el("div", { class: "name" }, r.name, r.builtin ? el("span", { class: "badge", text: "built-in" }) : null, blocked ? el("span", { class: "badge badge-danger", text: "step unavailable" }) : null, r.last_status === "failed" ? el("span", { class: "badge badge-danger", text: "last run failed" }) : null),
      el("div", { class: "desc", text: [r.description, routineMeta(r)].filter(Boolean).join(" · ") }),
      detail,
    ),
    el("div", { class: "inline", style: "gap:2px" }, toggle, schedule, rename, remove, run),
  );
}
