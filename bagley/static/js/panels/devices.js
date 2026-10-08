// Desktop & phone: Bagley on the Hyprland desktop (overlay, bar segment, hotkeys, shell),
// on the phone over Tailscale, the always-on runner, and moving your data between them.

import { api } from "../api.js";
import { toast } from "../ui.js";
import { copyText, el, icon } from "../util.js";
import { field, header, savePrefs, section, value } from "../settings-kit.js";

function command(text, note) {
  const copy = el("button", { class: "icon-btn icon-btn-sm", type: "button", "aria-label": `Copy ${text}`, title: "Copy", onclick: () => copyText(text).then(() => toast("Copied")) }, icon("copy", "icon-sm"));
  return el("div", { style: "margin-bottom:8px" }, el("div", { class: "cmd" }, el("code", { text }), copy), note ? el("div", { class: "help", style: "margin-top:3px", text: note }) : null);
}

function keys(rows) {
  return el("div", { class: "shortcut-list", style: "margin-bottom:12px" }, ...rows.flatMap(([combo, what]) => [el("span", { text: what }), el("span", {}, ...combo.split("+").map((k) => el("kbd", { text: k })))]));
}

function check(ok, label, detail) {
  return el("div", { class: "list-item", style: "align-items:center" },
    el("span", { class: `dot ${ok ? "ok" : ok === false ? "bad" : ""}` }),
    el("div", { class: "grow" }, el("div", { class: "name", text: label }), detail ? el("div", { class: "desc", text: detail }) : null));
}

export function render(panel, ctx) {
  header(panel, "Desktop & phone", "Bagley is part of the computer, not just a tab: a launcher-style overlay and a bar segment in Quickshell, replies in mako, a co-pilot in zsh, the app on your phone, and an always-on runner for automations.");

  const phone = el("div", { class: "section" }, el("div", { class: "section-title", text: "Phone" }), el("div", { class: "status-line" }, el("span", { class: "spinner" }), "Asking Tailscale"));
  const runner = el("div", { class: "section" });
  panel.append(
    section("Hyprland desktop",
      keys([["SUPER+B", "Ask Bagley (overlay)"], ["SUPER+SHIFT+B", "What's this? (focused window)"], ["SUPER+ALT+B", "Explain the selection"]]),
      command("bagley desktop install", "Copies the Quickshell overlay and bar segment, the Hyprland binds, the mako style and the zsh plugin into place, then prints what to add to your configs."),
      command("bagley see \"what's this error?\"", "Screenshot of the focused window, its title, the highlighted text and the clipboard, sent to a vision model."),
      command("bagley status --follow --json", "One line per state change, for status bars."),
      command("bagley service install", "Runs Bagley as a systemd user service in your desktop session, so mako notifications and desktop control work without a terminal open."),
    ),
    section("Shell",
      command("source ~/.local/share/bagley/bagley.zsh", "Add to ~/.zshrc. Then type ?? find files over 1GB and press Enter: the command lands on your prompt without running."),
      command("bagley why", "Explains the last failed command from its output (kitty with remote control, or tmux)."),
    ),
    phone,
    runner,
    dataSection(ctx),
  );
  drawPhone(phone);
  drawRunner(runner, ctx);
}

async function drawPhone(box) {
  let ts;
  try {
    ts = await api.get("/api/tailscale");
  } catch {
    ts = { available: false, error: "This server can't read Tailscale." };
  }
  const title = el("div", { class: "section-title", text: "Phone" });
  if (!ts.available) {
    box.replaceChildren(title,
      el("p", { class: "help", text: `Tailscale is not available here (${ts.error || "not installed"}). Install it on this machine and your phone, sign both in to the same tailnet, then come back.` }),
      command("bagley tailscale", "Prints the steps once Tailscale runs."));
    return;
  }
  const peers = (ts.peers || []).filter((p) => /android|ios/i.test(p.os || ""));
  box.replaceChildren(title,
    el("p", { class: "help", style: "margin:0 0 10px", text: `This machine is ${ts.dns_name || ts.machine} on your tailnet. Open the address below in Chrome on your phone, then Install app from the menu.` }),
    ts.url ? command(ts.url) : null,
    el("div", { class: "list" },
      check(ts.serving, "Served over HTTPS", ts.serving ? "tailscale serve proxies to Bagley" : "Run the serve command below"),
      check(ts.host_allowed, "Host allowed", ts.host_allowed ? ts.env?.BAGLEY_ALLOWED_HOSTS : `Set BAGLEY_ALLOWED_HOSTS=${ts.env?.BAGLEY_ALLOWED_HOSTS || ""}`),
      check(ts.public_url_set, "Notification links", ts.public_url_set ? "Clicks open the app on the phone" : `Set BAGLEY_PUBLIC_URL=${ts.env?.BAGLEY_PUBLIC_URL || ""}`),
      check(ts.identity_check || ts.token ? true : null, "Who may use it", ts.identity_check ? "Only your Tailscale login (BAGLEY_TAILSCALE_USERS)" : ts.token ? "Access token required" : "Anyone on your tailnet: set BAGLEY_TAILSCALE_USERS to your login"),
      ...peers.map((p) => check(p.online, `${p.name} (${p.os})`, p.online ? "Online on your tailnet" : "Offline")),
    ),
    !ts.serving && ts.serve?.length ? command(ts.serve.join(" "), "Serve Bagley on your tailnet over HTTPS.") : null,
    !(ts.host_allowed && ts.public_url_set) ? command("bagley tailscale --apply", "Writes the allowed host and public URL to ~/.config/bagley/env. Restart Bagley after.") : null,
  );
}

async function drawRunner(box, ctx) {
  const url = el("input", { class: "input mono", value: value("runner_url") || "", spellcheck: "false", placeholder: "http://surface:8765" });
  url.addEventListener("change", () => savePrefs({ runner_url: url.value.trim() }).then(() => drawRunner(box, ctx)));
  const token = el("input", { class: "input mono", type: "password", autocomplete: "off", placeholder: value("has_runner_token") ? "•••••••• saved" : "The runner's BAGLEY_TOKEN" });
  token.addEventListener("change", () => savePrefs({ runner_token: token.value.trim() }).then(() => drawRunner(box, ctx)));
  const status = el("div", { class: "status-line", "aria-live": "polite" }, el("span", { class: "spinner" }), "Checking the runner");
  const result = el("div", { class: "help" });
  const push = el("button", { class: "btn btn-sm", type: "button" }, icon("upload", "icon-sm"), "Move all automations");
  const pull = el("button", { class: "btn btn-sm btn-ghost", type: "button" }, icon("download", "icon-sm"), "Copy the runner's here");
  push.addEventListener("click", async () => {
    push.disabled = true;
    try {
      const r = await api.post("/api/runner/push", { all: true });
      result.textContent = `Moved ${r.moved.length}${r.failed.length ? `, failed ${r.failed.length}: ${r.failed.map((f) => `${f.name} (${f.error})`).join("; ")}` : ""}.`;
      await ctx.onAutomationsChanged();
    } catch (err) {
      toast(err.message, { type: "error" });
    }
    push.disabled = false;
  });
  pull.addEventListener("click", async () => {
    pull.disabled = true;
    try {
      const r = await api.post("/api/runner/pull");
      result.textContent = `Copied ${r.added?.automations ?? 0} automations (paused here, for reference), skipped ${r.skipped?.automations ?? 0}.`;
      await ctx.onAutomationsChanged();
    } catch (err) {
      toast(err.message, { type: "error" });
    }
    pull.disabled = false;
  });
  box.replaceChildren(
    el("div", { class: "section-title", text: "Always-on runner" }),
    el("p", { class: "help", style: "margin:0 0 10px", text: "Run Bagley on an always-on machine (the Surface on the shelf) and move automations there, so briefings and watchers keep running while this laptop sleeps. Set it up there with bagley service install --runner." }),
    el("div", { class: "field-row" }, field("Runner URL", url), field("Runner token", token)),
    status,
    el("div", { class: "inline", style: "margin-top:8px;flex-wrap:wrap" }, push, pull),
    result,
  );
  try {
    const r = await api.get("/api/runner");
    push.disabled = pull.disabled = !r.reachable;
    if (!r.configured) status.replaceChildren(el("span", { class: "dot" }), "No runner set. Automations run here, while this machine is awake.");
    else if (r.reachable) status.replaceChildren(el("span", { class: "dot ok" }), `Runner online // v${r.version} // ${r.automations} automations // ${r.latency_ms}ms`);
    else status.replaceChildren(el("span", { class: "dot bad" }), `NO SIGNAL // ${r.error || "not reachable"}`);
  } catch (err) {
    status.replaceChildren(el("span", { class: "dot bad" }), err.message);
  }
}

function dataSection(ctx) {
  const file = el("input", { type: "file", accept: "application/json,.json", hidden: true });
  const result = el("div", { class: "help", style: "margin-top:8px" });
  const exportBtn = el("button", { class: "btn btn-sm", type: "button" }, icon("download", "icon-sm"), "Export");
  exportBtn.addEventListener("click", async () => {
    try {
      const data = await api.get("/api/export?parts=automations,memories,routines");
      const a = el("a", { href: URL.createObjectURL(new Blob([JSON.stringify(data, null, 2)], { type: "application/json" })), download: "bagley-export.json" });
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 1000);
    } catch (err) {
      toast(err.message, { type: "error" });
    }
  });
  const importBtn = el("button", { class: "btn btn-sm", type: "button", onclick: () => file.click() }, icon("upload", "icon-sm"), "Import");
  file.addEventListener("change", async () => {
    const chosen = file.files[0];
    file.value = "";
    if (!chosen) return;
    try {
      const res = await fetch("/api/import", { method: "POST", headers: { "Content-Type": "application/json" }, body: await chosen.text() });
      const data = await res.json();
      if (!res.ok) throw new Error(data.detail || `Import failed (${res.status})`);
      const added = Object.entries(data.added).map(([k, v]) => `${v} ${k}`).join(", ");
      result.textContent = `Added ${added}.${data.errors?.length ? ` Skipped ${data.errors.length}: ${data.errors.map((e) => `${e.name} (${e.error})`).join("; ")}` : ""}`;
      await ctx.onAutomationsChanged();
      await ctx.onMemoriesChanged();
    } catch (err) {
      toast(err.message, { type: "error" });
    }
  });
  return section("Your data",
    el("p", { class: "help", style: "margin:0 0 10px", text: "Automations, memories and routines as one JSON file, to move between machines. Chats stay where they are." }),
    el("div", { class: "inline" }, exportBtn, importBtn, file), result);
}
