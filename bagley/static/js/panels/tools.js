// Tools & permissions: a tier per tool (allow, ask first, deny), the shell sandbox, MCP servers.

import { api } from "../api.js";
import { state } from "../state.js";
import { confirmDialog } from "../ui.js";
import { el, icon } from "../util.js";
import { header, notice, savePrefs, section, select, toggleRow, value } from "../settings-kit.js";

const CATEGORIES = {
  web: "Web", utility: "Utilities", files: "Files", knowledge: "Knowledge", memory: "Memory",
  system: "System", desktop: "Desktop", automation: "Automations", routines: "Routines",
  security: "Security", work: "Work", study: "Study", life: "Your life", mcp: "MCP servers", general: "Other",
}; // prettier-ignore
const TIERS = [["allow", "Allow"], ["ask", "Ask"], ["deny", "Deny"]];

export function render(panel, ctx) {
  const info = state.toolsInfo || {};
  const grouped = new Map();
  for (const t of state.tools) {
    const key = CATEGORIES[t.category] ? t.category : "general";
    if (!grouped.has(key)) grouped.set(key, []);
    grouped.get(key).push(t);
  }
  const overrides = value("tool_permissions") || {};
  const reload = async () => {
    const data = await api.get("/api/tools");
    state.tools = data.tools;
    state.toolsInfo = data;
    ctx.refresh();
  };
  const setTier = async (tool, tier) => {
    const next = { ...(value("tool_permissions") || {}) };
    if (tier === tool.default_permission) delete next[tool.name];
    else next[tool.name] = tier;
    // The old on/off list is folded into the tiers.
    const disabled = (value("disabled_tools") || []).filter((n) => n !== tool.name);
    if (await savePrefs({ tool_permissions: next, disabled_tools: disabled })) await reload();
  };
  header(panel, "Tools & permissions", ["What Bagley can do, and when it asks. ", el("strong", { text: "Allow" }), " runs without asking, ", el("strong", { text: "Ask" }), " waits for your approval every time, ", el("strong", { text: "Deny" }), " hides the tool. Scheduled runs never use tools that ask by default, whatever their tier."]);

  const changed = Object.keys(overrides).length;
  panel.append(section("",
    el("div", { class: "inline", style: "flex-wrap:wrap" },
      el("span", { class: "help", text: changed ? `${changed} tool${changed === 1 ? "" : "s"} changed from the default.` : "All tools use their default tier." }),
      changed ? el("button", { class: "btn btn-sm btn-ghost", type: "button", onclick: async () => {
        if (!(await confirmDialog({ title: "Reset all permissions?", message: "Every tool goes back to its default: risky tools ask, the rest are allowed.", confirm: "Reset" }))) return;
        if (await savePrefs({ tool_permissions: {}, disabled_tools: [] })) await reload();
      } }, icon("undo-2", "icon-sm"), "Reset to defaults") : null),
  ));

  for (const [cat, tools] of [...grouped].sort((a, b) => Object.keys(CATEGORIES).indexOf(a[0]) - Object.keys(CATEGORIES).indexOf(b[0]))) {
    panel.append(el("div", { class: "section" },
      el("div", { class: "section-title", text: CATEGORIES[cat] }),
      el("div", { class: "list" }, ...tools.map((t) => {
        const seg = el("div", { class: "segmented", role: "group", "aria-label": `Permission for ${t.name}` },
          ...TIERS.map(([tier, label]) => el("button", {
            type: "button", "aria-pressed": String(t.permission === tier), title: tier === t.default_permission ? "Default" : "",
            onclick: () => t.permission !== tier && setTier(t, tier),
          }, label)));
        return el("div", { class: "list-item", style: "align-items:center" },
          el("div", { class: "grow" },
            el("div", { class: "name" }, t.name,
              t.default_permission === "ask" ? el("span", { class: "badge badge-warn", text: "asks first" }) : null,
              t.permission !== t.default_permission ? el("span", { class: "badge badge-accent", text: "changed" }) : null,
              t.source !== "builtin" ? el("span", { class: "badge", text: t.source }) : null,
            ),
            el("div", { class: "desc", text: t.description.split("\n")[0] }),
          ),
          seg,
        );
      })),
    ));
  }

  const shell = info.shell_enabled;
  const sandbox = select([["off", "Off: run in the workspace as you"], ["bwrap", "bubblewrap: no network, no home, workspace only"]], value("sandbox") || "off", (v) => savePrefs({ sandbox: v }).then(() => ctx.refresh()));
  panel.append(section("Shell sandbox",
    el("p", { class: "help", style: "margin:0 0 10px", text: "run_command and run_python can run inside bubblewrap: the rest of your home folder and Bagley's database are hidden, the workspace is the only writable place, and the network is cut unless you allow it. With the sandbox on and bwrap missing, commands refuse to run rather than run unprotected." }),
    el("div", { class: "field" }, el("label", { for: "pref-sandbox", text: "Sandbox" }), Object.assign(sandbox, { id: "pref-sandbox" })),
    toggleRow("Network inside the sandbox", "Let sandboxed commands reach the network (pip install, curl).", value("sandbox_network"), (v) => savePrefs({ sandbox_network: v })),
    shell ? null : notice("Shell commands are off. Start Bagley with BAGLEY_ENABLE_SHELL=true to let it run commands (each one still asks first unless you allow it)."),
  ));

  const mcp = info.mcp || [];
  panel.append(section("MCP servers",
    mcp.length
      ? el("div", { class: "list" }, ...mcp.map((s) => el("div", { class: "list-item" },
          el("span", { class: `dot ${s.state === "running" ? "ok" : "bad"}`, style: "margin-top:6px" }),
          el("div", { class: "grow" },
            el("div", { class: "name" }, s.name, s.trusted ? el("span", { class: "badge", text: "trusted" }) : null),
            el("div", { class: "desc", text: s.state === "running" ? `${s.tools} tool${s.tools === 1 ? "" : "s"}` : s.error || s.state }),
          ),
        )))
      : el("p", { class: "help" }, "Connect Model Context Protocol servers by listing them in ", el("code", { class: "mono", text: `${state.info?.data_dir || "~/.bagley"}/mcp.json` }), ", then restart Bagley."),
  ));
  const notes = [];
  if (state.info?.workspace) notes.push(`File tools only see ${state.info.workspace}. Change it with BAGLEY_WORKSPACE.`);
  for (const err of info.errors || []) notes.push(`${err.source}: ${err.error}`);
  if (notes.length) panel.append(section("", ...notes.map((n) => notice(n))));
}
