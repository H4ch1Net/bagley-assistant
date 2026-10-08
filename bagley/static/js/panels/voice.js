// Voice: how Bagley speaks (browser voices, a local Piper voice or ElevenLabs) and listens
// (the browser's recognition or whisper.cpp on this machine), plus the desktop wake word.

import { api } from "../api.js";
import { setUi, state } from "../state.js";
import { toast } from "../ui.js";
import { el, icon } from "../util.js";
import { voice } from "../voice.js";
import { field, header, notice, savePrefs, section, select, toggleRow, value } from "../settings-kit.js";

export function render(panel, ctx) {
  header(panel, "Voice", "Bagley can speak in character with a British voice, offline with Piper or through ElevenLabs, and listen with whisper.cpp on this machine. The browser's own voices work too.");
  const status = el("div");
  const engines = el("div");
  panel.append(section("", toggleRow("Read replies aloud", "Bagley speaks each reply when it finishes. The avatar pulses with each word.", state.ui.speak, (v) => setUi("speak", v))), engines, status);

  const drawEngines = (info) => {
    const tts = select([["browser", "Browser voices"], ["piper", "Piper (local, offline)"], ["elevenlabs", "ElevenLabs (online)"]], value("tts_engine") || "browser", async (v) => {
      if (await savePrefs({ tts_engine: v })) ctx.refresh();
    });
    const stt = select([["browser", "Browser speech recognition"], ["whisper", "whisper.cpp (local, offline)"]], value("stt_engine") || "browser", async (v) => {
      if (await savePrefs({ stt_engine: v })) ctx.refresh();
    });
    const piperVoices = info?.piper?.voices || [];
    const piper = el("input", { class: "input mono", value: value("piper_voice") || "", placeholder: "~/.local/share/piper/en_GB-alan-medium.onnx", list: "piper-voices", spellcheck: "false" });
    piper.addEventListener("change", () => savePrefs({ piper_voice: piper.value.trim() }));
    const piperList = el("datalist", { id: "piper-voices" }, ...piperVoices.map((v) => el("option", { value: v.path || v })));
    const elKey = el("input", { class: "input mono", type: "password", autocomplete: "off", placeholder: value("has_elevenlabs_key") ? "•••••••• saved" : "xi-api-key" });
    elKey.addEventListener("change", () => savePrefs({ elevenlabs_key: elKey.value.trim() }).then(() => ctx.refresh()));
    const elVoice = el("input", { class: "input mono", value: value("elevenlabs_voice") || "", placeholder: "Voice ID", spellcheck: "false" });
    elVoice.addEventListener("change", () => savePrefs({ elevenlabs_voice: elVoice.value.trim() }));
    const whisper = el("input", { class: "input mono", value: value("whisper_model") || "", placeholder: "~/.local/share/whisper/ggml-base.en.bin", spellcheck: "false", list: "whisper-models" });
    const whisperList = el("datalist", { id: "whisper-models" }, ...(info?.whisper?.models || []).map((m) => el("option", { value: m.path || m })));
    whisper.addEventListener("change", () => savePrefs({ whisper_model: whisper.value.trim() }));
    const wake = el("input", { class: "input", value: value("wake_word") || "bagley", maxlength: 40 });
    wake.addEventListener("change", () => savePrefs({ wake_word: wake.value.trim() || "bagley" }));
    const ttsEngine = value("tts_engine") || "browser";
    const test = el("button", { class: "btn btn-sm", type: "button", onclick: () => document.dispatchEvent(new CustomEvent("bagley:speak", { detail: "Hello. I'm Bagley. Your systems are, against all odds, still running." })) }, icon("volume-2", "icon-sm"), "Test voice");
    engines.replaceChildren(
      section("Speaking",
        field("Engine", tts, { id: "pref-tts" }),
        ttsEngine === "piper" ? el("div", {}, field("Piper voice", piper, { help: "A British voice suits him: en_GB-alan-medium or en_GB-northern_english_male-medium from huggingface.co/rhasspy/piper-voices." }), piperList,
          !piperVoices.length && info?.piper?.recommended?.length ? el("ul", { class: "help", style: "margin:0 0 12px;padding-left:1.2em" }, ...info.piper.recommended.map((r) => el("li", {}, `${r.name}: ${r.description} `, el("a", { href: r.model_url, target: "_blank", rel: "noopener", text: "model" }), " · ", el("a", { href: r.config_url, target: "_blank", rel: "noopener", text: "config" })))) : null) : null,
        ttsEngine === "elevenlabs" ? el("div", { class: "field-row" }, field("API key", elKey, { help: "Text you have read aloud is sent to ElevenLabs." }), field("Voice", elVoice, { help: "Pick a refined British male voice in your ElevenLabs library." })) : null,
        ttsEngine === "browser" ? browserVoice(ctx) : null,
        el("div", { class: "inline" }, test),
      ),
      section("Listening",
        field("Engine", stt, { id: "pref-stt", help: "The browser's recognition sends audio to its vendor in Chrome and Edge. whisper.cpp keeps it on this machine." }),
        value("stt_engine") === "whisper" ? el("div", {}, field("whisper.cpp model", whisper, { help: "A ggml model file, e.g. ggml-base.en.bin or ggml-small.bin." }), whisperList) : null,
        field("Wake word", wake, { help: "For the desktop listener: run bagley listen (or its systemd unit) and say \"Bagley, ...\"." }),
      ),
    );
  };
  drawEngines(null);
  api.get("/api/voice/status").then((info) => {
    drawEngines(info);
    const items = [];
    if (info.piper) items.push(["Piper", info.piper.binary ? `${info.piper.binary}${info.piper.voice ? ` · ${info.piper.voice}` : " · no voice set"}` : "not installed", Boolean(info.piper.binary && info.piper.voice)]);
    if (info.whisper) items.push(["whisper.cpp", info.whisper.binary ? `${info.whisper.binary}${info.whisper.model ? ` · ${info.whisper.model}` : " · no model set"}` : "not installed", Boolean(info.whisper.binary && info.whisper.model)]);
    items.push(["ffmpeg", info.ffmpeg ? "found" : "not installed", Boolean(info.ffmpeg)]);
    items.push(["ElevenLabs", info.elevenlabs?.configured ? "key saved" : "not set up", Boolean(info.elevenlabs?.configured)]);
    const problems = (info.problems || []).map((p) => el("div", { class: "term-row" }, el("span", { class: "warn", text: "[WARN] " }), p));
    status.replaceChildren(section("On this machine", el("div", { class: "list" }, ...items.map(([name, text, ok]) => el("div", { class: "list-item", style: "align-items:center" },
      el("span", { class: `dot ${ok ? "ok" : ""}` }), el("div", { class: "grow" }, el("div", { class: "name", text: name }), el("div", { class: "desc", text: text || "--N/A--" }))))),
      problems.length ? el("pre", { class: "term", style: "margin-top:10px;white-space:pre-wrap" }, ...problems) : null));
  }).catch(() => {
    status.replaceChildren(notice("Local voice engines are not available on this server version."));
  });
}

function browserVoice(ctx) {
  if (!voice.canSpeakBrowser) return notice("This browser doesn't support speech synthesis.");
  const voiceSelect = el("select", { class: "select" });
  const fill = () => {
    const voices = voice.voices();
    const sorted = [...voices].sort((a, b) => Number(b.lang.startsWith("en-GB")) - Number(a.lang.startsWith("en-GB")) || a.name.localeCompare(b.name));
    voiceSelect.replaceChildren(el("option", { value: "", text: "System default" }),
      ...sorted.map((v) => el("option", { value: v.voiceURI, text: `${v.name} (${v.lang})${v.localService ? "" : " · online"}` })));
    voiceSelect.value = state.ui.voice || "";
  };
  fill();
  ctx.fillVoices = fill;
  voiceSelect.addEventListener("change", () => setUi("voice", voiceSelect.value));
  const rate = el("input", { class: "range", type: "range", min: 0.6, max: 1.6, step: 0.1, value: state.ui.rate, "aria-label": "Speaking rate" });
  const rateOut = el("output", { text: `${Number(state.ui.rate).toFixed(1)}×` });
  rate.addEventListener("input", () => { rateOut.textContent = `${Number(rate.value).toFixed(1)}×`; setUi("rate", Number(rate.value)); });
  return el("div", { class: "field-row" },
    field("Voice", voiceSelect, { id: "pref-voice", help: "British (en-GB) voices are listed first." }),
    field("Speed", el("div", { class: "range-row" }, rate, rateOut)),
  );
}

export function speakError(err) {
  toast(`Couldn't speak: ${err.message}`, { type: "error" });
}
