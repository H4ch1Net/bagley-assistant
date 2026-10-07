// Bagley's presence: a small node graph seen through a tracking overlay. A diamond hub (ID00) and
// a few satellite nodes hold their places inside a square viewport; edges join them, signal
// packets travel along the edges, and thin tracking boxes, IDs and readouts follow each node.
// The state sets how many nodes are tracked, how much traffic flows, which way it flows and what
// the readouts say. One shared animation loop drives every instance; off-screen canvases are
// skipped.

import { prefersReducedMotion } from "./util.js";

// flow: packets per second. dir: 1 = out of the hub, -1 = into the hub, 0 = any way.
const STATES = {
  idle:      { count: 4, speed: 0.2,  spread: 0.85, links: 0.55, mesh: 0.25, flow: 0.35, dir: 1,  color: "accent", code: "IDLE" },
  listening: { count: 4, speed: 0.28, spread: 0.7,  links: 0.7,  mesh: 0.3,  flow: 1.2,  dir: -1, color: "accent", code: "INPUT", attend: 1 },
  thinking:  { count: 8, speed: 0.6,  spread: 1,    links: 1,    mesh: 1,    flow: 7,    dir: 0,  color: "accent", code: "THINK" },
  reasoning: { count: 7, speed: 0.45, spread: 0.95, links: 1,    mesh: 1,    flow: 4.5,  dir: 0,  color: "accent", code: "REASON", chain: 1 },
  tool:      { count: 5, speed: 0.4,  spread: 0.9,  links: 0.85, mesh: 0.45, flow: 3,    dir: 1,  color: "accent", code: "EXEC", scan: 1, target: 1 },
  approval:  { count: 3, speed: 0.1,  spread: 0.55, links: 0.7,  mesh: 0.3,  flow: 0,    dir: 0,  color: "warn",   code: "AWAIT", blink: 1 },
  writing:   { count: 5, speed: 0.32, spread: 0.85, links: 0.8,  mesh: 0.5,  flow: 1.5,  dir: 1,  color: "accent", code: "TX" },
  speaking:  { count: 5, speed: 0.32, spread: 0.85, links: 0.8,  mesh: 0.5,  flow: 1.5,  dir: 1,  color: "accent", code: "VOICE" },
  happy:     { count: 4, speed: 0.2,  spread: 0.5,  links: 0.9,  mesh: 0.6,  flow: 0.6,  dir: 1,  color: "ok",     code: "DONE", lock: 1 },
  error:     { count: 4, speed: 0.5,  spread: 0.9,  links: 0.35, mesh: 0.2,  flow: 0,    dir: 0,  color: "danger", code: "ERROR", glitch: 1 },
  offline:   { count: 3, speed: 0.03, spread: 0.6,  links: 0.3,  mesh: 0.1,  flow: 0,    dir: 0,  color: "muted",  code: "NO SIGNAL", dim: 1 },
};  // prettier-ignore

const NUMERIC = ["speed", "spread", "links", "mesh", "flow", "attend", "scan", "blink", "lock", "glitch", "dim", "chain"];
const MAX_NODES = 9;
const INK = [226, 226, 226]; // Neutral tracker lines, as on a camera overlay.
const MONO = '"JetBrainsMono Nerd Font", "JetBrains Mono", ui-monospace, "SF Mono", "Cascadia Code", Menlo, Consolas, monospace';

// Fixed pseudo-random node places, identical for every instance. Node 0 is the hub.
const fract = (x) => x - Math.floor(x);
const SEEDS = Array.from({ length: MAX_NODES }, (_, i) => {
  const r = (n) => fract(Math.sin((i + 1) * 12.9898 + n * 78.233) * 43758.5453);
  if (i === 0) return { hx: 0, hy: 0, ax: 0.012, ay: 0.01, fx: 0.7, fy: 0.55, px: 0, py: 1.3 };
  const angle = i * 2.39996 + 0.5; // Golden angle: satellites spread evenly around the hub.
  const ring = 0.29 + r(1) * 0.08;
  return {
    hx: Math.cos(angle) * ring,
    hy: Math.sin(angle) * ring * 0.78,
    ax: 0.012 + r(2) * 0.02,
    ay: 0.01 + r(3) * 0.018,
    fx: 0.6 + r(4) * 0.9,
    fy: 0.5 + r(5) * 0.9,
    px: r(6) * Math.PI * 2,
    py: r(7) * Math.PI * 2,
  };
});

// Edges: every satellite reports to the hub (spine), and each satellite links to its two nearest
// satellites (mesh). Built once from the home positions so the graph keeps its shape.
const EDGES = (() => {
  const list = [];
  const seen = new Set();
  const add = (a, b, kind) => {
    const key = a < b ? `${a}-${b}` : `${b}-${a}`;
    if (seen.has(key)) return;
    seen.add(key);
    list.push({ a: Math.min(a, b), b: Math.max(a, b), kind });
  };
  for (let i = 1; i < MAX_NODES; i++) add(0, i, "spine");
  for (let i = 1; i < MAX_NODES; i++) {
    const near = [];
    for (let j = 1; j < MAX_NODES; j++) {
      if (j !== i) near.push([Math.hypot(SEEDS[i].hx - SEEDS[j].hx, SEEDS[i].hy - SEEDS[j].hy), j]);
    }
    near.sort((p, q) => p[0] - q[0]);
    for (const [, j] of near.slice(0, 2)) add(i, j, "mesh");
  }
  return list;
})();

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
    ok: toRgb(s.getPropertyValue("--ok")),
    warn: toRgb(s.getPropertyValue("--warn")),
    danger: toRgb(s.getPropertyValue("--danger")),
    muted: [122, 122, 122],
    face: toRgb(s.getPropertyValue("--avatar-face"), [14, 14, 14]),
  };
}

// Shared loop ------------------------------------------------------------------------------------

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

/** Four L-shaped corners, like the ctOS bar's CornerFrame. */
function brackets(ctx, x, y, w, h, len) {
  const l = Math.min(len, w / 2, h / 2);
  ctx.beginPath();
  ctx.moveTo(x, y + l); ctx.lineTo(x, y); ctx.lineTo(x + l, y);
  ctx.moveTo(x + w - l, y); ctx.lineTo(x + w, y); ctx.lineTo(x + w, y + l);
  ctx.moveTo(x + w, y + h - l); ctx.lineTo(x + w, y + h); ctx.lineTo(x + w - l, y + h);
  ctx.moveTo(x + l, y + h); ctx.lineTo(x, y + h); ctx.lineTo(x, y + h - l);
  ctx.stroke();
} // prettier-ignore

function diamond(ctx, x, y, r) {
  ctx.beginPath();
  ctx.moveTo(x, y - r);
  ctx.lineTo(x + r, y);
  ctx.lineTo(x, y + r);
  ctx.lineTo(x - r, y);
  ctx.closePath();
}

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
    this.spawn = 0;
    this.burst = 0;
    this.packets = [];
    this.nodes = SEEDS.map((seed, i) => ({ seed, presence: i < STATES.idle.count ? 1 : 0, act: 0, box: null, conf: 0.9 }));
    this.edgeCut = EDGES.map(() => 1); // 0..1, dropped out while an error glitches the graph.
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

  /** Label for the hub while a tool runs, e.g. "web_search". */
  setTag(text) {
    this.tag = String(text || "").slice(0, 18);
  }

  /** A token or spoken word arrived: the hub fires and sends packets out. */
  pulse(amount = 0.6) {
    this.energy = Math.min(1, this.energy + amount);
    this.burst = Math.min(6, this.burst + amount * 2.5);
  }

  destroy() {
    instances.delete(this);
    this.resizeObs.disconnect();
    this.visObs.disconnect();
  }

  get maxNodes() {
    return this.detail === "mini" ? 2 : this.detail === "compact" ? 3 : MAX_NODES;
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
    this.offset.x = ease(this.offset.x, lean.x * 0.08, dt, 4);
    this.offset.y = ease(this.offset.y, lean.y * 0.08, dt, 4);

    const count = Math.min(s.count, this.maxNodes);
    this.nodes.forEach((n, i) => {
      n.presence = ease(n.presence, i < count ? 1 : 0, dt, still ? 30 : 3.5);
      n.act = Math.max(0, n.act - dt * 1.8);
    });

    // Error: edges drop out and come back at random, as a tracker losing the graph.
    EDGES.forEach((_, i) => {
      const lost = v.glitch > 0.3 && !still && Math.random() < dt * 4 ? Math.random() : 1;
      this.edgeCut[i] = lost < 1 ? lost : ease(this.edgeCut[i], 1, dt, 2.5);
    });

    if (t > this.readoutAt) {
      const jitter = 0.04 + (v.flow / 7) * 0.1 + this.energy * 0.08;
      for (const n of this.nodes) n.conf = clamp(0.94 - Math.random() * jitter, 0.5, 0.99);
      this.readoutAt = t + (still ? 2 : 0.2);
    }

    if (!still) this.traffic(dt, s);
    else this.packets.length = 0;
    this.draw(t, dt, still);
  }

  /** Spawn, move and deliver signal packets along the edges. */
  traffic(dt, s) {
    const v = this.v;
    const live = (e) => Math.min(this.nodes[e.a].presence, this.nodes[e.b].presence) > 0.6;
    const pick = (fromHub) => {
      const pool = EDGES.map((e, i) => ({ e, i })).filter(({ e }) => live(e) && (fromHub ? e.kind === "spine" : e.kind === "spine" || v.mesh > 0.4));
      if (!pool.length) return null;
      if (s.target && fromHub) {
        const hit = pool.find(({ e }) => e.b === 1);
        if (hit && Math.random() < 0.7) return hit;
      }
      return pool[(Math.random() * pool.length) | 0];
    };
    const send = (fromHub) => {
      const p = pick(fromHub);
      if (!p) return;
      let forward = Math.random() < 0.5;
      if (p.e.kind === "spine") forward = fromHub ? true : s.dir === 0 ? forward : s.dir > 0;
      this.packets.push({ edge: p.i, forward, t: 0 });
    };

    this.spawn += v.flow * dt;
    while (this.spawn >= 1) {
      this.spawn -= 1;
      send(s.dir > 0);
    }
    while (this.burst >= 1) {
      this.burst -= 1;
      send(true);
      this.nodes[0].act = Math.min(1, this.nodes[0].act + 0.35);
    }

    const rate = 0.9 + v.speed * 1.6;
    for (const p of this.packets) {
      p.t += rate * dt;
      if (p.t >= 1) {
        const e = EDGES[p.edge];
        const node = this.nodes[p.forward ? e.b : e.a];
        node.act = Math.min(1, node.act + 0.55);
        // Reasoning passes the signal on along a chain instead of letting it stop.
        if (v.chain > 0.5 && Math.random() < 0.6) {
          const at = p.forward ? e.b : e.a;
          const next = EDGES.map((x, i) => ({ x, i })).filter(({ x, i }) => i !== p.edge && (x.a === at || x.b === at) && live(x));
          if (next.length) {
            const n = next[(Math.random() * next.length) | 0];
            this.packets.push({ edge: n.i, forward: n.x.a === at, t: 0 });
          }
        }
      }
    }
    this.packets = this.packets.filter((p) => p.t < 1 && live(EDGES[p.edge])).slice(-40);
  }

  /** Node position in canvas pixels. */
  position(n, S) {
    const { seed } = n;
    const sp = this.v.spread;
    const small = this.detail !== "full";
    const cy = small ? 0.5 : 0.54; // Leave room for the readout above the field.
    const x = 0.5 + this.offset.x + seed.hx * sp * (small ? 1.05 : 1) + Math.sin(this.phase * seed.fx * 2.4 + seed.px) * seed.ax;
    const y = cy + this.offset.y + seed.hy * sp * (small ? 1.05 : 1) + Math.cos(this.phase * seed.fy * 2.4 + seed.py) * seed.ay;
    return small ? { x: clamp(x, 0.24, 0.76) * S, y: clamp(y, 0.24, 0.76) * S } : { x: clamp(x, 0.14, 0.86) * S, y: clamp(y, 0.27, 0.86) * S };
  }

  draw(t, dt, still) {
    const { ctx, size: S, v, detail } = this;
    if (!S) return;
    const full = detail === "full";
    const small = !full;
    const c = this.color;
    const line = Math.max(1, S * 0.0055);
    const dim = 1 - v.dim * 0.6;

    ctx.clearRect(0, 0, S, S);
    ctx.save();
    ctx.fillStyle = rgba(palette.face, 1);
    ctx.fillRect(0, 0, S, S);

    // Viewport furniture: a faint square grid and corner brackets.
    if (full) {
      ctx.strokeStyle = rgba(INK, 0.045 * dim);
      ctx.lineWidth = 1;
      ctx.beginPath();
      const step = S / 8;
      for (let g = step; g < S - 1; g += step) {
        ctx.moveTo(Math.round(g) + 0.5, 0);
        ctx.lineTo(Math.round(g) + 0.5, S);
        ctx.moveTo(0, Math.round(g) + 0.5);
        ctx.lineTo(S, Math.round(g) + 0.5);
      }
      ctx.stroke();
      ctx.strokeStyle = rgba(INK, 0.55 * dim);
      ctx.lineWidth = line;
      const m = S * 0.06;
      brackets(ctx, m, m, S - 2 * m, S - 2 * m, S * 0.05);
    }

    // Scan line while a tool runs.
    if (v.scan > 0.02 && !still) {
      const y = ((t * 0.45) % 1) * S;
      ctx.fillStyle = rgba(c, 0.07 * v.scan);
      ctx.fillRect(0, y - S * 0.12, S, S * 0.12);
      ctx.strokeStyle = rgba(c, 0.55 * v.scan);
      ctx.lineWidth = line;
      ctx.beginPath();
      ctx.moveTo(0, y);
      ctx.lineTo(S, y);
      ctx.stroke();
    }

    const pos = this.nodes.map((n) => (n.presence < 0.02 ? null : this.position(n, S)));

    // Edges: the spine to the hub is solid accent, the mesh between satellites is a faint dash.
    ctx.lineWidth = line;
    EDGES.forEach((e, i) => {
      const A = pos[e.a];
      const B = pos[e.b];
      if (!A || !B) return;
      const presence = Math.min(this.nodes[e.a].presence, this.nodes[e.b].presence);
      const weight = e.kind === "spine" ? v.links : v.links * v.mesh;
      const alpha = weight * presence * this.edgeCut[i] * dim * (e.kind === "spine" ? 0.55 : 0.32);
      if (alpha < 0.02) return;
      ctx.setLineDash(e.kind === "spine" && v.dim < 0.5 ? [] : [S * 0.012, S * 0.014]);
      ctx.strokeStyle = e.kind === "spine" ? rgba(c, alpha) : rgba(INK, alpha);
      ctx.beginPath();
      ctx.moveTo(A.x, A.y);
      ctx.lineTo(B.x, B.y);
      ctx.stroke();
    });
    ctx.setLineDash([]);

    // Packets: a small square with a short tail, moving along its edge.
    const pk = Math.max(1.5, S * 0.011);
    for (const p of this.packets) {
      const e = EDGES[p.edge];
      const A = pos[p.forward ? e.a : e.b];
      const B = pos[p.forward ? e.b : e.a];
      if (!A || !B) continue;
      const x = A.x + (B.x - A.x) * p.t;
      const y = A.y + (B.y - A.y) * p.t;
      const tail = Math.max(0, p.t - 0.18);
      ctx.strokeStyle = rgba(c, 0.5 * dim);
      ctx.beginPath();
      ctx.moveTo(A.x + (B.x - A.x) * tail, A.y + (B.y - A.y) * tail);
      ctx.lineTo(x, y);
      ctx.stroke();
      ctx.fillStyle = rgba(c, 0.95 * dim);
      ctx.fillRect(x - pk / 2, y - pk / 2, pk, pk);
    }

    // Nodes. The hub is the ctOS diamond; satellites are small squares that brighten as signals land.
    const font = Math.max(8, Math.round(S * 0.046));
    const labels = full && S >= 100;
    ctx.font = `500 ${font}px ${MONO}`;
    ctx.textBaseline = "bottom";
    const blink = v.blink > 0.05 && !still ? 0.6 + 0.4 * Math.sin(t * 7) : 1;
    const energy = this.energy;

    this.nodes.forEach((n, i) => {
      const p = pos[i];
      if (!p) {
        n.box = null;
        return;
      }
      const alpha = n.presence * dim;
      const hub = i === 0;
      const boost = detail === "mini" ? 2.2 : detail === "compact" ? 1.6 : 1;
      const r = S * (hub ? 0.05 : 0.016) * boost * (0.5 + 0.5 * n.presence) * (1 + (hub ? 0.18 * energy : 0.35 * n.act));

      if (hub) {
        ctx.lineWidth = line;
        ctx.strokeStyle = rgba(c, 0.95 * alpha * blink);
        diamond(ctx, p.x, p.y, r);
        ctx.stroke();
        ctx.fillStyle = rgba(c, (0.35 + 0.6 * Math.max(n.act, energy)) * alpha * blink);
        diamond(ctx, p.x, p.y, r * 0.45);
        ctx.fill();
        if (full) {
          ctx.strokeStyle = rgba(c, 0.5 * alpha);
          ctx.beginPath();
          ctx.moveTo(p.x, p.y - r);
          ctx.lineTo(p.x, p.y - r * 0.45);
          ctx.stroke();
        }
      } else {
        ctx.fillStyle = rgba(INK, (0.45 + 0.55 * n.act) * alpha);
        ctx.fillRect(p.x - r, p.y - r, r * 2, r * 2);
        if (n.act > 0.05) {
          ctx.fillStyle = rgba(c, n.act * alpha);
          ctx.fillRect(p.x - r * 0.5, p.y - r * 0.5, r, r);
        }
      }

      // Tracking box, lagging a little behind its node.
      if (small && !hub) return;
      const pad = hub ? r * 1.9 : r * 3.2 * (1 - 0.15 * v.attend);
      const target = { x: p.x - pad, y: p.y - pad, w: pad * 2, h: pad * 2 };
      if (!n.box) n.box = { ...target };
      const rate = still ? 30 : 6 + 6 * v.mesh;
      for (const key of ["x", "y", "w", "h"]) n.box[key] = ease(n.box[key], target[key], dt, rate);
      let { x, y, w, h } = n.box;
      if (v.glitch > 0.05 && !still && Math.random() < 0.3 + 0.5 * this.shake) {
        x += (Math.random() - 0.5) * S * 0.04 * v.glitch;
        y += (Math.random() - 0.5) * S * 0.03 * v.glitch;
      }
      ctx.lineWidth = line;
      if (v.dim > 0.5) ctx.setLineDash([S * 0.015, S * 0.015]);
      if (hub) {
        ctx.strokeStyle = rgba(c, 0.95 * alpha * blink);
        brackets(ctx, x, y, w, h, Math.max(3, w * 0.28));
      } else {
        ctx.strokeStyle = rgba(INK, (0.3 + 0.4 * n.act) * alpha);
        ctx.strokeRect(x, y, w, h);
      }
      ctx.setLineDash([]);

      if (labels && alpha > 0.3 && v.lock < 0.3) {
        ctx.fillStyle = hub ? rgba(c, alpha * blink) : rgba(INK, 0.5 * alpha);
        ctx.fillText(String(i).padStart(2, "0"), x, y - 2);
      }
    });

    // Lock-on: one box around the whole graph when a reply lands.
    const shown = pos.filter(Boolean);
    if (v.lock > 0.05 && shown.length) {
      const xs = shown.map((p) => p.x);
      const ys = shown.map((p) => p.y);
      const m = S * 0.08;
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

    // Readout: state code and hub track top, hub position and counts bottom.
    if (labels) {
      const m = S * 0.1;
      const hub = this.nodes[0];
      ctx.textBaseline = "top";
      ctx.fillStyle = rgba(c, 0.9 * blink);
      ctx.fillRect(m, m + font * 0.3, font * 0.45, font * 0.45);
      ctx.fillStyle = rgba(INK, 0.8);
      ctx.fillText(STATES[this.name].code, m + font * 0.8, m);
      ctx.textAlign = "right";
      ctx.fillStyle = rgba(c, 0.9 * dim * blink);
      ctx.fillText(this.name === "tool" && this.tag ? this.tag : `ID00 ${hub.conf.toFixed(2)}`, S - m, m);
      ctx.textBaseline = "bottom";
      ctx.fillStyle = rgba(INK, 0.5);
      const sig = this.packets.length ? `SIG ${String(this.packets.length).padStart(2, "0")}  ` : "";
      ctx.fillText(`${sig}TRK ${String(shown.length).padStart(2, "0")}`, S - m, S - m);
      ctx.textAlign = "left";
      if (pos[0]) ctx.fillText(`x${(pos[0].x / S).toFixed(3).slice(1)} y${(pos[0].y / S).toFixed(3).slice(1)}`, m, S - m);
    }

    ctx.restore();
    ctx.lineWidth = 1;
    ctx.strokeStyle = rgba(c, 0.3);
    ctx.strokeRect(0.5, 0.5, S - 1, S - 1);
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
    '<rect x=".5" y=".5" width="23" height="23" fill="var(--avatar-face)" stroke="var(--avatar-accent)" stroke-opacity=".35"/>' +
    '<path d="M10.5 11 17 6.5M10.5 11 17.5 16.5M10.5 11 6 17" stroke="var(--avatar-accent)" stroke-opacity=".6" stroke-width=".9"/>' +
    '<path d="M17 6.5 17.5 16.5" stroke="#e2e2e2" stroke-opacity=".35" stroke-width=".8" stroke-dasharray="1.2 1.2"/>' +
    '<path d="M10.5 6.8 14.7 11 10.5 15.2 6.3 11Z" fill="var(--avatar-face)" stroke="var(--avatar-accent)" stroke-width="1.1"/>' +
    '<path d="M10.5 9.2 12.3 11 10.5 12.8 8.7 11Z" fill="var(--avatar-accent)"/>' +
    '<rect x="15.9" y="5.4" width="2.2" height="2.2" fill="#e2e2e2" fill-opacity=".8"/>' +
    '<rect x="16.4" y="15.4" width="2.2" height="2.2" fill="#e2e2e2" fill-opacity=".8"/>' +
    '<rect x="4.9" y="15.9" width="2.2" height="2.2" fill="#e2e2e2" fill-opacity=".8"/>';
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
