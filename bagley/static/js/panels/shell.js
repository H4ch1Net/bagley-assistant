// Shell: the zsh co-pilot. `?? request` puts a command on the prompt, `bagley why` explains the
// last failure. This tab shows what Bagley knows about the machine, the setup and a try-it box.

import { api } from "../api.js";
import { toast } from "../ui.js";
import { copyText, el, icon } from "../util.js";
import { header, section } from "../settings-kit.js";

const SETUP = [
  ["Install the plugin and desktop files", "bagley desktop install"],
  ["Load it from ~/.zshrc, after your plugin manager", "source ~/.local/share/bagley/zsh/bagley.zsh"],
  ["kitty.conf, so bagley why can read the failed output", "shell_integration enabled\nallow_remote_control socket-only\nlisten_on unix:/tmp/kitty"],
];

function snippet(label, code) {
  const copy = el("button", { class: "icon-btn icon-btn-sm", type: "button", title: "Copy", "aria-label": `Copy: ${label}` }, icon("copy", "icon-sm"));
  copy.addEventListener("click", async () => {
    await copyText(code);
    toast("Copied");
  });
  return el("div", { class: "field" }, el("span", { class: "field-label", text: label }),
    el("div", { class: "snippet" }, el("pre", { class: "term", text: code }), copy));
}

export function render(panel) {
  const facts = el("div", { class: "help" }, "Reading this machine…");
  api.get("/api/shell/facts").then((f) => {
    facts.replaceChildren(
      el("div", { class: "stat" }, el("span", { class: "k", text: "System" }), el("span", { class: "v", text: [f.os, f.kernel].filter(Boolean).join(" · ") })),
      el("div", { class: "stat" }, el("span", { class: "k", text: "Shell" }), el("span", { class: "v", text: f.shell || "--" })),
      el("div", { class: "stat" }, el("span", { class: "k", text: "Packages" }), el("span", { class: "v", text: (f.package_managers || []).join(", ") || "--" })),
      el("div", { class: "stat" }, el("span", { class: "k", text: "Tools" }), el("span", { class: "v", title: (f.tools || []).join(", "), text: (f.tools || []).join(", ") || "--" })),
    );
  }).catch(() => facts.replaceChildren("The shell co-pilot is not available on this server."));

  const ask = el("input", { class: "input mono", placeholder: "e.g. find files over 1GB in my home folder", "aria-label": "What you want to do", spellcheck: "false" });
  const out = el("div", { class: "shell-out", "aria-live": "polite" });
  const go = el("button", { class: "btn btn-primary", type: "button" }, icon("terminal", "icon-sm"), "Suggest");
  const run = async () => {
    if (!ask.value.trim()) return ask.focus();
    go.disabled = true;
    out.replaceChildren(el("div", { class: "status-line" }, el("span", { class: "spinner" }), "Thinking"));
    try {
      const res = await api.post("/api/shell/suggest", { request: ask.value.trim(), shell: "zsh" });
      out.replaceChildren(
        res.danger ? el("div", { class: "notice error-text", text: `CAREFUL // ${res.danger}` }) : null,
        snippet("Command (not run: paste it into your terminal)", res.command),
        res.explanation ? el("p", { class: "help", text: res.explanation }) : null,
        el("div", { class: "help", text: [res.machine, res.model].filter(Boolean).join(" // ") }),
      );
    } catch (err) {
      out.replaceChildren(el("div", { class: "status-line error-text", text: err.message }));
    }
    go.disabled = false;
  };
  go.addEventListener("click", run);
  ask.addEventListener("keydown", (e) => {
    if (e.key === "Enter") run();
  });

  header(panel, "Shell", [
    "In zsh, type ", el("code", { text: "?? find files over 1GB" }), " and Bagley puts a command on your prompt without running it. After a failure, ",
    el("code", { text: "bagley why" }), " explains what went wrong and how to fix it. Both use the light route, so they stay on this machine's model.",
  ]);
  panel.append(
    section("Setup", ...SETUP.map(([label, code]) => snippet(label, code))),
    section("This machine", facts),
    section("Try it", el("div", { class: "inline", style: "margin-bottom:10px" }, ask, go), out),
  );
}
