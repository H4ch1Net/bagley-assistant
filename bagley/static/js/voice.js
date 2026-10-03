// Text to speech (local system voices) and optional speech input.

import { plainText } from "./markdown.js";
import { state } from "./state.js";

const synth = window.speechSynthesis;
const Recognition = window.SpeechRecognition || window.webkitSpeechRecognition;

export const voice = {
  canSpeak: Boolean(synth),
  canListen: Boolean(Recognition),
  speaking: false,

  voices() {
    return synth ? synth.getVoices() : [];
  },

  onVoicesChanged(fn) {
    if (synth) synth.addEventListener?.("voiceschanged", fn);
  },

  /** Speak markdown text. Hooks: onStart, onWord, onEnd. */
  speak(markdown, { onStart, onWord, onEnd } = {}) {
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

  stop() {
    if (synth && (synth.speaking || synth.pending)) synth.cancel();
    this.speaking = false;
  },

  /** Start dictation. Calls onText(transcript, isFinal) and onEnd(). Returns a stop function. */
  listen({ onText, onEnd, onError }) {
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
};
