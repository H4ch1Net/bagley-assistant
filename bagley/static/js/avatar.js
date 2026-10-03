// Bagley's presence: a minimal "blob tracking" display. Soft blobs drift inside a dark viewport
// while thin tracking boxes, centroids, links and small readouts follow them. The state sets how
// many blobs are tracked, how fast they move, how they connect and what the readouts say.
// One shared animation loop drives every instance; off-screen canvases are skipped.

import { prefersReducedMotion } from "./util.js";

const STATES = {
  idle:      { count: 3, speed: 0.22, spread: 0.85, links: 0.35, energy: 0.12, color: "accent", code: "IDLE" },
  listening: { count: 3, speed: 0.32, spread: 0.6,  links: 0.55, energy: 0.2,  color: "accent", code: "INPUT", attend: 1 },
  thinking:  { count: 6, speed: 0.95, spread: 1,    links: 1,    energy: 0.55, color: "accent", code: "THINK", mesh: 1 },
  reasoning: { count: 5, speed: 0.7,  spread: 0.95, links: 1,    energy: 0.45, color: "accent", code: "REASON", mesh: 1 },
  tool:      { count: 4, speed: 0.55, spread: 0.85, links: 0.7,  energy: 0.45, color: "accent", code: "EXEC", scan: 1 },
  approval:  { count: 2, speed: 0.12, spread: 0.5,  links: 0.7,  energy: 0.25, color: "warn",   code: "AWAIT", blink: 1 },
  writing:   { count: 4, speed: 0.42, spread: 0.8,  links: 0.6,  energy: 0.4,  color: "accent", code: "TX" },
  speaking:  { count: 4, speed: 0.42, spread: 0.8,  links: 0.6,  energy: 0.45, color: "accent", code: "VOICE" },
  happy:     { count: 3, speed: 0.25, spread: 0.42, links: 0.85, energy: 0.25, color: "accent", code: "DONE", lock: 1 },
  error:     { count: 3, speed: 0.6,  spread: 0.9,  links: 0.2,  energy: 0.6,  color: "danger", code: "ERROR", glitch: 1 },
  offline:   { count: 2, speed: 0.04, spread: 0.6,  links: 0.15, energy: 0.04, color: "muted",  code: "NO SIGNAL", dim: 1 },
};  // prettier-ignore

const NUMERIC = ["speed", "spread", "links", "energy", "attend", "mesh", "scan", "blink", "lock", "glitch", "dim"];
const MAX_BLOBS = 7;
const INK = [232, 238, 245]; // Neutral tracker lines, as on a camera overlay.
const MONO = 'ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, monospace';

// Fixed pseudo-random blob paths, identical for every instance.
const fract = (x) => x - Math.floor(x);
const SEEDS = Array.from({ length: MAX_BLOBS }, (_, i) => {
  const r = (n) => fract(Math.sin((i + 1) * 12.9898 + n * 78.233) * 43758.5453);
  if (i === 0) return { hx: 0, hy: 0, ax: 0.05, ay: 0.035, fx: 0.7, fy: 0.55, px: 0, py: 1.3, size: 0.1, w1: 0.4, w2: 2.1 };
  const angle = i * 2.39996 + 0.5; // Golden angle: secondary blobs spread evenly around the primary.
  const ring = 0.27 + r(1) * 0.07;
  return {
    hx: Math.cos(angle) * ring,
    hy: Math.sin(angle) * ring * 0.8,
    ax: 0.03 + r(2) * 0.05,
    ay: 0.025 + r(3) * 0.04,
    fx: 0.6 + r(4) * 0.9,
    fy: 0.5 + r(5) * 0.9,
    px: r(6) * Math.PI * 2,
    py: r(7) * Math.PI * 2,
    size: 0.038 + r(8) * 0.03,
    w1: r(9) * Math.PI * 2,
    w2: r(10) * Math.PI * 2,
  };
});

// Colour helpers ---------------------------------------------------------------------------------

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
const rgba = (c, a) => `rgba(${c[0] | 0},${c[1] | 0},${c[2] | 0},${Math.max(0, Math.min(1, a))})`;

let palette = null;
function readPalette() {
  const s = getComputedStyle(document.documentElement);
  palette = {
    accent: toRgb(s.getPropertyValue("--avatar-accent")),
    warn: toRgb(s.getPropertyValue("--warn")),
    danger: toRgb(s.getPropertyValue("--danger")),
    muted: [120, 130, 150],
    face: toRgb(s.getPropertyValue("--avatar-face"), [12, 14, 19]),
  };
}

// Shared loop and pointer ------------------------------------------------------------------------

const instances = new Set();
let rafId = 0;
let last = 0;

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
const clamp = (v, lo, hi) => Math.max(lo, Math.min(hi, v));

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

function brackets(ctx, x, y, w, h, len) {
  const l = Math.min(len, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x, y + l); ctx.lineTo(x, y); ctx.lineTo(x + l, y);
  ctx.moveTo(x + w - l, y); ctx.lineTo(x + w, y); ctx.lineTo(x + w, y + l);
  ctx.moveTo(x + w, y + h - l); ctx.lineTo(x + w, y + h); ctx.lineTo(x + w - l, y + h);
  ctx.moveTo(x + l, y + h); ctx.lineTo(x, y + h); ctx.lineTo(x, y + h - l);
  ctx.stroke();
} // prettier-ignore

// Avatar -----------------------------------------------------------------------------------------

export class Avatar {
  constructor(canvas, { detail = "full" } = {}) {
    if (!palette) readPalette();
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.detail = detail;
    this.visible = false;
    this.name = "idle";
    this.tag = "";
    this.v = Object.fromEntries(NUMERIC.map((k) => [k, STATES.idle[k] ?? 0]));
    this.color = [...palette.accent];
    this.phase = 0;
    this.energy = 0;
    this.shake = 0;
    this.offset = { x: 0, y: 0 };
    this.readoutAt = 0;
    this.blobs = SEEDS.map((seed, i) => ({
      seed,
      presence: i < STATES.idle.count ? 1 : 0,
      box: null,
      conf: 0.9,
    }));
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

  /** Label for the primary track while a tool runs, e.g. "web_search". */
  setTag(text) {
    this.tag = String(text || "").slice(0, 18);
  }

  /** A token or spoken word arrived: the blobs swell briefly. */
  pulse(amount = 0.6) {
    this.energy = Math.min(1, this.energy + amount);
  }

  destroy() {
    instances.delete(this);
    this.resizeObs.disconnect();
    this.visObs.disconnect();
  }

  get maxBlobs() {
    return this.detail === "mini" ? 1 : this.detail === "compact" ? 2 : MAX_BLOBS;
  }

  /** Direction (normalised) from this canvas towards the message box, for the "attend" lean. */
  towardsInput() {
    const input = document.getElementById("composer-input");
    if (!input) return { x: 0, y: 0.6 };
    const a = this.canvas.getBoundingClientRect();
    const b = input.getBoundingClientRect();
    const dx = b.left + b.width / 2 - (a.left + a.width / 2);
    const dy = b.top + b.height / 2 - (a.top + a.height / 2);
    const d = Math.hypot(dx, dy) || 1;
    return { x: dx / d, y: dy / d };
  }

  tick(dt, t) {
    const s = STATES[this.name];
    const v = this.v;
    const still = prefersReducedMotion();
    const k = still ? 30 : 5;
    for (const key of NUMERIC) v[key] = ease(v[key], s[key] ?? 0, dt, k);
    const target = palette[s.color] || palette.accent;
    for (let i = 0; i < 3; i++) this.color[i] = ease(this.color[i], target[i], dt, 6);

    if (!still) this.phase += v.speed * dt;
    this.energy = Math.max(0, this.energy - dt * 2.2);
    this.shake = Math.max(0, this.shake - dt * 1.5);

    const lean = s.attend ? this.towardsInput() : { x: 0, y: 0 };
    this.offset.x = ease(this.offset.x, lean.x * 0.12, dt, 4);
    this.offset.y = ease(this.offset.y, lean.y * 0.12, dt, 4);

    const count = Math.min(s.count, this.maxBlobs);
    this.blobs.forEach((b, i) => (b.presence = ease(b.presence, i < count ? 1 : 0, dt, still ? 30 : 3.5)));

    if (t > this.readoutAt) {
      const jitter = 0.04 + v.energy * 0.12;
      for (const b of this.blobs) b.conf = clamp(0.93 - Math.random() * jitter, 0.5, 0.99);
      this.readoutAt = t + (still ? 2 : 0.18);
    }
    this.draw(t, dt, still);
  }

  /** Blob position in canvas pixels. */
  position(b, S) {
    const { seed } = b;
    const sp = this.v.spread;
    const small = this.detail !== "full";
    const wander = 0.6 + 0.6 * sp;
    const cy = small ? 0.5 : 0.55; // Leave room for the readout above the field.
    const x = 0.5 + this.offset.x + seed.hx * sp + Math.sin(this.phase * seed.fx * 2.4 + seed.px) * seed.ax * wander;
    const y = cy + this.offset.y + seed.hy * sp + Math.cos(this.phase * seed.fy * 2.4 + seed.py) * seed.ay * wander;
    return small ? { x: clamp(x, 0.3, 0.7) * S, y: clamp(y, 0.3, 0.7) * S } : { x: clamp(x, 0.16, 0.84) * S, y: clamp(y, 0.3, 0.84) * S };
  }

  draw(t, dt, still) {
    const { ctx, size: S, v, detail } = this;
    if (!S) return;
    const full = detail === "full";
    const small = detail !== "full";
    const c = this.color;
    const energy = v.energy * 0.5 + this.energy * 0.7;
    const line = Math.max(1, S * 0.0055);
    const dim = 1 - v.dim * 0.6;

    ctx.clearRect(0, 0, S, S);
    ctx.save();
    roundRect(ctx, 0.5, 0.5, S - 1, S - 1, S * (small ? 0.26 : 0.16));
    ctx.fillStyle = rgba(palette.face, 1);
    ctx.fill();
    ctx.clip();

    // Viewport furniture: dot grid and corner ticks.
    if (full) {
      const step = S / 10;
      ctx.fillStyle = rgba(INK, 0.07 * dim);
      for (let gx = step; gx < S; gx += step) {
        for (let gy = step; gy < S; gy += step) ctx.fillRect(gx - 0.5, gy - 0.5, 1, 1);
      }
      ctx.strokeStyle = rgba(c, 0.45 * dim);
      ctx.lineWidth = line;
      const m = S * 0.07;
      brackets(ctx, m, m, S - 2 * m, S - 2 * m, S * 0.06);
    }

    // Scan line while a tool runs.
    if (v.scan > 0.02 && !still) {
      const y = ((t * 0.45) % 1) * S;
      const g = ctx.createLinearGradient(0, y - S * 0.18, 0, y);
      g.addColorStop(0, rgba(c, 0));
      g.addColorStop(1, rgba(c, 0.16 * v.scan));
      ctx.fillStyle = g;
      ctx.fillRect(0, y - S * 0.18, S, S * 0.18);
      ctx.strokeStyle = rgba(c, 0.55 * v.scan);
      ctx.lineWidth = line;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(S, y);
      ctx.stroke();
    }

    const live = [];
    this.blobs.forEach((b, i) => {
      if (b.presence < 0.02) {
        b.box = null;
        return;
      }
      const p = this.position(b, S);
      const boost = detail === "mini" ? 2.4 : detail === "compact" ? 1.7 : 1;
      const r = S * b.seed.size * boost * (0.4 + 0.6 * b.presence) * (1 + 0.3 * energy);
      live.push({ b, i, p, r });
    });

    // Blob bodies: soft, additive so overlaps brighten like a heat map.
    ctx.globalCompositeOperation = "lighter";
    for (const { b, p, r } of live) {
      const g = ctx.createRadialGradient(p.x, p.y, 0, p.x, p.y, r * 1.6);
      g.addColorStop(0, rgba(c, 0.9 * b.presence * dim));
      g.addColorStop(0.32, rgba(c, 0.55 * b.presence * dim));
      g.addColorStop(0.62, rgba(c, 0.1 * b.presence * dim));
      g.addColorStop(1, rgba(c, 0));
      ctx.fillStyle = g;
      ctx.beginPath();
      ctx.arc(p.x, p.y, r * 1.6, 0, Math.PI * 2);
      ctx.fill();
    }
    ctx.globalCompositeOperation = "source-over";

    // Links between tracked centroids: a mesh while thinking, a star around the primary otherwise.
    if (live.length > 1) {
      ctx.lineWidth = line;
      for (let a = 0; a < live.length; a++) {
        for (let z = a + 1; z < live.length; z++) {
          const A = live[a];
          const Z = live[z];
          const mesh = A.i === 0 ? 1 : v.mesh;
          if (mesh < 0.05) continue;
          const d = Math.hypot(A.p.x - Z.p.x, A.p.y - Z.p.y) / S;
          const alpha = v.links * mesh * Math.max(0, 1 - d / 0.75) * Math.min(A.b.presence, Z.b.presence) * 0.6 * dim;
          if (alpha < 0.02) continue;
          ctx.setLineDash(A.i === 0 ? [] : [S * 0.012, S * 0.016]);
          ctx.strokeStyle = rgba(c, alpha);
          ctx.beginPath();
          ctx.moveTo(A.p.x, A.p.y);
          ctx.lineTo(Z.p.x, Z.p.y);
          ctx.stroke();
        }
      }
      ctx.setLineDash([]);
    }

    // Contours (full size only): the blob outline a tracker would extract.
    if (full) {
      ctx.lineWidth = line;
      for (const { b, p, r } of live) {
        const wob = 0.07 + 0.16 * energy;
        ctx.strokeStyle = rgba(c, (b.seed === SEEDS[0] ? 0.7 : 0.35) * b.presence * dim);
        ctx.beginPath();
        for (let k = 0; k <= 40; k++) {
          const a = (k / 40) * Math.PI * 2;
          const rr = r * (1 + wob * (0.6 * Math.sin(3 * a + t * 1.7 + b.seed.w1) + 0.4 * Math.sin(5 * a - t * 2.3 + b.seed.w2)));
          const x = p.x + Math.cos(a) * rr;
          const y = p.y + Math.sin(a) * rr;
          if (k === 0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
        ctx.stroke();
      }
    }

    // Tracking boxes lag a little behind their blobs, which is what makes them read as trackers.
    const font = Math.max(8, Math.round(S * 0.046));
    const labels = full && S >= 100;
    ctx.font = `500 ${font}px ${MONO}`;
    ctx.textBaseline = "bottom";
    const blink = v.blink > 0.05 && !still ? 0.6 + 0.4 * Math.sin(t * 7) : 1;
    for (const { b, i, p, r } of live) {
      const pad = r * (1.35 - 0.2 * v.attend);
      const target = { x: p.x - pad, y: p.y - pad, w: pad * 2, h: pad * 2 };
      if (!b.box) b.box = { ...target };
      const rate = still ? 30 : 7 + 9 * v.mesh;
      for (const key of ["x", "y", "w", "h"]) b.box[key] = ease(b.box[key], target[key], dt, rate);
      let { x, y, w, h } = b.box;
      if (v.glitch > 0.05 && !still && Math.random() < 0.3 + 0.5 * this.shake) {
        x += (Math.random() - 0.5) * S * 0.04 * v.glitch;
        y += (Math.random() - 0.5) * S * 0.03 * v.glitch;
      }
      const alpha = b.presence * dim;
      ctx.lineWidth = line;
      if (v.dim > 0.5) ctx.setLineDash([S * 0.015, S * 0.015]);
      if (i === 0) {
        ctx.strokeStyle = rgba(c, 0.95 * alpha * blink);
        brackets(ctx, x, y, w, h, Math.max(3, w * 0.28));
      } else {
        ctx.strokeStyle = rgba(INK, 0.42 * alpha);
        ctx.strokeRect(x, y, w, h);
      }
      ctx.setLineDash([]);

      // Centroid crosshair.
      if (!small || i === 0) {
        const arm = Math.max(2, S * 0.022);
        ctx.strokeStyle = rgba(INK, 0.85 * alpha);
        ctx.beginPath();
        ctx.moveTo(p.x - arm, p.y);
        ctx.lineTo(p.x + arm, p.y);
        ctx.moveTo(p.x, p.y - arm);
        ctx.lineTo(p.x, p.y + arm);
        ctx.stroke();
      }

      if (labels && alpha > 0.3 && v.lock < 0.3) {
        let text = i === 0 ? `ID00 ${b.conf.toFixed(2)}` : String(i).padStart(2, "0");
        if (i === 0 && this.name === "tool" && this.tag) text = this.tag;
        ctx.fillStyle = i === 0 ? rgba(c, alpha * blink) : rgba(INK, 0.45 * alpha);
        ctx.fillText(text, x, y - 2);
        if (i === 0) {
          ctx.textBaseline = "top";
          ctx.fillStyle = rgba(INK, 0.45 * alpha);
          ctx.fillText(`x${(p.x / S).toFixed(3).slice(1)} y${(p.y / S).toFixed(3).slice(1)}`, x, y + h + 2);
          ctx.textBaseline = "bottom";
        }
      }
    }

    // Lock-on: one box around everything when a reply lands.
    if (v.lock > 0.05 && live.length) {
      const xs = live.map((l) => l.p.x);
      const ys = live.map((l) => l.p.y);
      const m = S * 0.09;
      const x = Math.min(...xs) - m;
      const y = Math.min(...ys) - m;
      const w = Math.max(...xs) - Math.min(...xs) + 2 * m;
      const h = Math.max(...ys) - Math.min(...ys) + 2 * m;
      ctx.strokeStyle = rgba(c, 0.9 * v.lock);
      ctx.lineWidth = line * 1.5;
      brackets(ctx, x, y, w, h, Math.max(4, w * 0.18));
      if (labels) {
        ctx.fillStyle = rgba(c, v.lock);
        ctx.fillText("LOCK", x, y - 2);
      }
    }

    // Readout: state code top left, track count bottom right.
    if (labels) {
      const m = S * 0.1;
      ctx.textBaseline = "top";
      ctx.fillStyle = rgba(c, 0.9 * blink);
      ctx.fillRect(m, m + font * 0.3, font * 0.45, font * 0.45);
      ctx.fillStyle = rgba(INK, 0.75);
      ctx.fillText(STATES[this.name].code, m + font * 0.8, m);
      ctx.textBaseline = "bottom";
      ctx.textAlign = "right";
      ctx.fillStyle = rgba(INK, 0.45);
      ctx.fillText(`TRK ${String(live.length).padStart(2, "0")}`, S - m, S - m);
      ctx.textAlign = "left";
    }

    ctx.restore();
    ctx.lineWidth = 1;
    ctx.strokeStyle = rgba(c, 0.28);
    roundRect(ctx, 0.5, 0.5, S - 1, S - 1, S * (small ? 0.26 : 0.16));
    ctx.stroke();
  }
}

/** Static SVG version for message headers (no animation cost per message). */
export function avatarGlyph(size = 24) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("width", size);
  svg.setAttribute("height", size);
  svg.setAttribute("aria-hidden", "true");
  svg.innerHTML =
    '<rect x=".5" y=".5" width="23" height="23" rx="6" fill="var(--avatar-face)" stroke="var(--avatar-accent)" stroke-opacity=".35"/>' +
    '<circle cx="10" cy="10.5" r="3.4" fill="var(--avatar-accent)" fill-opacity=".85"/>' +
    '<circle cx="16.8" cy="16" r="1.6" fill="#e8eef5" fill-opacity=".7"/>' +
    '<path d="M10 10.5 16.8 16" stroke="var(--avatar-accent)" stroke-opacity=".55" stroke-width=".9"/>' +
    '<path d="M5.5 8V5.5H8M12 5.5h2.5V8M14.5 13v2.5H12M8 15.5H5.5V13" fill="none" stroke="var(--avatar-accent)" stroke-width="1.1" stroke-linecap="round"/>';
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
    setTag: (text) => list.forEach((a) => a.setTag(text)),
    pulse: (amount) => list.forEach((a) => a.pulse(amount)),
  };
}
