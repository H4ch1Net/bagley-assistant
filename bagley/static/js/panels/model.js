// Model & machines: this machine's model server, other machines to route to (a GPU desktop
// over Tailscale, a hosted API as fallback), generation settings and the model manager.

import { api } from "../api.js";
import { bus, state } from "../state.js";
import { confirmDialog, toast } from "../ui.js";
import { el, formatBytes, icon } from "../util.js";
import { field, header, locked, pullModel, RECOMMENDED_MODELS, savePrefs, section, select, toggleRow, value } from "../settings-kit.js";

const ROLES = [["gpu", "GPU machine"], ["local", "Nearby machine"], ["cloud", "Cloud fallback"]];
const KINDS = [["auto", "Auto-detect"], ["ollama", "Ollama"], ["openai", "OpenAI-compatible"], ["anthropic", "Anthropic (Claude)"]];
const CLAUDE_URL = "https://api.anthropic.com";
const CLAUDE_MODEL = "claude-opus-5-5";
const ROLE_HELP = { gpu: "Heavy work goes here first when it answers.", local: "Used after this machine.", cloud: "Last resort when no machine of yours answers." };

export function render(panel, ctx) {
  const provider = select(KINDS, value("provider"), (v) => {
    // Claude needs its own URL and a model; Ollama's localhost URL would never answer.
    const claude = v === "anthropic" && !/^https:/.test(value("base_url") || "") ? { base_url: CLAUDE_URL, model: value("model") || CLAUDE_MODEL } : {};
    saveConnection(ctx, { provider: v, ...claude });
  });
  const baseUrl = el("input", { class: "input mono", value: value("base_url"), spellcheck: "false", placeholder: "http://localhost:11434" });
  baseUrl.addEventListener("change", () => saveConnection(ctx, { base_url: baseUrl.value.trim() }));
  const apiKey = el("input", { class: "input mono", type: "password", autocomplete: "off", placeholder: value("has_api_key") ? "•••••••• saved" : value("provider") === "anthropic" ? "sk-ant-… or ANTHROPIC_API_KEY" : "Not needed for local servers" });
  apiKey.addEventListener("change", () => saveConnection(ctx, { api_key: apiKey.value }));
  const status = el("div", { class: "status-line", "aria-live": "polite" });
  const test = el("button", { class: "btn btn-sm", type: "button", onclick: () => testConnection(status) }, icon("zap", "icon-sm"), "Test connection");

  const modelSelect = el("select", { class: "select mono" });
  const fillModels = () => {
    const chat = state.models.filter((m) => !/embed|minilm|bge-|e5-|gte-/i.test(m.name));
    const names = chat.map((m) => m.name);
    const current = value("model");
    const options = [el("option", { value: "", text: names.length ? `Automatic (${names[0]})` : "Automatic" })];
    for (const m of chat) options.push(el("option", { value: m.name, text: [m.name, m.parameter_size].filter(Boolean).join("  ·  ") }));
    if (current && !names.includes(current)) options.push(el("option", { value: current, text: `${current} (not installed)` }));
    modelSelect.replaceChildren(...options);
    modelSelect.value = current || "";
  };
  fillModels();
  modelSelect.addEventListener("change", () => savePrefs({ model: modelSelect.value }));
  const refresh = el("button", { class: "icon-btn", type: "button", "aria-label": "Refresh model list", title: "Refresh model list", onclick: async () => { await ctx.onModelsChanged(); fillModels(); } }, icon("refresh-cw", "icon-sm"));

  const temp = el("input", { class: "range", type: "range", min: 0, max: 2, step: 0.1, value: value("temperature"), "aria-label": "Temperature" });
  const tempOut = el("output", { text: Number(value("temperature")).toFixed(1) });
  temp.addEventListener("input", () => (tempOut.textContent = Number(temp.value).toFixed(1)));
  temp.addEventListener("change", () => savePrefs({ temperature: Number(temp.value) }));
  if (locked("temperature")) temp.disabled = true;

  const ctxSizes = [2048, 4096, 8192, 16384, 32768, 65536, 131072];
  const current = value("context_tokens");
  if (!ctxSizes.includes(current)) ctxSizes.push(current);
  const ctxSelect = select(ctxSizes.sort((a, b) => a - b).map((n) => [n, `${n.toLocaleString()} tokens`]), current, (v) => savePrefs({ context_tokens: Number(v) }));

  const steps = el("input", { class: "input", type: "number", min: 1, max: 32, value: value("max_steps") });
  steps.addEventListener("change", () => savePrefs({ max_steps: Math.max(1, Math.min(32, Number(steps.value) || 8)) }));

  const toolMode = select([["auto", "Automatic"], ["native", "Native function calling"], ["prompt", "Text-based (any model)"], ["off", "Off"]], value("tool_mode"), (v) => savePrefs({ tool_mode: v }));

  const vision = el("input", { class: "input mono", value: value("vision_model") || "", placeholder: "Automatic (a model that can see)", list: "local-models", spellcheck: "false" });
  vision.addEventListener("change", () => savePrefs({ vision_model: vision.value.trim() }));
  const localModels = el("datalist", { id: "local-models" }, ...state.models.map((m) => el("option", { value: m.name })));

  header(panel, "Model & machines", "Bagley works with Ollama, any OpenAI-compatible server and Claude. Add your other machines and hosted APIs, and it routes each request to the best one that answers.");
  panel.append(
    section("This machine",
      el("div", { class: "field-row" },
        field("Server type", provider, { key: "provider", id: "pref-provider" }),
        field("Server URL", baseUrl, { key: "base_url", id: "pref-base-url", help: "Ollama 11434 · LM Studio 1234 · llama.cpp 8080" }),
      ),
      field("API key", apiKey, { key: "api_key", id: "pref-api-key", help: "Only for hosted APIs (Claude, OpenAI, OpenRouter…). Stored in Bagley's local database, never shown again. Left empty, the usual variable is used, e.g. ANTHROPIC_API_KEY." }),
      el("div", { class: "inline" }, test),
      status,
    ),
    machinesSection(ctx),
    section("Generation",
      field("Model", el("div", { class: "inline" }, modelSelect, refresh), { key: "model", id: "pref-model" }),
      el("div", { class: "field-row" },
        field("Temperature", el("div", { class: "range-row" }, temp, tempOut), { key: "temperature", help: "Lower is more focused, higher is more creative." }),
        field("Context window", ctxSelect, { key: "context_tokens", id: "pref-ctx", help: "Longer remembers more but uses more memory." }),
      ),
      el("div", { class: "field-row" },
        field("Tool calling", toolMode, { key: "tool_mode", id: "pref-tool-mode", help: "Text-based mode lets models without function calling use tools." }),
        field("Max steps per reply", steps, { key: "max_steps", id: "pref-steps", help: "How many tool rounds Bagley may take." }),
      ),
      field("Vision model", vision, { id: "pref-vision", help: "Answers questions about screenshots and images. e.g. qwen2.5vl:7b, gemma3:12b, minicpm-v." }),
      localModels,
      toggleRow("Reasoning", "Let models that support it think before answering. Slower, often better.", value("think"), (v) => savePrefs({ think: v }), { key: "think" }),
      toggleRow("Load tools on demand", "Send the core tools every time and the rest (file changes, automations, system, code, desktop) only when a chat needs them. Saves context and keeps small models focused.", value("tool_routing"), (v) => savePrefs({ tool_routing: v })),
    ),
  );
  if (locked("model")) modelSelect.disabled = true;
  if (state.health?.provider === "ollama" || (!state.health && ["auto", "ollama"].includes(value("provider")))) {
    panel.append(installedSection(ctx), pullSection(ctx));
  }
  for (const extra of EXTRA_SECTIONS) panel.append(extra(ctx));
}

/** Feature modules can add sections (the benchmark does). */
export const EXTRA_SECTIONS = [];

// Machines and routing -------------------------------------------------------------------------

function slug(name, taken) {
  const base = name.toLowerCase().replace(/[^a-z0-9_-]+/g, "-").replace(/^-+|-+$/g, "").slice(0, 28) || "machine";
  let id = base;
  for (let n = 2; taken.has(id) || id === "local" || id === "auto"; n++) id = `${base}-${n}`;
  return id;
}

function machinesSection(ctx) {
  const box = el("div", { class: "section" });
  const machines = () => (value("machines") || []).map((m) => ({ ...m }));
  const save = async (list) => {
    const ok = await savePrefs({ machines: list.map(({ has_api_key, ...m }) => ({ ...m, api_key: m.api_key || "" })) });
    if (ok) {
      await ctx.onModelsChanged();
      draw();
    }
    return ok;
  };

  const statusOf = (id) => state.machines?.machines?.find((m) => m.id === id);

  const row = (m, local = false) => {
    const live = statusOf(local ? "local" : m.id);
    const online = live?.ok;
    const reading = live ? (online ? `Online ${live.latency_ms ?? "?"}ms · ${live.models?.length ?? 0} models` : live.error === "disabled" ? "Disabled" : `Offline · ${live.error || "no answer"}`) : "Checking…";
    const actions = local
      ? el("span", { class: "subtle", style: "font-size:.74rem", text: "this machine" })
      : el("div", { class: "inline", style: "gap:2px" },
          el("button", { class: "icon-btn icon-btn-sm", type: "button", title: "Edit", "aria-label": `Edit ${m.name}`, onclick: () => edit(m) }, icon("pencil", "icon-sm")),
          el("button", { class: "icon-btn icon-btn-sm", type: "button", title: "Remove", "aria-label": `Remove ${m.name}`, onclick: async () => {
            if (!(await confirmDialog({ title: `Remove ${m.name}?`, message: "Bagley stops routing to it. Its saved API key is deleted.", confirm: "Remove", danger: true }))) return;
            await save(machines().filter((x) => x.id !== m.id));
          } }, icon("trash-2", "icon-sm")));
    const toggle = local ? null : el("input", { type: "checkbox", role: "switch", checked: m.enabled !== false, "aria-label": `Use ${m.name}` });
    toggle?.addEventListener("change", () => save(machines().map((x) => (x.id === m.id ? { ...x, enabled: toggle.checked } : x))));
    return el("div", { class: "list-item machine-row", dataset: { machine: local ? "local" : m.id } },
      el("span", { class: `dot ${live ? (online ? "ok" : live.error === "disabled" ? "" : "bad") : ""}`, style: "margin-top:6px" }),
      el("div", { class: "grow" },
        el("div", { class: "name" }, local ? state.prefs.machine : m.name,
          el("span", { class: "badge", text: local ? "local" : m.role }),
          live?.priority === 0 ? el("span", { class: "badge badge-accent", text: "answers now" }) : null,
          !local && m.has_api_key ? el("span", { class: "badge", text: "key saved" }) : null),
        el("div", { class: "desc mono", text: local ? `${value("base_url")} · ${value("model") || "auto model"}` : `${m.base_url} · ${m.model || "auto model"}${m.vision_model ? ` · vision ${m.vision_model}` : ""}` }),
        el("div", { class: `desc${live && !online && live.error !== "disabled" ? " error-text" : ""}`, text: reading }),
      ),
      actions,
      toggle ? el("label", { class: "switch" }, toggle, el("span")) : null,
    );
  };

  const edit = (machine, preset = null) => {
    const draft = machine
      ? { ...machine }
      : preset
        ? { name: preset.name, role: "cloud", provider: preset.provider, base_url: preset.base_url, model: preset.model || "", vision_model: "", enabled: true }
        : { name: "", role: "gpu", provider: "ollama", base_url: "http://", model: "", vision_model: "", enabled: true };
    const name = el("input", { class: "input", value: draft.name, maxlength: 32, placeholder: "e.g. H4CH1" });
    const role = select(ROLES, draft.role, () => (roleHelp.textContent = ROLE_HELP[role.value]));
    const roleHelp = el("div", { class: "help", text: ROLE_HELP[draft.role] });
    const kind = select(KINDS, draft.provider, () => {});
    const url = el("input", { class: "input mono", value: draft.base_url, spellcheck: "false", placeholder: "http://h4ch1:11434 or https://openrouter.ai/api/v1" });
    const keyHint = preset?.key_in_env ? `Empty: uses ${preset.key_env} from the environment` : preset ? `Paste your ${preset.label} key` : "Only for hosted APIs";
    const key = el("input", { class: "input mono", type: "password", autocomplete: "off", placeholder: draft.has_api_key ? "•••••••• saved (leave empty to keep)" : keyHint });
    const keyHelp = preset
      ? el("span", {}, `Saved in Bagley's local database and never shown again. Or set ${preset.key_env} where Bagley runs. `, el("a", { href: preset.keys_url, target: "_blank", rel: "noopener noreferrer", text: "Get a key" }))
      : "Saved in Bagley's local database and never shown again.";
    const modelName = el("input", { class: "input mono", value: draft.model || "", spellcheck: "false", placeholder: preset ? "Press Test key, then pick one" : "Automatic, or e.g. qwen3:14b", list: "machine-models" });
    const visionName = el("input", { class: "input mono", value: draft.vision_model || "", spellcheck: "false", placeholder: "Optional, e.g. qwen2.5vl:7b", list: "machine-models" });
    const list = el("datalist", { id: "machine-models" }, ...(preset?.models || []).map((m) => el("option", { value: m })));
    const probe = el("div", { class: "status-line", "aria-live": "polite" });
    const loadModels = el("button", { class: "btn btn-sm", type: "button" }, icon(preset ? "key-round" : "refresh-cw", "icon-sm"), preset ? "Test key" : "List models");
    loadModels.addEventListener("click", async () => {
      if (!/^https?:\/\/[^/]+/.test(url.value.trim())) return url.focus();
      probe.replaceChildren(el("span", { class: "spinner" }), "Connecting…");
      try {
        // Works before saving: the typed key, else the saved one, else the environment's.
        const data = await api.post("/api/machines/test", { provider: kind.value, base_url: url.value.trim(), api_key: key.value, id: machine?.id || "" });
        if (!data.ok) {
          probe.replaceChildren(el("span", { class: "dot bad" }), `${data.error} ${data.hint || ""}`.trim());
          return;
        }
        list.replaceChildren(...data.models.map((m) => el("option", { value: m.name })));
        probe.replaceChildren(el("span", { class: "dot ok" }), `Connected · ${data.models.length} model${data.models.length === 1 ? "" : "s"}${kind.value === "auto" ? ` · ${data.kind}` : ""}`);
      } catch (err) {
        probe.replaceChildren(el("span", { class: "dot bad" }), err.message);
      }
    });
    const cancel = el("button", { class: "btn", type: "button", text: "Cancel", onclick: () => draw() });
    const submit = el("button", { class: "btn btn-primary", type: "button" }, icon("check", "icon-sm"), machine ? "Save machine" : "Add machine");
    submit.addEventListener("click", async () => {
      const label = name.value.trim();
      if (!label) return name.focus();
      if (!/^https?:\/\/[^/]+/.test(url.value.trim())) return toast("Enter the machine's URL, e.g. http://h4ch1:11434", { type: "error" });
      if (role.value === "cloud" && kind.value === "openai" && !modelName.value.trim()) {
        toast("Pick a model: hosted APIs list hundreds. Test key shows them.", { type: "error" });
        return modelName.focus();
      }
      const list = machines();
      const entry = {
        id: machine?.id || slug(label, new Set(list.map((m) => m.id))),
        name: label, role: role.value, provider: kind.value, base_url: url.value.trim().replace(/\/+$/, ""),
        api_key: key.value, model: modelName.value.trim(), vision_model: visionName.value.trim(), enabled: machine?.enabled !== false,
      };
      await save(machine ? list.map((m) => (m.id === machine.id ? entry : m)) : [...list, entry]);
    });
    box.replaceChildren(
      el("div", { class: "section-title", text: machine ? `Edit ${machine.name}` : preset ? `Add ${preset.label}` : "Add a machine" }),
      el("div", { class: "field-row" }, field("Name", name, { help: "Shown in the readout, e.g. H4CH1." }), el("div", { class: "field" }, el("label", { text: "Role" }), role, roleHelp)),
      el("div", { class: "field-row" }, field("Server type", kind), field("URL", url, { help: preset || draft.role === "cloud" ? "The API's address. Change it only to go through a proxy." : "Over Tailscale use the machine name: http://h4ch1:11434. Ollama must listen beyond localhost (OLLAMA_HOST=0.0.0.0)." })),
      field("API key", key, { help: keyHelp }),
      el("div", { class: "field-row" }, field("Model", modelName), field("Vision model", visionName)),
      list,
      el("div", { class: "inline", style: "flex-wrap:wrap" }, submit, cancel, loadModels),
      probe,
    );
    name.focus();
  };

  const draw = () => {
    const list = machines();
    const routes = [["auto", "Automatic: GPU, then here, then cloud"], ["local", `Only ${state.prefs.machine}`], ...list.map((m) => [m.id, `Only ${m.name}`])];
    const routing = select(routes, value("routing") || "auto", async (v) => {
      if (await savePrefs({ routing: v })) await ctx.onModelsChanged();
    });
    const name = el("input", { class: "input", value: value("machine_name") || "", maxlength: 32, placeholder: state.prefs.machine });
    name.addEventListener("change", async () => {
      if (await savePrefs({ machine_name: name.value.trim() })) await ctx.onModelsChanged();
    });
    const route = state.route;
    box.replaceChildren(
      el("div", { class: "section-title", text: "Machines and routing" }),
      el("div", { class: "field-row" },
        field("Routing", routing, { id: "pref-routing", help: "Automatic falls back to the next machine when one doesn't answer." }),
        field("This machine's name", name, { help: "Shown in the NODE readout." }),
      ),
      toggleRow("Keep light work on this machine", "Chat titles and shell one-liners use this machine's model, so the GPU machine isn't woken for small things.", value("light_local"), (v) => savePrefs({ light_local: v })),
      el("div", { class: "status-line" },
        route ? el("span", { class: `dot ${route.ok ? "ok" : "bad"}` }) : el("span", { class: "spinner" }),
        route ? (route.ok ? `Answering now: ${route.machine} · ${route.model}` : `${route.error} ${route.hint || ""}`) : "Checking machines…"),
      el("div", { class: "list", style: "margin-top:10px" }, row({}, true), ...list.map((m) => row(m))),
      hostedApis(list, (preset) => edit(null, preset)),
      el("div", { class: "inline", style: "margin-top:12px;flex-wrap:wrap" },
        el("button", { class: "btn", type: "button", onclick: () => edit(null) }, icon("plus", "icon-sm"), "Add machine"),
        el("button", { class: "btn btn-ghost", type: "button", onclick: async () => {
          state.machines = await api.get("/api/machines?fresh=true").catch(() => state.machines);
          state.route = await api.get("/api/machines/route").catch(() => state.route);
          bus.emit("health");
          draw();
        } }, icon("refresh-cw", "icon-sm"), "Check all")),
    );
  };
  draw();
  return box;
}

/** One button per hosted API (Claude first); each opens the machine form filled in. */
function hostedApis(list, open) {
  const box = el("div", { class: "hosted", "aria-label": "Hosted APIs" },
    el("div", { class: "field-label", text: "Hosted APIs" }),
    el("p", { class: "help", style: "margin:0 0 8px", text: "Use an API key as a cloud fallback, or route to it only (Routing above). Keys stay on this machine." }));
  const grid = el("div", { class: "hosted-grid" });
  box.append(grid);
  api.get("/api/machines/presets").then((presets) => {
    grid.replaceChildren(...presets.map((p) => {
      const added = list.some((m) => (m.base_url || "").replace(/\/+$/, "") === p.base_url);
      return el("button", { class: "hosted-api", type: "button", dataset: { preset: p.id }, onclick: () => open(p) },
        el("span", { class: "name", text: p.label }),
        el("span", { class: "state", text: added ? "ADDED" : p.key_in_env ? "KEY IN ENV" : "ADD" }));
    }));
  }).catch(() => box.remove());
  return box;
}

// Installed models -------------------------------------------------------------------------------

function installedSection(ctx) {
  const list = el("div", { class: "list" });
  const memory = el("div", { class: "help", "aria-live": "polite", style: "margin-bottom:8px" });
  const refresh = async () => {
    const loaded = await api.get("/api/models/loaded").catch(() => []);
    const inMemory = new Map(loaded.map((m) => [m.name, m]));
    const vram = loaded.reduce((sum, m) => sum + (m.size_vram || 0), 0);
    memory.textContent = loaded.length
      ? `${loaded.length} in memory · ${formatBytes(vram)} on the GPU`
      : "Nothing in memory. A model loads on its first message and unloads after a few idle minutes.";
    const rows = state.models.map((m) => modelRow(ctx, m, inMemory.get(m.name), refresh));
    list.replaceChildren(...(rows.length ? rows : [el("div", { class: "list-empty", text: "No models installed." })]));
  };
  refresh();
  return section("Installed models", memory, list);
}

function modelRow(ctx, m, loaded, refresh) {
  const active = state.health?.model === m.name;
  const unload = loaded && el("button", { class: "btn btn-sm", type: "button", title: "Free its memory now" }, icon("power", "icon-xs"), "Unload");
  unload?.addEventListener("click", async () => {
    unload.disabled = true;
    try {
      await api.post("/api/models/unload", { name: m.name });
      toast(`Unloaded ${m.name}`);
    } catch (err) {
      toast(err.message, { type: "error" });
    }
    await refresh();
  });
  const remove = el("button", { class: "icon-btn icon-btn-sm", type: "button", title: "Delete", "aria-label": `Delete ${m.name}` }, icon("trash-2", "icon-sm"));
  remove.addEventListener("click", async () => {
    const ok = await confirmDialog({
      title: `Delete ${m.name}?`,
      message: `This frees ${formatBytes(m.size || 0)} of disk. You can download it again later.`,
      confirm: "Delete",
      danger: true,
    });
    if (!ok) return;
    try {
      await api.del(`/api/models/${encodeURIComponent(m.name)}`);
      toast(`Deleted ${m.name}`);
    } catch (err) {
      toast(err.message, { type: "error" });
    }
    await ctx.onModelsChanged();
    ctx.refresh();
  });
  const facts = [m.parameter_size, m.quantization, m.size ? formatBytes(m.size) : ""].filter(Boolean).join(" · ");
  return el("div", { class: "list-item model-row", dataset: { name: m.name } },
    el("span", { class: "tool-icon" }, icon(loaded ? "activity" : "hard-drive", "icon-sm")),
    el("div", { class: "grow" },
      el("div", { class: "name" }, m.name,
        active ? el("span", { class: "badge", text: "in use" }) : null,
        loaded ? el("span", { class: "badge badge-ok", text: `in memory${loaded.size_vram ? ` · ${formatBytes(loaded.size_vram)} VRAM` : ""}` }) : null),
      facts ? el("div", { class: "desc", text: facts }) : null,
    ),
    el("div", { class: "inline", style: "gap:4px" }, unload, remove),
  );
}

async function saveConnection(ctx, changes) {
  if (await savePrefs(changes)) {
    await ctx.onModelsChanged();
    ctx.refresh();
  }
}

async function testConnection(status) {
  status.replaceChildren(el("span", { class: "spinner" }), "Checking…");
  const health = await api.get("/api/health").catch((err) => ({ ok: false, error: err.message }));
  state.health = health;
  bus.emit("health");
  if (health.ok) {
    status.replaceChildren(el("span", { class: "dot ok" }), `Connected to ${health.provider} ${health.version || ""} · ${health.models} model${health.models === 1 ? "" : "s"}`);
  } else {
    status.replaceChildren(el("span", { class: "dot bad" }), health.error || "Not reachable");
  }
}

function pullSection(ctx) {
  const input = el("input", { class: "input mono", placeholder: "e.g. qwen3:4b", "aria-label": "Model to download", spellcheck: "false" });
  const progress = el("div", { class: "progress", hidden: true }, el("i"));
  const statusText = el("div", { class: "pull-status" });
  const button = el("button", { class: "btn", type: "button" }, icon("hard-drive-download", "icon-sm"), "Download");
  const run = async (name) => {
    name = (name || input.value).trim();
    if (!name) return input.focus();
    input.value = name;
    button.disabled = true;
    await pullModel(name, { progress, status: statusText });
    button.disabled = false;
    await ctx.onModelsChanged();
    ctx.refresh();
  };
  button.addEventListener("click", () => run());
  input.addEventListener("keydown", (e) => e.key === "Enter" && run());
  const installed = new Set(state.models.map((m) => m.name));
  return section("Download a model",
    el("div", { class: "inline" }, input, button),
    progress,
    statusText,
    el("div", { class: "inline", style: "flex-wrap:wrap;margin-top:12px" },
      ...RECOMMENDED_MODELS.map((m) => el("button", {
        class: "btn btn-sm", type: "button", title: m.note, disabled: installed.has(m.name),
        onclick: () => run(m.name),
      }, installed.has(m.name) ? icon("check", "icon-xs") : icon("plus", "icon-xs"), m.name)),
    ),
    el("div", { class: "help", style: "margin-top:8px", text: "Browse more at ollama.com/library. Models with tool support work best. On a 20 GB GPU, 14B models run fully on the card; 30B mixture-of-experts models fit too." }),
  );
}
