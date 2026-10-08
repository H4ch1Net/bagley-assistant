// Benchmark: test every installed model on real tool-use tasks, per machine, and pick the best.
// Shown as a section of Model & machines.

import { api } from "../api.js";
import { toast } from "../ui.js";
import { el, icon } from "../util.js";
import { EXTRA_SECTIONS } from "./model.js";
import { savePrefs, value } from "../settings-kit.js";

const num = (v, digits = 1) => (v === null || v === undefined ? "--" : Number(v).toFixed(digits));

function table(ctx, machine, draw) {
  const head = ["", "Model", "Pass", "Avg", "Tok/s", "Score", ""];
  const use = async (model) => {
    const ok = machine.machine === "local"
      ? await savePrefs({ model: model.model })
      : await savePrefs({ machines: (value("machines") || []).map((m) => (m.id === machine.machine ? { ...m, api_key: "", model: model.model } : { ...m, api_key: "" })) });
    if (ok) {
      toast(`${machine.name} now uses ${model.model}`);
      await ctx.onModelsChanged();
      draw();
    }
  };
  const current = machine.machine === "local" ? value("model") : (value("machines") || []).find((m) => m.id === machine.machine)?.model;
  return el("div", { style: "margin-bottom:14px" },
    el("div", { class: "help", style: "margin-bottom:6px", text: `${machine.name} // ${new Date(machine.created_at * 1000).toLocaleString()}` }),
    el("div", { class: "prose" }, el("table", {},
      el("thead", {}, el("tr", {}, ...head.map((h) => el("th", { text: h })))),
      el("tbody", {}, ...machine.models.map((m) => el("tr", { title: m.error || m.tasks?.filter((t) => !t.ok).map((t) => `${t.task}: ${t.reason}`).join("\n") || "" },
        el("td", { text: m.winner ? ">" : "" }),
        el("td", {}, el("code", { text: m.model }), m.mode === "prompt" ? el("span", { class: "subtle", text: " text" }) : null),
        el("td", { text: `${m.passed}/${m.total}`, style: m.passed === m.total ? "color:var(--ok)" : m.passed ? "" : "color:var(--danger)" }),
        el("td", { text: `${num(m.avg_seconds)}s` }),
        el("td", { text: num(m.tokens_per_second, 0) }),
        el("td", { text: num(m.score, 0) }),
        el("td", {}, m.model === current ? el("span", { class: "badge badge-ok", text: "in use" }) : el("button", { class: "btn btn-sm btn-ghost", type: "button", onclick: () => use(m) }, "Use")),
      ))),
    )),
  );
}

function benchSection(ctx) {
  const box = el("div", { class: "section" });
  const results = el("div");
  const progress = el("div", { class: "progress", hidden: true }, el("i"));
  const line = el("div", { class: "pull-status", "aria-live": "polite" });
  const apply = el("input", { type: "checkbox", id: "bench-apply" });
  const models = el("input", { class: "input mono", placeholder: "Only these models (globs), e.g. qwen3:* gpt-oss*", spellcheck: "false", "aria-label": "Models to test" });
  const run = el("button", { class: "btn", type: "button" }, icon("flask-conical", "icon-sm"), "Run benchmark");

  const draw = async () => {
    let data;
    try {
      data = await api.get("/api/bench");
    } catch {
      results.replaceChildren(el("p", { class: "help", text: "The benchmark is not available on this server." }));
      return;
    }
    run.disabled = data.running;
    results.replaceChildren(...(data.machines.length ? data.machines.map((m) => table(ctx, m, draw)) : [el("p", { class: "help", text: `No results yet. ${data.tasks.length} tasks: ${data.tasks.map((t) => t.title).join(", ")}.` })]));
  };

  run.addEventListener("click", async () => {
    run.disabled = true;
    progress.hidden = false;
    const bar = progress.firstElementChild;
    bar.style.width = "0%";
    line.textContent = "Starting";
    const globs = models.value.trim().split(/\s+/).filter(Boolean);
    try {
      await api.stream("/api/bench/run", { apply: apply.checked, ...(globs.length ? { models: globs } : {}) }, (ev) => {
        if (ev.type === "bench.task") {
          bar.style.width = `${(ev.index / ev.total) * 100}%`;
          line.textContent = `[${String(ev.index).padStart(2, "0")}/${String(ev.total).padStart(2, "0")}] ${ev.machine} ${ev.model} ${ev.task} ${ev.ok ? "OK" : "FAIL"} ${num(ev.seconds)}s`;
        } else if (ev.type === "bench.done") {
          bar.style.width = "100%";
          const applied = Object.entries(ev.applied?.applied || {}).map(([m, model]) => `${m} → ${model}`);
          line.textContent = applied.length ? `Done. Applied: ${applied.join(", ")}` : "Done.";
        } else if (ev.type === "error") {
          line.textContent = ev.message;
        }
      });
    } catch (err) {
      toast(err.message, { type: "error" });
    }
    if (apply.checked) await ctx.onModelsChanged();
    await draw();
  });

  box.append(
    el("div", { class: "section-title", text: "Benchmark" }),
    el("p", { class: "help", style: "margin:0 0 10px", text: "Tests every installed model on each machine with real tool-use tasks (weather, maths, time zones, picking the right tool, multi-step answers) and scores pass rate and speed. Also: bagley bench --apply." }),
    models,
    el("div", { class: "inline", style: "margin-top:8px;flex-wrap:wrap" }, run, el("label", { class: "inline help", style: "gap:6px" }, apply, "Use the winners")),
    progress, line, results,
  );
  draw();
  return box;
}

if (!EXTRA_SECTIONS.includes(benchSection)) EXTRA_SECTIONS.push(benchSection);
