// Bagley's animated face. Canvas-drawn, state driven, and cheap: every parameter eases toward
// the target of the current state, one shared animation loop drives all instances, and
// off-screen or hidden canvases are skipped.

import { prefersReducedMotion } from "./util.js";

const STATES = {
  idle:      { eyeH: 1,    eyeW: 1,    squint: 0,    look: "wander", ring: 0.22, spread: 0,   glow: 0.42, smile: 0.32, mouthW: 1,    color: "accent" },
  listening: { eyeH: 1.12, eyeW: 1.06, squint: 0,    look: "input",  ring: 0.45, spread: 0.2, glow: 0.62, smile: 0.18, mouthW: 0.8,  color: "accent" },
  thinking:  { eyeH: 0.72, eyeW: 1,    squint: 0.4,  look: "up",     ring: 2.6,  spread: 1,   glow: 0.66, smile: 0,    mouthW: 0.45, color: "accent" },
  reasoning: { eyeH: 0.68, eyeW: 1,    squint: 0.45, look: "up",     ring: 1.8,  spread: 0.8, glow: 0.7,  smile: 0,    mouthW: 0.45, color: "accent" },
  tool:      { eyeH: 0.86, eyeW: 1.04, squint: 0.1,  look: "scan",   ring: 1.3,  spread: 0.5, glow: 0.72, smile: 0.05, mouthW: 0.6,  color: "accent", scan: 1 },
  approval:  { eyeH: 1.16, eyeW: 1.06, squint: 0,    look: "down",   ring: 0.6,  spread: 0.3, glow: 0.7,  smile: 0,    mouthW: 0.55, color: "warn" },
  writing:   { eyeH: 1,    eyeW: 1,    squint: 0,    look: "drift",  ring: 0.7,  spread: 0.2, glow: 0.74, smile: 0.12, mouthW: 1,    color: "accent", talk: 1 },
  speaking:  { eyeH: 1,    eyeW: 1,    squint: 0,    look: "drift",  ring: 0.7,  spread: 0.2, glow: 0.78, smile: 0.12, mouthW: 1,    color: "accent", talk: 1 },
  happy:     { eyeH: 1,    eyeW: 1.05, squint: 0,    look: "center", ring: 0.9,  spread: 0.3, glow: 0.8,  smile: 0.75, mouthW: 1.05, color: "accent", happy: 1 },
  error:     { eyeH: 0.78, eyeW: 1,    squint: 0.15, look: "down",   ring: 0.12, spread: 0,   glow: 0.5,  smile: -0.45, mouthW: 0.7, color: "danger", tilt: 1 },
  offline:   { eyeH: 0.07, eyeW: 1.08, squint: 0,    look: "sleep",  ring: 0.04, spread: 0,   glow: 0.12, smile: 0.05, mouthW: 0.6,  color: "muted", sleep: 1 },
};

const NUMERIC = ["eyeH", "eyeW", "squint", "ring", "spread", "glow", "smile", "mouthW", "scan", "talk", "happy", "tilt", "sleep"];

// Colour helpers -------------------------------------------------------------------------------

const probe = document.createElement("canvas").getContext("2d");
function toRgb(css, fallback = [80, 200, 230]) {
  if (!css) return fallback;
  probe.fillStyle = "#000";
  probe.fillStyle = css.trim();
  const v = probe.fillStyle;
  if (v.startsWith("#")) return [1, 3, 5].map((i) => parseInt(v.slice(i, i + 2), 16));
  const m = v.match(/[\d.]+/g);
  return m ? m.slice(0, 3).map(Number) : fallback;
}
const rgba = (c, a) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${a})`;

let palette = null;
function readPalette() {
  const s = getComputedStyle(document.documentElement);
  palette = {
    accent: toRgb(s.getPropertyValue("--avatar-accent")),
    warn: toRgb(s.getPropertyValue("--warn")),
    danger: toRgb(s.getPropertyValue("--danger")),
    muted: toRgb(s.getPropertyValue("--text-3")),
    face: toRgb(s.getPropertyValue("--avatar-face"), [12, 14, 19]),
  };
}

// Shared loop and pointer ------------------------------------------------------------------------

const instances = new Set();
const pointer = { x: 0, y: 0, t: -1e9 };
let rafId = 0;
let last = 0;

addEventListener("pointermove", (e) => {
  pointer.x = e.clientX;
  pointer.y = e.clientY;
  pointer.t = performance.now();
}, { passive: true });

new MutationObserver(() => {
  readPalette();
  wake();
}).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "style", "class"] });

document.addEventListener("visibilitychange", wake);

function frame(now) {
  rafId = 0;
  const dt = Math.min(0.05, (now - (last || now)) / 1000);
  last = now;
  let anyVisible = false;
  for (const a of instances) {
    if (!a.visible) continue;
    anyVisible = true;
    a.tick(dt, now / 1000);
  }
  if (anyVisible && !document.hidden) rafId = requestAnimationFrame(frame);
  else last = 0;
}

function wake() {
  if (!rafId && !document.hidden) rafId = requestAnimationFrame(frame);
}

const ease = (current, target, dt, speed) => current + (target - current) * (1 - Math.exp(-speed * dt));
const rand = (a, b) => a + Math.random() * (b - a);

// Avatar -----------------------------------------------------------------------------------------

export class Avatar {
  constructor(canvas, { detail = "full" } = {}) {
    if (!palette) readPalette();
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.detail = detail;
    this.visible = false;
    this.name = "idle";
    this.v = { ...STATES.idle, scan: 0, talk: 0, happy: 0, tilt: 0, sleep: 0, lookX: 0, lookY: 0 };
    this.color = [...palette.accent];
    this.phase = rand(0, Math.PI * 2);
    this.energy = 0;
    this.blinkAt = performance.now() / 1000 + rand(1, 4);
    this.blinkP = -1;
    this.wanderAt = 0;
    this.wander = { x: 0, y: 0 };
    this.shake = 0;
    this.size = 0;

    this.resizeObs = new ResizeObserver(() => this.resize());
    this.resizeObs.observe(canvas);
    this.visObs = new IntersectionObserver((entries) => {
      this.visible = entries[entries.length - 1].isIntersecting;
      if (this.visible) wake();
    });
    this.visObs.observe(canvas);
    instances.add(this);
    this.resize();
  }

  resize() {
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const size = Math.round(this.canvas.clientWidth);
    if (!size) return;
    this.size = size;
    this.canvas.width = size * dpr;
    this.canvas.height = size * dpr;
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    wake();
  }

  setState(name) {
    if (!STATES[name] || name === this.name) return;
    if (name === "error") this.shake = 1;
    this.name = name;
    wake();
  }

  /** A token or spoken word arrived: give the mouth a bit of energy. */
  pulse(amount = 0.6) {
    this.energy = Math.min(1, this.energy + amount);
  }

  destroy() {
    instances.delete(this);
    this.resizeObs.disconnect();
    this.visObs.disconnect();
  }

  lookTarget(t) {
    const s = STATES[this.name];
    const still = prefersReducedMotion();
    switch (s.look) {
      case "up": return { x: 0.35 + Math.sin(t * 0.7) * 0.15, y: -0.7 };
      case "down": return { x: 0, y: 0.7 };
      case "sleep": return { x: 0, y: 0.35 };
      case "center": return { x: 0, y: -0.1 };
      case "scan": return still ? { x: 0, y: 0.1 } : { x: Math.sin(t * 3.4) * 0.85, y: 0.15 };
      case "input": {
        const input = document.getElementById("composer-input");
        if (input) return this.towards(input.getBoundingClientRect(), 0.9);
        return { x: 0, y: 0.6 };
      }
      default: {
        if (!still && performance.now() - pointer.t < 2500) return this.towards({ left: pointer.x, top: pointer.y, width: 0, height: 0 }, 1);
        if (still) return { x: 0, y: 0 };
        if (t > this.wanderAt) {
          const range = s.look === "drift" ? 0.25 : 0.55;
          this.wander = { x: rand(-range, range), y: rand(-range * 0.6, range * 0.5) };
          this.wanderAt = t + rand(1.4, 4);
        }
        return this.wander;
      }
    }
  }

  towards(rect, strength) {
    const box = this.canvas.getBoundingClientRect();
    const cx = box.left + box.width / 2;
    const cy = box.top + box.height / 2;
    const dx = rect.left + rect.width / 2 - cx;
    const dy = rect.top + rect.height / 2 - cy;
    const dist = Math.hypot(dx, dy) || 1;
    const reach = Math.min(1, dist / 300) * strength;
    return { x: (dx / dist) * reach, y: (dy / dist) * reach * 0.8 };
  }

  tick(dt, t) {
    const s = STATES[this.name];
    const v = this.v;
    const still = prefersReducedMotion();
    const k = still ? 30 : 7;
    for (const key of NUMERIC) v[key] = ease(v[key], s[key] ?? 0, dt, k);
    const look = this.lookTarget(t);
    v.lookX = ease(v.lookX, look.x, dt, still ? 30 : 9);
    v.lookY = ease(v.lookY, look.y, dt, still ? 30 : 9);
    const target = palette[s.color] || palette.accent;
    for (let i = 0; i < 3; i++) this.color[i] = ease(this.color[i], target[i], dt, 6);

    if (!still) this.phase += v.ring * dt;
    this.energy = Math.max(0, this.energy - dt * 2.2);
    if (s.talk && !still && this.name === "writing") this.energy = Math.max(this.energy, 0.25);
    this.shake = Math.max(0, this.shake - dt * 2);

    if (this.blinkP < 0 && t > this.blinkAt && v.sleep < 0.5) this.blinkP = 0;
    if (this.blinkP >= 0) {
      this.blinkP += dt / 0.16;
      if (this.blinkP >= 1) {
        this.blinkP = -1;
        this.blinkAt = t + (Math.random() < 0.18 ? 0.22 : rand(2.2, 6));
      }
    }
    this.draw(t);
  }

  draw(t) {
    const { ctx, size, v, detail } = this;
    if (!size) return;
    const c = this.color;
    const still = prefersReducedMotion();
    ctx.clearRect(0, 0, size, size);
    const R = size * (detail === "mini" ? 0.5 : 0.4);
    const cx = size / 2 + (still ? 0 : Math.sin(t * 46) * R * 0.05 * this.shake);
    const cy = size / 2;
    const breathe = still ? 0 : Math.sin(t * 1.3) * 0.06;

    // Glow
    if (detail !== "mini") {
      const g = ctx.createRadialGradient(cx, cy, R * 0.55, cx, cy, R * 1.3);
      g.addColorStop(0, rgba(c, Math.max(0, (v.glow + breathe) * 0.42)));
      g.addColorStop(1, rgba(c, 0));
      ctx.fillStyle = g;
      ctx.fillRect(0, 0, size, size);
    }

    // Orbit ring
    if (detail !== "mini") {
      const rr = R * 1.02;
      ctx.lineCap = "round";
      ctx.lineWidth = Math.max(1, R * 0.028);
      ctx.strokeStyle = rgba(c, 0.13);
      ctx.beginPath();
      ctx.arc(cx, cy, rr, 0, Math.PI * 2);
      ctx.stroke();
      ctx.lineWidth = Math.max(1.5, R * 0.045);
      const arcs = [[0, 0.95], [2.25, 0.5], [4.15, 0.24]];
      for (const [offset, len] of arcs) {
        const a0 = this.phase + offset;
        const span = len * (1 + v.spread * 1.1);
        ctx.strokeStyle = rgba(c, 0.55 + v.glow * 0.4);
        ctx.beginPath();
        ctx.arc(cx, cy, rr, a0, a0 + span);
        ctx.stroke();
      }
      if (v.scan > 0.02) {
        const a0 = -this.phase * 1.7;
        ctx.strokeStyle = rgba(c, 0.8 * v.scan);
        ctx.beginPath();
        ctx.arc(cx, cy, rr * 1.12, a0, a0 + 0.7);
        ctx.stroke();
      }
    }

    // Face
    const fr = R * (detail === "mini" ? 0.98 : 0.84) * (1 + breathe * 0.04);
    ctx.fillStyle = rgba(palette.face, 1);
    ctx.beginPath();
    ctx.arc(cx, cy, fr, 0, Math.PI * 2);
    ctx.fill();
    const hl = ctx.createLinearGradient(0, cy - fr, 0, cy + fr);
    hl.addColorStop(0, "rgba(255,255,255,0.09)");
    hl.addColorStop(0.5, "rgba(255,255,255,0)");
    ctx.fillStyle = hl;
    ctx.fill();
    ctx.lineWidth = Math.max(1, R * 0.02);
    ctx.strokeStyle = rgba(c, 0.32);
    ctx.stroke();

    // Eyes
    let blink = 1;
    if (this.blinkP >= 0) blink = 1 - Math.sin(Math.PI * Math.min(1, this.blinkP)) * 0.92;
    const gap = fr * 0.33;
    const ey = cy - fr * 0.08 + v.lookY * fr * 0.11;
    const ex = v.lookX * fr * 0.15;
    const ew = fr * 0.2 * v.eyeW;
    const fullH = fr * 0.36 * v.eyeH;
    const eh = Math.max(ew * 0.22, fullH * blink * (1 - v.squint * 0.45));
    const lidShift = v.squint * fullH * 0.18;
    ctx.save();
    if (detail !== "mini") {
      ctx.shadowColor = rgba(c, 0.85);
      ctx.shadowBlur = R * 0.16 * v.glow;
    }
    for (const side of [-1, 1]) {
      const x = cx + side * gap + ex;
      const y = ey + lidShift;
      ctx.save();
      ctx.translate(x, y);
      ctx.rotate(side * -0.32 * v.tilt);
      const rectAlpha = 1 - Math.min(1, v.happy * 1.4);
      if (rectAlpha > 0.01) {
        ctx.fillStyle = rgba(c, rectAlpha);
        roundRect(ctx, -ew / 2, -eh / 2, ew, eh, Math.min(ew, eh) / 2);
        ctx.fill();
      }
      if (v.happy > 0.05) {
        ctx.strokeStyle = rgba(c, Math.min(1, v.happy));
        ctx.lineWidth = ew * 0.5;
        ctx.lineCap = "round";
        ctx.beginPath();
        ctx.arc(0, ew * 0.55, ew * 0.85, Math.PI * 1.2, Math.PI * 1.8);
        ctx.stroke();
      }
      ctx.restore();
    }
    ctx.restore();

    // Mouth
    if (detail !== "mini") {
      const mw = fr * 0.5 * v.mouthW;
      const my = cy + fr * 0.4;
      const amp = still ? 0 : fr * 0.085 * Math.min(1, this.energy * 1.4) * v.talk;
      ctx.strokeStyle = rgba(c, 0.9);
      ctx.lineWidth = Math.max(1.5, fr * 0.055);
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      ctx.beginPath();
      const steps = 24;
      for (let i = 0; i <= steps; i++) {
        const u = (i / steps) * 2 - 1;
        const env = 1 - u * u;
        const y = my + v.smile * fr * 0.09 * env + Math.sin(u * Math.PI * 2.3 + t * 15) * amp * env;
        const x = cx + (u * mw) / 2;
        if (i === 0) ctx.moveTo(x, y);
        else ctx.lineTo(x, y);
      }
      ctx.stroke();
    }

    // Sleeping z's
    if (detail === "full" && v.sleep > 0.5 && !still) {
      ctx.fillStyle = rgba(c, 0.7);
      for (let i = 0; i < 2; i++) {
        const p = (t / 2.6 + i * 0.5) % 1;
        ctx.globalAlpha = Math.sin(p * Math.PI) * (v.sleep - 0.5) * 2;
        ctx.font = `600 ${Math.round(fr * (0.2 + p * 0.12))}px ui-sans-serif, system-ui, sans-serif`;
        ctx.fillText("z", cx + fr * (0.62 + p * 0.3), cy - fr * (0.55 + p * 0.55));
      }
      ctx.globalAlpha = 1;
    }
  }
}

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

/** Static SVG version of the face for message headers (no animation cost per message). */
export function avatarGlyph(size = 24) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML =
    '<circle cx="12" cy="12" r="11" fill="var(--avatar-face)" stroke="var(--avatar-accent)" stroke-opacity=".45"/>' +
    '<rect x="7.2" y="7.6" width="3" height="5.6" rx="1.5" fill="var(--avatar-accent)"/>' +
    '<rect x="13.8" y="7.6" width="3" height="5.6" rx="1.5" fill="var(--avatar-accent)"/>' +
    '<path d="M9 16.2q3 1.4 6 0" stroke="var(--avatar-accent)" stroke-width="1.4" fill="none" stroke-linecap="round"/>';
  return svg;
}

/** Instantiate every `canvas[data-avatar]` and return a controller for all of them. */
export function mountAvatars(root = document) {
  const details = { stage: "full", hero: "full", compact: "compact", mini: "mini" };
  const list = [...root.querySelectorAll("canvas[data-avatar]")].map(
    (canvas) => new Avatar(canvas, { detail: details[canvas.dataset.avatar] || "full" }),
  );
  return {
    list,
    setState: (name) => list.forEach((a) => a.setState(name)),
    pulse: (amount) => list.forEach((a) => a.pulse(amount)),
  };
}
