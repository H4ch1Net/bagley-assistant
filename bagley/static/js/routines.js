// Saved routines: named sequences of approved tool calls. Save them from a chat (pick the
// steps, or record from now on), run them with one confirmation and live progress, and
// schedule them like any automation.

import { api } from "./api.js";
import { addKind } from "./panels/automations.js";
import { bus, state } from "./state.js";
import { toolIcon, toolSummary } from "./state.js";
import { keepToasts, toast } from "./ui.js";
import { $, el, icon, relTime } from "./util.js";

export async function loadRoutines() {
  try {
    state.routines = await api.get("/api/routines");
  } catch {
    state.routines = [];
  }
  bus.emit("routines");
  return state.routines;
}

addKind({
  id: "routine", icon: "workflow", label: "Routine", help: "Run one of your saved routines on a schedule. Its saved steps run unattended, exactly as you approved them.",
  when: "weekdays at 08:45", name: "e.g. Coding mode at 9", prompt: null, defaultName: "",
  target: { label: "Routine", options: () => (state.routines || []).map((r) => [String(r.id), `${r.name} · ${r.steps.length} steps`]) },
  note: "Steps that normally ask first run without asking on this schedule.",
}); // prettier-ignore

function dialog() {
  return $("#tool-dialog");
}

function frame(title, body, foot) {
  const d = dialog();
  d.replaceChildren(
    el("div", { class: "dialog-head" }, el("h2", {}, el("span", { class: "barcode", "aria-hidden": "true" }), title),
      el("button", { class: "icon-btn", type: "button", "aria-label": "Close", onclick: () => d.close() }, icon("x"))),
    el("div", { class: "dialog-body" }, ...body),
    foot ? el("div", { class: "dialog-foot" }, ...foot) : null,
  );
  if (!d.open) d.showModal();
  keepToasts();
  return d;
}

/** One line per step: icon, readable summary, the tool and a marker if it can't run now. */
export function stepList(steps, { states } = {}) {
  return el("ol", { class: "list", style: "list-style:none;margin:0;padding:0" }, ...steps.map((s, i) => el("li", { class: "list-item", style: "align-items:center", dataset: { index: i } },
    el("span", { class: "subtle", style: "font-size:.74rem;width:2ch", text: String(i + 1).padStart(2, "0") }),
    el("span", { class: "tool-icon" }, icon(toolIcon(s.tool), "icon-sm")),
    el("div", { class: "grow" },
      el("div", { class: "name" }, s.summary || toolSummary(s.tool, s.arguments), s.risk === "confirm" ? el("span", { class: "badge badge-warn", text: "asks first" }) : null, s.available === false ? el("span", { class: "badge badge-danger", text: "unavailable" }) : null),
      el("div", { class: "desc mono", text: `${s.tool} ${JSON.stringify(s.arguments)}` }),
    ),
    states ? el("span", { class: "tool-status", dataset: { step: i }, text: "" }) : null,
  )));
}

/** Confirm, then run with live progress. */
export function runRoutine(routine, { onDone } = {}) {
  const list = stepList(routine.steps, { states: true });
  const status = el("div", { class: "status-line", "aria-live": "polite" });
  const start = el("button", { class: "btn btn-primary", type: "button" }, icon("play", "icon-sm"), `Run ${routine.steps.length} steps`);
  const d = frame(`Run routine // ${routine.name}`, [
    routine.description ? el("p", { text: routine.description }) : null,
    el("p", { class: "help", style: "margin-bottom:12px", text: "Running it approves these steps once, exactly as saved." }),
    list, status,
  ], [el("button", { class: "btn", type: "button", text: "Cancel", onclick: () => d.close() }), start]);
  start.focus();
  start.addEventListener("click", async () => {
    start.disabled = true;
    status.replaceChildren(el("span", { class: "spinner" }), "Running");
    const mark = (index, text, cls) => {
      const cell = list.querySelector(`[data-step="${index}"]`);
      if (cell) {
        cell.textContent = text;
        cell.style.color = cls ? `var(--${cls})` : "";
      }
    };
    mark(0, "exec");
    try {
      await api.stream(`/api/routines/${routine.id}/run`, { source: "web" }, (ev) => {
        if (ev.type === "routine.step") {
          mark(ev.index, ev.ok ? "ok" : "fail", ev.ok ? "ok" : "danger");
          if (ev.ok && ev.index + 1 < ev.total) mark(ev.index + 1, "exec");
        } else if (ev.type === "routine.end") {
          status.replaceChildren(el("span", { class: `dot ${ev.ok ? "ok" : "bad"}` }), ev.headline || (ev.ok ? "Complete" : `Halted at step ${ev.failed_at + 1}`));
          start.replaceChildren(icon("check", "icon-sm"), "Done");
          if (ev.conversation_id) {
            d.querySelector(".dialog-foot").prepend(el("button", { class: "btn btn-ghost", type: "button", onclick: () => { d.close(); location.hash = `#/c/${ev.conversation_id}`; } }, icon("message-square", "icon-sm"), "Open log"));
          }
        } else if (ev.type === "error") {
          status.replaceChildren(el("span", { class: "dot bad" }), ev.message);
        }
      });
    } catch (err) {
      status.replaceChildren(el("span", { class: "dot bad" }), err.message);
    }
    await loadRoutines();
    onDone?.();
  });
}

/** Save actions from a chat as a routine: tick steps, or record from now on. */
export async function saveFromChat(conversationId) {
  let candidates = [];
  let rec = { recording: false };
  try {
    [candidates, rec] = await Promise.all([
      api.get(`/api/routines/candidates?conversation_id=${encodeURIComponent(conversationId)}`),
      api.get(`/api/routines/record?conversation_id=${encodeURIComponent(conversationId)}`),
    ]);
  } catch (err) {
    toast(err.message, { type: "error" });
    return;
  }
  const name = el("input", { class: "input", maxlength: 60, placeholder: "e.g. Coding mode", "aria-label": "Routine name" });
  const description = el("input", { class: "input", maxlength: 500, placeholder: "What it does (optional)", "aria-label": "Routine description" });
  const boxes = candidates.map((c) => el("input", { type: "checkbox", checked: c.suggested, "aria-label": c.summary || c.tool }));
  const rows = candidates.map((c, i) => el("label", { class: "list-item", style: "align-items:center;cursor:pointer" },
    boxes[i],
    el("span", { class: "tool-icon" }, icon(toolIcon(c.tool), "icon-sm")),
    el("div", { class: "grow" },
      el("div", { class: "name" }, c.summary || c.tool, c.risk === "confirm" ? el("span", { class: "badge badge-warn", text: "asks first" }) : null),
      el("div", { class: "desc mono", text: `${c.tool} ${JSON.stringify(c.arguments)}` }),
    )));
  const save = el("button", { class: "btn btn-primary", type: "button" }, icon("check", "icon-sm"), "Save routine");
  const recBtn = el("button", { class: "btn", type: "button" }, icon(rec.recording ? "square" : "play", "icon-sm"), rec.recording ? `Stop recording (${rec.steps?.length || 0})` : "Record from now");
  const d = frame("Save as routine", [
    el("p", { style: "margin-bottom:12px", text: "A routine repeats tool calls exactly, with one confirmation. Tick the actions from this chat to keep, or record: everything Bagley does from now on in this chat becomes a step." }),
    el("div", { class: "field-row" }, el("div", { class: "field" }, el("label", { text: "Name" }), name), el("div", { class: "field" }, el("label", { text: "Description" }), description)),
    candidates.length ? el("div", { class: "list" }, ...rows) : el("div", { class: "list-empty", text: "No successful actions in this chat yet. Record from now, then ask Bagley to do the steps." }),
  ], [recBtn, el("span", { style: "flex:1" }), el("button", { class: "btn", type: "button", text: "Cancel", onclick: () => d.close() }), save]);
  name.focus();
  save.addEventListener("click", async () => {
    if (!name.value.trim()) return name.focus();
    const steps = candidates.filter((_, i) => boxes[i].checked).map((c) => ({ tool: c.tool, arguments: c.arguments }));
    if (!steps.length) return toast("Tick at least one action.", { type: "error" });
    try {
      await api.post("/api/routines", { name: name.value.trim(), description: description.value.trim(), steps });
      toast(`Saved routine “${name.value.trim()}”`);
      d.close();
      loadRoutines();
    } catch (err) {
      toast(err.message, { type: "error" });
    }
  });
  recBtn.addEventListener("click", async () => {
    try {
      if (!rec.recording) {
        await api.post("/api/routines/record", { conversation_id: conversationId, action: "start" });
        toast("Recording. Ask Bagley to do the steps, then save from here.");
        d.close();
        bus.emit("recording", { conversationId, recording: true });
      } else {
        if (!name.value.trim()) {
          toast("Name the routine first.");
          return name.focus();
        }
        await api.post("/api/routines/record", { conversation_id: conversationId, action: "stop", name: name.value.trim(), description: description.value.trim() });
        toast(`Saved routine “${name.value.trim()}”`);
        d.close();
        bus.emit("recording", { conversationId, recording: false });
        loadRoutines();
      }
    } catch (err) {
      toast(err.message, { type: "error" });
    }
  });
}

export function routineMeta(r) {
  const last = r.last_run ? `${r.last_status === "failed" ? "Failed" : "Ran"} ${relTime(r.last_run)}` : "Never run";
  return `${r.steps.length} step${r.steps.length === 1 ? "" : "s"} · ${last}`;
}
