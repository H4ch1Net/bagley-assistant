// Work: the clients you look after, their asset inventory, health checks and client-ready
// ticket summaries in your working languages.

import { api } from "../api.js";
import { addKind } from "./automations.js";
import { setMarkdown } from "../markdown.js";
import { confirmDialog, toast } from "../ui.js";
import { copyText, el, icon, relTime } from "../util.js";
import { field, header, section, value } from "../settings-kit.js";

addKind({
  id: "health", icon: "activity", label: "Client health",
  help: "Pings and port, web and certificate checks on a client's assets. You hear about it when something goes down or comes back.",
  when: "every 1 hour", name: "e.g. ACME health", defaultName: "Client health",
  prompt: "Instructions (optional)", placeholder: "Optional. Include the word always to be told after every run.",
  target: { label: "Client (empty for all)", placeholder: "e.g. ACME" },
  note: "No model needed.",
}); // prettier-ignore

const KINDS = ["laptop", "desktop", "server", "printer", "switch", "router", "firewall", "nas", "phone", "vm", "other"];
const STATUS = { OK: "ok", WARN: "warn", CRIT: "crit", UNKNOWN: "unknown" };

function statusLabel(status) {
  const s = status || "UNCHECKED";
  return el("span", { class: `wd-status ${STATUS[s] || "unknown"}`, text: STATUS[s] ? `[${s}]` : "[--]", title: s });
}

const checksText = (checks = []) => checks.map((c) => [c.type, c.port, c.target].filter(Boolean).join(" ")).join(" · ") || "no checks";

export function render(panel, ctx) {
  ctx.workClient ??= "";
  const clients = el("div", { class: "client-strip", role: "group", "aria-label": "Clients" });
  const assets = el("div", { class: "list" });
  const formBox = el("div");
  const health = el("div", { class: "wd-view" });
  const count = el("span", { class: "subtle" });

  const drawClients = async () => {
    let data = { clients: [] };
    try {
      data = await api.get("/api/assets/clients");
    } catch {
      clients.replaceChildren(el("div", { class: "list-empty", text: "The work profile is not available on this server." }));
      return;
    }
    const all = el("button", { class: "client-card", type: "button", "aria-pressed": String(!ctx.workClient), onclick: () => pick("") },
      el("span", { class: "name", text: "ALL CLIENTS" }), el("span", { class: "state", text: `${data.clients.reduce((n, c) => n + c.count, 0)} ASSETS` }));
    clients.replaceChildren(all, ...data.clients.map((c) => el("button", { class: "client-card", type: "button", "aria-pressed": String(ctx.workClient === c.client), onclick: () => pick(c.client) },
      el("span", { class: "name", text: c.client }),
      el("span", { class: "state" }, statusLabel(c.worst), ` ${c.count} // ${c.last_checked ? relTime(c.last_checked) : "NOT CHECKED"}`))));
  };

  const drawAssets = async () => {
    let data;
    try {
      data = await api.get(`/api/assets${ctx.workClient ? `?client=${encodeURIComponent(ctx.workClient)}` : ""}`);
    } catch (err) {
      assets.replaceChildren(el("div", { class: "list-empty", text: err.message }));
      return;
    }
    count.textContent = ` ${data.count}`;
    assets.replaceChildren(...(data.assets.length ? data.assets.map(assetRow) : [el("div", { class: "list-empty", text: "No assets yet. Add one, import a CSV, or ask in chat: “add ACME's NAS, nas01 at 10.0.0.5”." })]));
  };

  const pick = (client) => {
    ctx.workClient = client;
    drawClients();
    drawAssets();
  };
  const changed = () => {
    drawClients();
    drawAssets();
  };

  const assetRow = (a) => el("div", { class: "list-item asset-row", dataset: { id: a.id } },
    statusLabel(a.status),
    el("div", { class: "grow" },
      el("div", { class: "name" }, a.name, el("span", { class: "badge", text: a.kind }), ctx.workClient ? null : el("span", { class: "badge", text: a.client }),
        a.tags?.map((t) => el("span", { class: "badge", text: `#${t}` }))),
      el("div", { class: "desc mono", text: [a.hostname, a.ip, a.os, a.warranty_until && `warranty ${a.warranty_until}`].filter(Boolean).join(" · ") || "no address" }),
      el("div", { class: `desc${a.status === "CRIT" ? " error-text" : ""}`, text: a.checked_at ? `${a.status_summary || a.status} · ${relTime(a.checked_at)}` : checksText(a.checks) }),
    ),
    el("div", { class: "inline", style: "gap:2px" },
      el("button", { class: "icon-btn icon-btn-sm", type: "button", title: "Edit", "aria-label": `Edit ${a.name}`, onclick: () => edit(a) }, icon("pencil", "icon-sm")),
      el("button", { class: "icon-btn icon-btn-sm", type: "button", title: "Delete", "aria-label": `Delete ${a.name}`, onclick: async () => {
        if (!(await confirmDialog({ title: `Delete ${a.name}?`, message: "It leaves the inventory and its checks stop.", confirm: "Delete", danger: true }))) return;
        await api.del(`/api/assets/${a.id}`).catch((err) => toast(err.message, { type: "error" }));
        changed();
      } }, icon("trash-2", "icon-sm"))),
  );

  const edit = (asset) => {
    const a = asset || { client: ctx.workClient, kind: "server" };
    const input = (key, placeholder, extra = {}) => el("input", { class: `input${extra.mono ? " mono" : ""}`, value: a[key] || "", placeholder, spellcheck: "false" });
    const f = {
      client: input("client", "e.g. ACME"), name: input("name", "e.g. nas01"),
      hostname: input("hostname", "nas01.acme.lan", { mono: true }), ip: input("ip", "10.0.0.5", { mono: true }),
      os: input("os", "e.g. Windows Server 2022"), serial: input("serial", "Serial number", { mono: true }),
      owner: input("owner", "Who uses it"), location: input("location", "e.g. Server room"),
      warranty_until: el("input", { class: "input mono", value: a.warranty_until || "", placeholder: "YYYY-MM-DD" }),
    };
    const kind = el("select", { class: "select" }, ...KINDS.map((k) => el("option", { value: k, text: k, selected: a.kind === k })));
    const tags = el("input", { class: "input", value: (a.tags || []).join(", "), placeholder: "e.g. critical, office" });
    const notes = el("textarea", { class: "textarea", rows: 2, text: a.notes || "" });
    const checks = el("textarea", { class: "textarea mono", rows: 2, spellcheck: "false", placeholder: 'Empty: default for the kind. Or JSON, e.g. [{"type": "tcp", "port": 3389}, {"type": "tls", "port": 443}]', text: a.custom_checks?.length ? JSON.stringify(a.custom_checks) : "" });
    const save = el("button", { class: "btn btn-primary", type: "button" }, icon("check", "icon-sm"), asset ? "Save asset" : "Add asset");
    save.addEventListener("click", async () => {
      const body = Object.fromEntries(Object.entries(f).map(([k, v]) => [k, v.value.trim()]));
      if (!body.client) return f.client.focus();
      if (!body.name) return f.name.focus();
      body.kind = kind.value;
      body.tags = tags.value;
      body.notes = notes.value.trim();
      body.warranty_until ||= null;
      let parsed = null;
      if (checks.value.trim()) {
        try {
          parsed = JSON.parse(checks.value);
        } catch {
          return toast("Checks must be JSON, or empty for the default.", { type: "error" });
        }
      }
      try {
        if (asset) await api.patch(`/api/assets/${asset.id}`, { fields: { ...body, checks: parsed } });
        else await api.post("/api/assets", { ...body, ...(parsed ? { checks: parsed } : {}) });
        toast(asset ? `${body.name} saved` : `${body.name} added`);
        formBox.replaceChildren();
        changed();
      } catch (err) {
        toast(err.message, { type: "error" });
      }
    });
    formBox.replaceChildren(el("div", { class: "section asset-form", style: "margin-top:10px" },
      el("div", { class: "section-title", text: asset ? `Edit ${asset.name}` : "New asset" }),
      el("div", { class: "field-row" }, field("Client", f.client), field("Name", f.name)),
      el("div", { class: "field-row" }, field("Kind", kind), field("Tags", tags)),
      el("div", { class: "field-row" }, field("Hostname", f.hostname), field("IP address", f.ip)),
      el("div", { class: "field-row" }, field("Operating system", f.os), field("Serial", f.serial)),
      el("div", { class: "field-row" }, field("Owner", f.owner), field("Location", f.location)),
      el("div", { class: "field-row" }, field("Warranty until", f.warranty_until), field("Checks", checks, { help: "ping, tcp (port), http (URL or host), tls (certificate expiry)." })),
      field("Notes", notes),
      el("div", { class: "inline" }, save, el("button", { class: "btn", type: "button", text: "Cancel", onclick: () => formBox.replaceChildren() })),
    ));
    (asset ? f.name : f.client.value ? f.name : f.client).focus();
  };

  const csv = el("input", { type: "file", accept: ".csv,text/csv", hidden: true });
  csv.addEventListener("change", async () => {
    const file = csv.files?.[0];
    csv.value = "";
    if (!file) return;
    try {
      const res = await fetch(`/api/assets/import${ctx.workClient ? `?client=${encodeURIComponent(ctx.workClient)}` : ""}`, { method: "POST", headers: { "Content-Type": "text/csv" }, body: await file.text() });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || "Import failed");
      toast(`Imported: ${data.added} added · ${data.updated} updated${data.errors.length ? ` · ${data.errors.length} errors (line ${data.errors[0].line}: ${data.errors[0].error})` : ""}`);
      changed();
    } catch (err) {
      toast(err.message, { type: "error" });
    }
  });

  const showHealth = (result) => {
    if (!result?.assets?.length) {
      health.replaceChildren(el("p", { class: "help", text: "No checks yet. Assets without checks of their own get the default for their kind (servers, printers, NAS, network gear)." }));
      return;
    }
    const term = el("pre", { class: "term" });
    const line = (parts) => term.append(el("span", { class: "term-row" }, ...parts.map(([cls, text]) => el("span", { class: cls, text })), "\n"));
    const s = result.summary || {};
    line([["g", "» HEALTH // "], ["w", ctx.workClient ? ctx.workClient.toUpperCase() : "ALL CLIENTS"], ["g", ` // OK ${s.OK ?? 0} WARN ${s.WARN ?? 0} CRIT ${s.CRIT ?? 0} UNKNOWN ${s.UNKNOWN ?? 0}`], ["g", result.checked_at ? ` // ${relTime(result.checked_at).toUpperCase()}` : ""]]);
    for (const a of result.assets) {
      const cls = a.status === "OK" ? "ok" : a.status === "CRIT" ? "bad" : a.status === "WARN" ? "warn" : "g";
      line([[cls, `[${(a.status || "--").padEnd(4)}] `], ["w", `${a.client}/${a.name}`.padEnd(28)], ["g", ` ${a.summary || ""}`], ["warn", a.changed ? `  ${a.previous || "--"}→${a.status}` : ""]]);
      for (const c of a.checks || []) {
        if (c.status !== "OK") line([["g", `        ${c.type} ${c.port || c.target || ""}`.padEnd(30)], [c.status === "CRIT" ? "bad" : "g", ` ${c.detail || c.status}`]]);
      }
    }
    for (const fnd of result.findings || []) line([["warn", "[INFO] "], ["", `${fnd.client}/${fnd.name}: ${fnd.detail}`]]);
    health.replaceChildren(term);
  };
  const run = el("button", { class: "btn btn-primary", type: "button" }, icon("scan", "icon-sm"), "Run checks");
  run.addEventListener("click", async () => {
    run.disabled = true;
    health.replaceChildren(el("div", { class: "status-line" }, el("span", { class: "spinner" }), "Checking"));
    try {
      showHealth(await api.post("/api/work/health", ctx.workClient ? { client: ctx.workClient } : {}));
      changed();
    } catch (err) {
      health.replaceChildren(el("div", { class: "status-line error-text", text: err.message }));
    }
    run.disabled = false;
  });

  header(panel, `${value("work_name") || "Work"} // clients`, [
    "The clients you look after: their machines, health checks and ticket summaries. In the ",
    el("em", { text: "work" }), " mode Bagley uses all of this in chat. In the terminal: bagley assets, bagley health, bagley ticket.",
  ]);
  panel.append(
    section("Clients", clients),
    el("div", { class: "section" },
      el("div", { class: "section-title" }, "Assets", count),
      el("div", { class: "inline", style: "flex-wrap:wrap;margin-bottom:10px" },
        el("button", { class: "btn", type: "button", onclick: () => edit(null) }, icon("plus", "icon-sm"), "Add asset"),
        el("button", { class: "btn btn-ghost", type: "button", onclick: () => csv.click() }, icon("upload", "icon-sm"), "Import CSV"),
        el("a", { class: "btn btn-ghost", href: "/api/assets/export", download: "", onclick: (e) => { e.currentTarget.href = `/api/assets/export${ctx.workClient ? `?client=${encodeURIComponent(ctx.workClient)}` : ""}`; } }, icon("download", "icon-sm"), "Export CSV"), csv),
      formBox, assets),
    section("Health", el("div", { class: "inline", style: "margin-bottom:10px" }, run, el("span", { class: "help", text: "Schedule it under Automations → Client health." })), health),
    ticketSection(),
  );
  drawClients();
  drawAssets();
  api.get(`/api/work/health/last${ctx.workClient ? `?client=${encodeURIComponent(ctx.workClient)}` : ""}`).then(showHealth).catch(() => showHealth(null));
}

/** Ticket notes in, a client-ready summary in each working language out. */
function ticketSection() {
  const notes = el("textarea", { class: "textarea", rows: 6, placeholder: "Paste your notes: what was reported, what you found, what you did, what's left.\ne.g. user can't print, spooler stuck on PRN-02, cleared queue, restarted spooler, test page ok, to watch" });
  const client = el("input", { class: "input", placeholder: "Client (optional)" });
  const langs = el("input", { class: "input", value: (value("work_languages") || ["English", "French"]).join(", ") });
  const out = el("div", { class: "prose ticket-out" });
  const meta = el("div", { class: "help" });
  const copy = el("button", { class: "btn btn-sm", type: "button", hidden: true }, icon("copy", "icon-sm"), "Copy");
  let markdown = "";
  copy.addEventListener("click", async () => {
    await copyText(markdown);
    toast("Copied");
  });
  const go = el("button", { class: "btn btn-primary", type: "button" }, icon("ticket", "icon-sm"), "Write summary");
  go.addEventListener("click", async () => {
    if (!notes.value.trim()) return notes.focus();
    go.disabled = true;
    out.replaceChildren(el("div", { class: "status-line" }, el("span", { class: "spinner" }), "Writing"));
    try {
      const res = await api.post("/api/work/ticket-summary", {
        notes: notes.value, client: client.value.trim() || null,
        languages: langs.value.split(",").map((l) => l.trim()).filter(Boolean).slice(0, 4),
      });
      markdown = res.markdown;
      setMarkdown(out, markdown);
      meta.textContent = [res.machine, res.model].filter(Boolean).join(" // ");
      copy.hidden = false;
    } catch (err) {
      out.replaceChildren(el("div", { class: "status-line error-text", text: err.message }));
    }
    go.disabled = false;
  });
  return section("Ticket summary",
    field("Notes", notes),
    el("div", { class: "field-row" }, field("Client", client), field("Languages", langs, { help: "Comma separated. Default from the work profile." })),
    el("div", { class: "inline", style: "margin-bottom:10px" }, go, copy),
    out, meta);
}
