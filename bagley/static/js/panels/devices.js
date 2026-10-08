// Desktop & phone: Bagley on the Hyprland desktop (overlay, bar segment, hotkeys, shell),
// on the phone over Tailscale, and the always-on runner.

import { api } from "../api.js";
import { state } from "../state.js";
import { toast } from "../ui.js";
import { copyText, el, icon } from "../util.js";
import { field, header, notice, savePrefs, section, value } from "../settings-kit.js";

function command(text, note) {
  const copy = el("button", { class: "icon-btn icon-btn-sm", type: "button", "aria-label": `Copy ${text}`, title: "Copy", onclick: () => copyText(text).then(() => toast("Copied")) }, icon("copy", "icon-sm"));
  return el("div", { style: "margin-bottom:8px" }, el("div", { class: "cmd" }, el("code", { text }), copy), note ? el("div", { class: "help", style: "margin-top:3px", text: note }) : null);
}

function keys(rows) {
  return el("div", { class: "shortcut-list", style: "margin-bottom:12px" }, ...rows.flatMap(([combo, what]) => [el("span", { text: what }), el("span", {}, ...combo.split("+").map((k) => el("kbd", { text: k })))]));
}

export function render(panel, ctx) {
  header(panel, "Desktop & phone", "Bagley is part of the computer, not just a tab: a launcher-style overlay and a bar segment in Quickshell, replies in mako, a co-pilot in zsh, the app on your phone, and an always-on runner for automations.");

  panel.append(
    section("Hyprland desktop",
      keys([["SUPER+B", "Ask Bagley (overlay)"], ["SUPER+SHIFT+B", "What's this? (focused window)"], ["SUPER+ALT+B", "Explain the selection"]]),
      command("bagley desktop install", "Copies the Quickshell overlay and bar segment, the Hyprland binds, the mako style and the zsh plugin into place, then prints what to add to your configs."),
      command("bagley see \"what's this error?\"", "Screenshot of the focused window, its title, the highlighted text and the clipboard, sent to a vision model."),
      command("bagley status --follow --json", "One line per state change, for status bars."),
    ),
    section("Shell",
      command("source ~/.local/share/bagley/bagley.zsh", "Add to ~/.zshrc. Then type ?? find files over 1GB and press Enter: the command lands on your prompt without running."),
      command("bagley why", "Explains the last failed command from its output (kitty with remote control, or tmux)."),
    ),
  );

  // Phone and runner details come from the server when it knows them.
  const phone = el("div", { class: "section" });
  const runner = el("div", { class: "section" });
  panel.append(phone, runner);

  const publicUrl = state.info?.public_url || "";
  phone.replaceChildren(
    el("div", { class: "section-title", text: "Phone" }),
    el("ol", { class: "steps" },
      el("li", {}, "Install Tailscale on this machine and your phone, signed in to the same tailnet."),
      el("li", {}, "Serve Bagley over HTTPS on your tailnet:", command("bagley tailscale --apply"), "It sets up tailscale serve and adds the allowed host. Restart Bagley after."),
      el("li", {}, publicUrl ? `Open ${publicUrl} in Chrome on the phone, then Install app from the menu.` : "Open the https://<machine>.<tailnet>.ts.net address it prints in Chrome on the phone, then Install app from the menu."),
      el("li", {}, "For push notifications, set up ntfy under Notifications."),
    ),
  );

  const url = el("input", { class: "input mono", value: value("runner_url") || "", spellcheck: "false", placeholder: "http://surface:8765" });
  url.addEventListener("change", () => savePrefs({ runner_url: url.value.trim() }).then(() => drawRunner()));
  const token = el("input", { class: "input mono", type: "password", autocomplete: "off", placeholder: value("has_runner_token") ? "•••••••• saved" : "The runner's BAGLEY_TOKEN" });
  token.addEventListener("change", () => savePrefs({ runner_token: token.value.trim() }).then(() => drawRunner()));
  const status = el("div", { class: "status-line", "aria-live": "polite" });
  const push = el("button", { class: "btn btn-sm", type: "button" }, icon("upload", "icon-sm"), "Move all automations");
  push.addEventListener("click", async () => {
    push.disabled = true;
    try {
      const result = await api.post("/api/runner/push", { all: true });
      toast(`Moved ${result.moved ?? result.count ?? 0} automations to the runner`);
      await ctx.onAutomationsChanged();
    } catch (err) {
      toast(err.message, { type: "error" });
    }
    push.disabled = false;
  });
  const drawRunner = async () => {
    status.replaceChildren(el("span", { class: "spinner" }), "Checking the runner…");
    try {
      const r = await api.get("/api/runner");
      if (!r.configured) status.replaceChildren(el("span", { class: "dot" }), "No runner set. Automations run here, while this machine is awake.");
      else if (r.reachable) status.replaceChildren(el("span", { class: "dot ok" }), `Runner online · v${r.version} · ${r.automations} automations · ${r.latency_ms}ms`);
      else status.replaceChildren(el("span", { class: "dot bad" }), r.error || "Runner not reachable");
    } catch {
      status.replaceChildren(el("span", { class: "dot" }), "This server can't check runners yet.");
    }
  };
  runner.replaceChildren(
    el("div", { class: "section-title", text: "Always-on runner" }),
    el("p", { class: "help", style: "margin:0 0 10px", text: "Run Bagley on an always-on machine (a Surface on the shelf) and move automations there, so briefings and watchers keep running while this laptop sleeps. Install it there with bagley service install --runner." }),
    el("div", { class: "field-row" }, field("Runner URL", url), field("Runner token", token)),
    el("div", { class: "inline" }, push),
    status,
    value("runner_url") ? null : notice("Point this laptop's CLI and overlay at the runner with BAGLEY_URL and BAGLEY_TOKEN if you want them to use it too."),
  );
  drawRunner();
}
