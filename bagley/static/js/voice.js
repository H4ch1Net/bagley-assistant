// Speech out and in. Bagley speaks with the browser's voices, or with the server's engine (a
// local Piper voice or ElevenLabs) when one is chosen in Settings → Voice. Dictation uses the
// browser's recognition, or records audio for whisper.cpp on the server.

import { plainText } from "./markdown.js";
import { state } from "./state.js";

const synth = window.speechSynthesis;
const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;
const canRecord = Boolean(navigator.mediaDevices?.getUserMedia && window.MediaRecorder);

const serverTts = () => ["piper", "elevenlabs"].includes(state.prefs?.values.tts_engine);
const serverStt = () => state.prefs?.values.stt_engine === "whisper" && canRecord;

export const voice = {
  canSpeakBrowser: Boolean(synth),
  get canSpeak() {
    return Boolean(synth) || serverTts();
  },
  get canListen() {
    return Boolean(Recognition) || serverStt();
  },
  speaking: false,
  audio: null,

  voices() {
    return synth ? synth.getVoices() : [];
  },

  onVoicesChanged(fn) {
    if (synth) synth.addEventListener?.("voiceschanged", fn);
  },

  /** Speak markdown text. Hooks: onStart, onWord, onEnd. */
  speak(markdown, hooks = {}) {
    if (serverTts()) return this.speakServer(markdown, hooks);
    const { onStart, onWord, onEnd } = hooks;
    if (!synth) return;
    this.stop();
    const text = plainText(markdown);
    if (!text) return;
    // Chrome cuts off very long utterances; speak sentence groups one after another.
    const chunks = text.match(/[^.!?\n]+[.!?]*\s*/g)?.reduce((acc, s) => {
      if (acc.length && (acc[acc.length - 1] + s).length < 220) acc[acc.length - 1] += s;
      else acc.push(s);
      return acc;
    }, []) || [text];
    const chosen = this.voices().find((v) => v.voiceURI === state.ui.voice);
    this.speaking = true;
    onStart?.();
    chunks.forEach((chunk, i) => {
      const u = new SpeechSynthesisUtterance(chunk);
      if (chosen) u.voice = chosen;
      u.rate = Number(state.ui.rate) || 1;
      u.onboundary = () => onWord?.();
      if (i === chunks.length - 1) {
        u.onend = u.onerror = () => {
          this.speaking = false;
          onEnd?.();
        };
      }
      synth.speak(u);
    });
  },

  /** Fetch audio from the server's voice engine and play it, pulsing roughly per word. */
  async speakServer(markdown, { onStart, onWord, onEnd, onError } = {}) {
    this.stop();
    const text = plainText(markdown);
    if (!text) return;
    this.speaking = true;
    onStart?.();
    const finish = () => {
      if (this.audio?.src) URL.revokeObjectURL(this.audio.src);
      this.audio = null;
      this.speaking = false;
      onEnd?.();
    };
    try {
      const res = await fetch("/api/tts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }) });
      if (!res.ok) throw new Error((await res.json().catch(() => ({}))).detail || `Speech failed (${res.status})`);
      const audio = new Audio(URL.createObjectURL(await res.blob()));
      this.audio = audio;
      const words = Math.max(1, text.split(/\s+/).length);
      let last = -1;
      audio.addEventListener("timeupdate", () => {
        if (!audio.duration) return;
        const word = Math.floor((audio.currentTime / audio.duration) * words);
        if (word !== last) {
          last = word;
          onWord?.();
        }
      });
      audio.onended = audio.onerror = finish;
      await audio.play();
    } catch (err) {
      finish();
      onError?.(err);
    }
  },

  stop() {
    if (synth && (synth.speaking || synth.pending)) synth.cancel();
    if (this.audio) {
      this.audio.pause();
      if (this.audio.src) URL.revokeObjectURL(this.audio.src);
      this.audio = null;
    }
    this.speaking = false;
  },

  /** Start dictation. Calls onText(transcript, isFinal) and onEnd(). Returns a stop function. */
  listen(hooks) {
    if (serverStt()) return this.listenServer(hooks);
    const { onText, onEnd, onError } = hooks;
    if (!Recognition) return () => {};
    const rec = new Recognition();
    rec.lang = navigator.language || "en-US";
    rec.interimResults = true;
    rec.continuous = false;
    rec.onresult = (e) => {
      let text = "";
      let final = false;
      for (const result of e.results) {
        text += result[0].transcript;
        final = result.isFinal;
      }
      onText(text, final);
    };
    rec.onerror = (e) => onError?.(e.error);
    rec.onend = () => onEnd?.();
    rec.start();
    return () => rec.stop();
  },

  /** Record until stopped (or 30 s), then transcribe on the server with whisper.cpp. */
  listenServer({ onText, onEnd, onError }) {
    let recorder = null;
    let stream = null;
    let timer = 0;
    const chunks = [];
    const done = async () => {
      clearTimeout(timer);
      stream?.getTracks().forEach((t) => t.stop());
      try {
        if (chunks.length) {
          const blob = new Blob(chunks, { type: recorder?.mimeType || "audio/webm" });
          const res = await fetch("/api/stt", { method: "POST", headers: { "Content-Type": blob.type }, body: blob });
          const data = await res.json().catch(() => ({}));
          if (!res.ok) throw new Error(data.detail || `Transcription failed (${res.status})`);
          if (data.text) onText(data.text, true);
        }
      } catch (err) {
        onError?.(err.message);
      }
      onEnd?.();
    };
    navigator.mediaDevices.getUserMedia({ audio: true }).then((s) => {
      stream = s;
      recorder = new MediaRecorder(stream);
      recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
      recorder.onstop = done;
      recorder.start();
      timer = setTimeout(() => recorder.state === "recording" && recorder.stop(), 30000);
    }).catch((err) => {
      onError?.(err.name === "NotAllowedError" ? "not-allowed" : err.message);
      onEnd?.();
    });
    return () => {
      if (recorder?.state === "recording") recorder.stop();
    };
  },
};
