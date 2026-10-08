// General: how Bagley talks, the mode new chats start in, and the work profile.

import { setUi, state } from "../state.js";
import { toast } from "../ui.js";
import { debounce, el, icon } from "../util.js";
import { ENV_NAMES, field, header, locked, savePrefs, section, stringList, toggleRow, value } from "../settings-kit.js";

export function render(panel, ctx) {
  const personas = state.prefs.personas;
  const grid = el("div", { class: "choice-grid", role: "radiogroup", "aria-label": "Personality" },
    ...Object.entries(personas).map(([id, p]) => {
      const input = el("input", { type: "radio", name: "persona", value: id, checked: value("persona") === id, disabled: locked("persona") });
      input.addEventListener("change", () => savePrefs({ persona: id }));
      return el("label", { class: "choice" }, input, el("strong", { text: p.label }), el("span", { text: p.description }));
    }),
  );
  const instructions = el("textarea", { class: "textarea", rows: 5, maxlength: 4000, placeholder: "e.g. Call me Sam. I live in Leeds. Prefer short answers with examples in Python." });
  instructions.value = value("custom_instructions") || "";
  const counter = el("span", { class: "help" });
  const updateCount = () => (counter.textContent = `${String(instructions.value.length).padStart(4, "0")} / 4000`);
  updateCount();
  instructions.addEventListener("input", updateCount);
  instructions.addEventListener("input", debounce(() => savePrefs({ custom_instructions: instructions.value }), 600));

  const modes = state.prefs.modes || [];
  const modeGrid = el("div", { class: "choice-grid", role: "radiogroup", "aria-label": "Mode for new chats" },
    ...modes.map((m) => {
      const input = el("input", { type: "radio", name: "default-mode", value: m.id, checked: (value("default_mode") || "default") === m.id });
      input.addEventListener("change", () => savePrefs({ default_mode: m.id }));
      return el("label", { class: "choice" }, input, el("strong", { text: `${m.code} · ${m.label}` }), el("span", { text: m.description }));
    }),
  );

  const workName = el("input", { class: "input", value: value("work_name") || "", maxlength: 40, placeholder: "e.g. ATS" });
  workName.addEventListener("change", () => savePrefs({ work_name: workName.value.trim() || "Work" }));

  header(panel, "General", "How Bagley talks to you, and what kind of help a new chat starts with.");
  panel.append(
    section("",
      el("div", { class: "field" }, el("span", { class: "field-label", text: "Personality" }, locked("persona") ? el("span", { class: "lock" }, icon("lock", "icon-xs"), ENV_NAMES.persona) : null), grid),
      field("Custom instructions", instructions, { id: "custom-instructions", help: "Added to every conversation. Bagley also keeps its own memory, see the Memory tab." }),
      counter,
    ),
    section("Modes",
      el("p", { class: "help", style: "margin:0 0 10px", text: "A mode adds instructions and loads the tools it needs. Switch the open chat's mode with the cells in the top bar or Ctrl+Shift+M." }),
      modeGrid,
    ),
    section("Work profile",
      el("div", { class: "field-row" },
        field("Company or team", workName, { help: "Shown as the work mode's name and used in client summaries." }),
        el("div", { class: "field" },
          el("span", { class: "field-label", text: "Client summary languages" }),
          stringList({
            items: [...(value("work_languages") || [])],
            placeholder: "Add a language, e.g. German",
            mono: false,
            onChange: (items) => {
              if (!items.length) {
                toast("Keep at least one language.", { type: "error" });
                return false;
              }
              return savePrefs({ work_languages: items });
            },
          }),
        ),
      ),
    ),
    section("Behaviour",
      toggleRow("Smart titles", "Ask the model for a short title after the first reply.", value("smart_titles"), (v) => savePrefs({ smart_titles: v })),
      toggleRow("Browser notifications", "Reminders, automations and notify requests appear as browser notifications while this tab is in the background. Desktop (mako) and phone notifications are under Notifications.", state.ui.desktopNotify, async (v) => {
        if (v && "Notification" in window && Notification.permission !== "granted") {
          const result = await Notification.requestPermission();
          if (result !== "granted") {
            toast("Notifications are blocked for this site in your browser settings.", { type: "error" });
            ctx.refresh();
            return;
          }
        }
        setUi("desktopNotify", v);
      }),
    ),
  );
}
