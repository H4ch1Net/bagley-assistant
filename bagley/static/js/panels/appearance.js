// Appearance: theme, presets, every colour token, the accent, type size and the ctOS chrome.

import { setUi, state } from "../state.js";
import { toast } from "../ui.js";
import { copyText, el, icon } from "../util.js";
import {
  ACCENTS, appearance, contrast, currentTheme, DEFAULTS, exportAppearance, importAppearance, PRESETS,
  resetToken, setToken, TOKENS, tokenValue, updateAppearance, usePreset,
} from "../theme.js"; // prettier-ignore
import { header, section, toggleRow } from "../settings-kit.js";

const HEX = /^#[0-9a-f]{6}$/i;

export function render(panel, ctx) {
  const look = appearance();
  const theme = currentTheme();
  const themes = [["system", "monitor", "System"], ["dark", "moon", "Dark"], ["light", "sun", "Light"]];
  const seg = el("div", { class: "segmented", role: "group", "aria-label": "Theme" },
    ...themes.map(([id, ic, label]) => el("button", {
      type: "button", "aria-pressed": String(state.ui.theme === id),
      onclick: () => { setUi("theme", id); ctx.refresh(); },
    }, icon(ic, "icon-sm"), label)),
  );

  const presets = el("div", { class: "preset-grid", role: "group", "aria-label": "Presets" },
    ...PRESETS.map((p) => {
      const colors = { ...DEFAULTS[theme], ...p[theme] };
      return el("button", {
        class: "preset", type: "button", "aria-pressed": String(look.preset === p.id), title: p.note,
        style: `--pv-bg:${colors["c-ground"]};--pv-text:${colors["c-white"]}`,
        onclick: () => { usePreset(p.id); ctx.refresh(); },
      },
      el("span", { class: "pv-name", text: p.name }),
      el("span", { class: "pv-bar", "aria-hidden": "true" }, ...["c-raised", "c-muted", "c-body", "c-chrome", "c-accent", "c-ok", "c-err"].map((k) => el("i", { style: `background:${colors[k]}` }))),
      el("span", { style: `font-size:.72rem;color:${colors["c-muted"]}`, text: p.note }));
    }),
  );

  const accentNow = tokenValue("c-accent");
  const custom = el("input", { type: "color", class: "swatch-input", value: accentNow, "aria-label": "Custom accent colour", title: "Custom accent" });
  custom.addEventListener("change", () => { setToken("c-accent", custom.value); ctx.refresh(); });
  const swatches = el("div", { class: "swatches", role: "group", "aria-label": "Accent colour" },
    ...ACCENTS.map((a) => el("button", {
      class: "swatch", type: "button", style: `--sw:${a.color}`, title: a.name, "aria-label": a.name,
      "aria-pressed": String(accentNow === a.color),
      onclick: () => {
        setToken("c-accent", a.color);
        if (document.getElementById("follow-avatar")?.checked) setToken("c-avatar", a.color);
        ctx.refresh();
      },
    })),
    custom,
  );
  const follow = el("label", { class: "inline help", style: "margin-top:8px;gap:6px" },
    el("input", { type: "checkbox", id: "follow-avatar", checked: tokenValue("c-avatar") === accentNow }), "Avatar uses the accent too");

  const rows = TOKENS.map(({ key, label, help }) => {
    const current = tokenValue(key);
    const picker = el("input", { type: "color", class: "swatch-input", value: current, "aria-label": `${label} colour` });
    const hex = el("input", { class: "input mono hex", value: current, maxlength: 7, spellcheck: "false", "aria-label": `${label} hex value` });
    picker.addEventListener("input", () => { hex.value = picker.value; setToken(key, picker.value); });
    picker.addEventListener("change", () => ctx.refresh());
    hex.addEventListener("change", () => {
      const v = hex.value.trim().startsWith("#") ? hex.value.trim() : `#${hex.value.trim()}`;
      if (!HEX.test(v)) {
        toast("Use a six-digit hex colour like #00fa9a.", { type: "error" });
        hex.value = current;
        return;
      }
      setToken(key, v);
      ctx.refresh();
    });
    const changed = Boolean(look.colors[theme][key]);
    return el("div", { class: `color-row${changed ? " changed" : ""}` },
      picker,
      el("div", { class: "text" }, el("strong", { text: label }), el("span", { text: help })),
      hex,
      el("button", { class: "icon-btn icon-btn-sm", type: "button", title: "Reset to the preset", "aria-label": `Reset ${label}`, disabled: !changed, onclick: () => { resetToken(key); ctx.refresh(); } }, icon("undo-2", "icon-sm")),
    );
  });

  // Readability checks against the ground.
  const ground = tokenValue("c-ground");
  const checks = [["Body text", "c-body", 4.5], ["Muted text", "c-muted", 4.5], ["Bright text", "c-white", 7], ["Success", "c-ok", 3], ["Error", "c-err", 3]].map(([label, key, min]) => {
    const ratio = contrast(tokenValue(key), ground);
    return el("span", { class: `contrast${ratio < min ? " bad" : ""}`, title: `Needs ${min}:1` }, `${label} ${ratio.toFixed(1)}:1${ratio < min ? " LOW" : ""}`);
  });
  const inkRatio = contrast(tokenValue("c-ground"), tokenValue("c-accent"));
  checks.push(el("span", { class: `contrast${inkRatio < 4.5 ? " bad" : ""}`, title: "Text on accent buttons" }, `Button text ${inkRatio.toFixed(1)}:1${inkRatio < 4.5 ? " LOW" : ""}`));

  const sizes = [[12, "XS"], [13, "S"], [14, "M"], [15, "L"], [16, "XL"], [17, "XXL"]];
  const size = el("div", { class: "segmented", role: "group", "aria-label": "Text size" },
    ...sizes.map(([px, label]) => el("button", { type: "button", "aria-pressed": String(look.scale === px), title: `${px}px`, onclick: () => { updateAppearance({ scale: px }); ctx.refresh(); } }, label)));
  const reading = el("div", { class: "segmented", role: "group", "aria-label": "Message font" },
    ...[["mono", "Mono"], ["sans", "Sans"]].map(([id, label]) => el("button", { type: "button", "aria-pressed": String(look.reading === id), onclick: () => { updateAppearance({ reading: id }); ctx.refresh(); } }, label)));

  const importBox = el("textarea", { class: "textarea mono", rows: 4, placeholder: "Paste a theme exported from Bagley…", "aria-label": "Theme to import" });

  header(panel, "Appearance", "The ctOS look: gray on black, green means OK. Every colour can be changed; presets and your edits are kept per theme and sync to your other devices.");
  panel.append(
    section("Theme", seg, el("div", { class: "help", style: "margin-top:8px", text: "ctOS has no light theme; Light is derived from the same grays with darker state colours so text stays readable." })),
    section("Presets", presets),
    section("Accent", swatches, follow),
    section(`Colours // ${theme}`,
      el("div", { class: "inline", style: "flex-wrap:wrap;gap:6px 14px;margin-bottom:12px" }, ...checks),
      el("div", { class: "color-grid" }, ...rows),
      el("div", { class: "inline", style: "margin-top:12px;flex-wrap:wrap" },
        el("button", { class: "btn btn-sm", type: "button", onclick: () => { usePreset(look.preset === "custom" ? "ctos" : look.preset); ctx.refresh(); } }, icon("undo-2", "icon-sm"), "Reset colours"),
        el("button", { class: "btn btn-sm btn-ghost", type: "button", onclick: () => { usePreset("ctos"); ctx.refresh(); } }, "Back to ctOS"),
      ),
    ),
    section("Type",
      el("div", { class: "field-row" },
        el("div", { class: "field" }, el("span", { class: "field-label", text: "Text size" }), size),
        el("div", { class: "field" }, el("span", { class: "field-label", text: "Message font" }), reading, el("div", { class: "help", text: "Mono is JetBrains Mono, as everywhere in ctOS. Sans is easier on long replies." })),
      ),
    ),
    section("Chrome",
      toggleRow("Corner brackets", "White L-corners around messages, tool cards and bar segments.", look.brackets, (v) => updateAppearance({ brackets: v })),
      toggleRow("Background grid", "The faint 20px grid of the ctOS lock screen.", look.grid, (v) => updateAppearance({ grid: v })),
      toggleRow("Uppercase labels", "Machine-like readouts: labels, buttons and states in capitals.", look.caps, (v) => updateAppearance({ caps: v })),
      toggleRow("Reduce motion", "Calmer avatar and no animated transitions, like CTOS_BAR_ANIMATIONS=reduced. Your system setting is respected either way.", state.ui.reduceMotion, (v) => setUi("reduceMotion", v)),
    ),
    section("Share",
      el("div", { class: "inline", style: "flex-wrap:wrap" },
        el("button", { class: "btn btn-sm", type: "button", onclick: () => copyText(exportAppearance()).then(() => toast("Theme copied as JSON")) }, icon("copy", "icon-sm"), "Copy theme"),
      ),
      el("div", { class: "field", style: "margin-top:10px" }, importBox),
      el("button", { class: "btn btn-sm", type: "button", onclick: () => {
        try {
          importAppearance(importBox.value);
          toast("Theme imported");
          ctx.refresh();
        } catch (err) {
          toast(err.message, { type: "error" });
        }
      } }, icon("download", "icon-sm"), "Import theme"),
    ),
  );
}
