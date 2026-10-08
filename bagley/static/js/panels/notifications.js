// Notifications beyond this window: the Linux desktop (mako) and the phone (ntfy).

import { api } from "../api.js";
import { setUi, state } from "../state.js";
import { toast } from "../ui.js";
import { el, icon } from "../util.js";
import { field, header, savePrefs, section, select, toggleRow, value } from "../settings-kit.js";

export function render(panel, ctx) {
  const url = el("input", { class: "input mono", value: value("ntfy_url") || "", spellcheck: "false", placeholder: "https://ntfy.sh/bagley-7f3a9c2e" });
  url.addEventListener("change", () => savePrefs({ ntfy_url: url.value.trim() }));
  const token = el("input", { class: "input mono", type: "password", autocomplete: "off", placeholder: value("has_ntfy_token") ? "•••••••• saved" : "Optional access token" });
  token.addEventListener("change", () => savePrefs({ ntfy_token: token.value.trim() }).then(() => ctx.refresh()));
  const level = select([["all", "Everything"], ["important", "Important: automation results, briefings"], ["critical", "Critical only: failures, security"]], value("ntfy_level") || "important", (v) => savePrefs({ ntfy_level: v }));
  const status = el("div", { class: "status-line", "aria-live": "polite" });
  const test = el("button", { class: "btn btn-sm", type: "button" }, icon("bell", "icon-sm"), "Send a test");
  test.addEventListener("click", async () => {
    status.replaceChildren(el("span", { class: "spinner" }), "Sending…");
    try {
      const result = await api.post("/api/notify/test", { targets: ["desktop", "phone"] });
      const results = Object.entries(result.results || {});
      const text = results.map(([target, r]) => `${target.toUpperCase()} ${r.ok ? "OK" : "FAILED"}${r.detail ? ` (${r.detail})` : ""}`);
      text.push(`${result.windows ?? 0} window${result.windows === 1 ? "" : "s"}`);
      status.replaceChildren(el("span", { class: `dot ${results.every(([, r]) => r.ok) ? "ok" : "bad"}` }), text.join(" · "));
    } catch (err) {
      status.replaceChildren(el("span", { class: "dot bad" }), err.message);
    }
  });
  const random = () => `bagley-${[...crypto.getRandomValues(new Uint8Array(6))].map((b) => b.toString(16).padStart(2, "0")).join("")}`;

  header(panel, "Notifications", "Reminders, automation results, morning briefings and approvals can reach you beyond this window: as mako notifications on your Hyprland desktop and as push notifications on your phone.");
  panel.append(
    section("Desktop",
      toggleRow("Desktop notifications", "Send notifications through notify-send, which mako shows top right. Approvals from the overlay or the shell get Allow and Deny actions there. Needs Bagley to run in your desktop session (the systemd user service does).", value("desktop_notifications"), (v) => savePrefs({ desktop_notifications: v })),
      toggleRow("Browser notifications", "Also show them as browser notifications while this tab is in the background.", state.ui.desktopNotify, async (v) => {
        if (v && "Notification" in window && Notification.permission !== "granted" && (await Notification.requestPermission()) !== "granted") {
          toast("Notifications are blocked for this site in your browser settings.", { type: "error" });
          ctx.refresh();
          return;
        }
        setUi("desktopNotify", v);
      }),
    ),
    section("Phone (ntfy)",
      el("p", { class: "help", style: "margin:0 0 10px" }, "Install the ntfy app on your phone and subscribe to a private topic. Anyone who knows the topic name can read it on ntfy.sh, so make it long and random, or run your own ntfy server on your tailnet."),
      el("div", { class: "field-row" },
        field("Topic URL", el("div", { class: "inline" }, url, el("button", { class: "btn btn-sm", type: "button", title: "Make a random private topic", onclick: () => { url.value = `https://ntfy.sh/${random()}`; url.dispatchEvent(new Event("change")); } }, "Random")), { id: "pref-ntfy" }),
        field("Access token", token, { help: "For protected topics or your own server." }),
      ),
      field("Send to the phone", level),
      el("div", { class: "inline" }, test),
      status,
    ),
  );
}
