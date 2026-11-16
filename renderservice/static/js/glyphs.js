const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

export const COLORS = { acid: "#c0ff00", dim: "#8aa800", alert: "#ff4b2b", void: "#000000" };

function hash(i, j, s) {
  let h = Math.imul(i, 374761393) + Math.imul(j, 668265263) + Math.imul(s, 1442695041);
  h = Math.imul(h ^ (h >>> 13), 1274126177);
  return ((h ^ (h >>> 16)) >>> 0) / 4294967295;
}

function smooth(t) {
  return t * t * (3 - 2 * t);
}

function vnoise(x, y, s) {
  const xi = Math.floor(x);
  const yi = Math.floor(y);
  const u = smooth(x - xi);
  const v = smooth(y - yi);
  const a = hash(xi, yi, s);
  const b = hash(xi + 1, yi, s);
  const c = hash(xi, yi + 1, s);
  const d = hash(xi + 1, yi + 1, s);
  return a + (b - a) * u + (c - a) * v + (a - b - c + d) * u * v;
}

function rng(seed) {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function seg(ctx, x1, y1, x2, y2) {
  ctx.beginPath();
  ctx.moveTo(x1, y1);
  ctx.lineTo(x2, y2);
  ctx.stroke();
}

export function drawGlyph(ctx, level, x, y, c) {
  const cx = x + c / 2;
  const cy = y + c / 2;
  switch (level) {
    case 1:
      ctx.fillRect(cx - 1, cy - 1, 2, 2);
      break;
    case 2: {
      const r = c * 0.2;
      ctx.lineWidth = 1;
      seg(ctx, cx - r, cy, cx + r, cy);
      seg(ctx, cx, cy - r, cx, cy + r);
      break;
    }
    case 3: {
      const r = c * 0.22;
      ctx.lineWidth = 1.5;
      seg(ctx, cx - r, cy - r, cx + r, cy + r);
      seg(ctx, cx - r, cy + r, cx + r, cy - r);
      break;
    }
    case 4:
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(cx, cy, c * 0.3, 0, 6.2832);
      ctx.stroke();
      break;
    case 5:
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      ctx.arc(cx, cy, c * 0.34, 0, 6.2832);
      ctx.stroke();
      ctx.fillRect(cx - 1.5, cy - 1.5, 3, 3);
      break;
    case 6: {
      const r = c * 0.28;
      ctx.lineWidth = 3;
      seg(ctx, cx - r, cy - r, cx + r, cy + r);
      seg(ctx, cx - r, cy + r, cx + r, cy - r);
      break;
    }
    case 7: {
      const s = c * 0.6;
      ctx.lineWidth = 2.5;
      ctx.strokeRect(cx - s / 2, cy - s / 2, s, s);
      break;
    }
    case 8: {
      const s = c * 0.78;
      ctx.fillRect(cx - s / 2, cy - s / 2, s, s);
      break;
    }
  }
}

function levelFromValue(v) {
  if (v < 0.2) return 0;
  if (v < 0.3) return 1;
  if (v < 0.4) return 2;
  if (v < 0.5) return 3;
  if (v < 0.6) return 4;
  if (v < 0.7) return 5;
  if (v < 0.8) return 6;
  if (v < 0.9) return 7;
  return 8;
}

const drawables = new Set();
const STEP = 1000 / 12;
let looping = false;
let lastStep = 0;

function loop(now) {
  if (now - lastStep >= STEP) {
    lastStep = now;
    if (!document.hidden) {
      for (const item of drawables) {
        if (item.visible) item.draw(now);
      }
    }
  }
  requestAnimationFrame(loop);
}

const resizer = new ResizeObserver((items) => {
  for (const item of items) {
    const target = item.target.__drawable;
    if (!target) continue;
    target.dirty = true;
    if (reduced) target.draw(performance.now());
  }
});

const watcher = new IntersectionObserver((items) => {
  for (const item of items) {
    const target = item.target.__drawable;
    if (target) target.visible = item.isIntersecting;
  }
});

class Drawable {
  constructor(canvas) {
    this.canvas = canvas;
    this.ctx = canvas.getContext("2d");
    this.visible = true;
    this.dirty = true;
    this.w = 0;
    this.h = 0;
    this.dpr = 1;
    canvas.__drawable = this;
  }

  start() {
    resizer.observe(this.canvas);
    watcher.observe(this.canvas);
    drawables.add(this);
    if (reduced) {
      this.draw(performance.now());
    } else if (!looping) {
      looping = true;
      requestAnimationFrame(loop);
    }
  }

  fit() {
    const rect = this.canvas.getBoundingClientRect();
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    const w = Math.max(1, Math.round(rect.width));
    const h = Math.max(1, Math.round(rect.height));
    if (w === this.w && h === this.h && dpr === this.dpr) return false;
    this.w = w;
    this.h = h;
    this.dpr = dpr;
    this.canvas.width = Math.round(w * dpr);
    this.canvas.height = Math.round(h * dpr);
    this.ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return true;
  }

  destroy() {
    drawables.delete(this);
    resizer.unobserve(this.canvas);
    watcher.unobserve(this.canvas);
    delete this.canvas.__drawable;
  }
}

export class GlyphField extends Drawable {
  constructor(canvas, { cell = 14, seed = 1 } = {}) {
    super(canvas);
    this.cell = cell;
    this.seed = seed;
    this.t0 = performance.now();
    this.lastTick = -1;
    this.cols = 0;
    this.rows = 0;
    this.blocks = [];
    this.mask = new Uint8Array(0);
    this.random = rng(seed * 7919);
    this.start();
  }

  makeBlocks() {
    const { cols, rows } = this;
    const count = Math.max(4, Math.round(cols / 12));
    this.blocks = Array.from({ length: count }, (_, k) => this.rollBlock({ kind: k % 5 < 3 ? 1 : 2 }, 0));
    this.rebuildMask();
  }

  rollBlock(block, now) {
    const { cols, rows } = this;
    const r = this.random;
    block.w = 3 + Math.floor(r() * Math.max(4, cols * 0.12));
    block.h = 2 + Math.floor(r() * Math.max(2, rows * 0.4));
    block.c = Math.floor(cols * 0.34 + r() * (cols * 0.66 - block.w + 1));
    block.r = Math.floor(r() * Math.max(1, rows - block.h + 1));
    block.next = now + 900 + Math.random() * 2400;
    return block;
  }

  rebuildMask() {
    const { cols, rows } = this;
    this.mask = new Uint8Array(cols * rows);
    for (const b of this.blocks) {
      for (let j = b.r; j < Math.min(rows, b.r + b.h); j++) {
        for (let i = b.c; i < Math.min(cols, b.c + b.w); i++) {
          this.mask[j * cols + i] = b.kind;
        }
      }
    }
  }

  levelAt(i, j, t) {
    const s = this.seed;
    const drift = t * 0.00006;
    const n =
      vnoise(i * 0.09 + drift * 8, j * 0.13 - drift * 5, s) * 0.62 +
      vnoise(i * 0.31 - drift * 12, j * 0.37 + drift * 9, s + 7) * 0.38;
    const side = Math.pow(i / Math.max(1, this.cols), 1.15);
    const v = (n - 0.5) * 2.2 + 0.5 + (side - 0.35) * 0.95;
    return levelFromValue(v);
  }

  draw(now) {
    const resized = this.fit();
    const t = now - this.t0;
    const tick = reduced ? 0 : Math.floor(t / 110);
    if (!resized && !this.dirty && tick === this.lastTick) return;
    this.lastTick = tick;
    this.dirty = false;

    const { ctx, cell } = this;
    const cols = Math.ceil(this.w / cell);
    const rows = Math.ceil(this.h / cell);
    if (cols !== this.cols || rows !== this.rows) {
      this.cols = cols;
      this.rows = rows;
      this.makeBlocks();
    }
    let changed = false;
    if (!reduced) {
      for (const b of this.blocks) {
        if (now > b.next) {
          this.rollBlock(b, now);
          changed = true;
        }
      }
    }
    if (changed) this.rebuildMask();

    ctx.fillStyle = COLORS.void;
    ctx.fillRect(0, 0, this.w, this.h);
    ctx.fillStyle = COLORS.acid;
    ctx.strokeStyle = COLORS.acid;
    const boot = reduced ? 1 : Math.min(1, t / 1100);
    const bootRows = Math.ceil(rows * boot);
    const time = tick * 110;
    for (let j = 0; j < bootRows; j++) {
      for (let i = 0; i < cols; i++) {
        const m = this.mask[j * cols + i];
        if (m === 2) continue;
        if (m === 1) {
          ctx.fillRect(i * cell, j * cell, cell, cell);
          continue;
        }
        const level = this.levelAt(i, j, time);
        if (level) drawGlyph(ctx, level, i * cell, j * cell, cell);
      }
    }
  }
}

export class ProgressStrip extends Drawable {
  constructor(canvas, { cell = 14, colors = COLORS } = {}) {
    super(canvas);
    this.cell = cell;
    this.colors = colors;
    this.target = 0;
    this.shown = 0;
    this.mode = "idle";
    this.start();
  }

  set(value, mode) {
    this.target = Math.max(0, Math.min(1, value));
    this.mode = mode;
    this.dirty = true;
    if (reduced) {
      this.shown = this.target;
      this.draw(performance.now());
    }
  }

  draw(now) {
    this.fit();
    const { ctx, cell, colors } = this;
    const cols = Math.max(1, Math.floor(this.w / cell));
    const rows = Math.max(1, Math.floor(this.h / cell));
    this.shown += (this.target - this.shown) * 0.25;
    if (Math.abs(this.target - this.shown) < 0.002) this.shown = this.target;
    const active = this.mode === "running";
    const color =
      this.mode === "failed" ? colors.alert : this.mode === "idle" || this.mode === "cancelled" ? colors.dim : colors.acid;
    ctx.clearRect(0, 0, this.w, this.h);
    ctx.fillStyle = color;
    ctx.strokeStyle = color;
    const front = this.shown * cols;
    const tick = Math.floor(now / 110);
    for (let j = 0; j < rows; j++) {
      for (let i = 0; i < cols; i++) {
        let level;
        if (this.mode === "done") {
          level = 8;
        } else if (i < front - 4) {
          level = 8;
        } else if (i < front + 3 && front > 0.5) {
          const wave = active ? Math.sin(tick * 0.9 + j * 1.9 + i) * 1.3 : 0;
          level = Math.max(1, Math.min(8, Math.round(7 - (i - (front - 4)) * 1.1 + wave)));
        } else {
          level = (active || this.mode === "queued") && hash(i, j, tick) > 0.96 ? 3 : 1;
        }
        drawGlyph(ctx, level, i * cell, j * cell, cell);
      }
    }
    if (this.mode === "failed") {
      const i = Math.min(cols - 1, Math.floor(front));
      for (let j = 0; j < rows; j++) drawGlyph(ctx, 6, i * cell, j * cell, cell);
    }
  }
}

export class TrackOverlay extends Drawable {
  constructor(canvas) {
    super(canvas);
    this.resolution = [1920, 1080];
    this.alpha = 0;
    this.on = false;
    const r = Math.random;
    this.markers = Array.from({ length: 7 }, () => ({
      fx: 0.5 + r() * 1.3,
      fy: 0.4 + r() * 1.1,
      px: r() * 6.28,
      py: r() * 6.28,
      cx: 0.15 + r() * 0.7,
      cy: 0.15 + r() * 0.7,
      ax: 0.06 + r() * 0.16,
      ay: 0.06 + r() * 0.16,
      size: 14 + Math.floor(r() * 30),
    }));
    this.start();
  }

  setActive(on) {
    this.on = on;
    this.dirty = true;
  }

  draw(now) {
    this.fit();
    const { ctx } = this;
    this.alpha += ((this.on ? 1 : 0) - this.alpha) * 0.2;
    ctx.clearRect(0, 0, this.w, this.h);
    if (this.alpha < 0.03 || reduced) return;
    const t = now / 1000;
    const points = this.markers.map((m) => ({
      m,
      x: Math.round((m.cx + m.ax * Math.sin(t * m.fx + m.px)) * this.w * 0.5) * 2,
      y: Math.round((m.cy + m.ay * Math.sin(t * m.fy + m.py)) * this.h * 0.5) * 2,
    }));
    ctx.globalAlpha = this.alpha;
    ctx.strokeStyle = COLORS.acid;
    ctx.lineWidth = 1;
    ctx.globalAlpha = this.alpha * 0.45;
    ctx.beginPath();
    points.forEach((p, k) => (k ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
    ctx.moveTo(points[0].x, points[0].y);
    ctx.lineTo(points[3].x, points[3].y);
    ctx.stroke();
    ctx.globalAlpha = this.alpha;
    ctx.font = "10px 'Martian Mono', monospace";
    ctx.textBaseline = "top";
    points.forEach((p, k) => {
      const s = p.m.size;
      ctx.strokeRect(p.x - s / 2, p.y - s / 2, s, s);
      ctx.beginPath();
      ctx.moveTo(p.x - 3, p.y);
      ctx.lineTo(p.x + 3, p.y);
      ctx.moveTo(p.x, p.y - 3);
      ctx.lineTo(p.x, p.y + 3);
      ctx.stroke();
      const nx = Math.round((p.x / this.w) * this.resolution[0]);
      const ny = Math.round((p.y / this.h) * this.resolution[1]);
      const label = `x: ${nx} y: ${ny}`;
      const width = ctx.measureText(label).width + 6;
      ctx.save();
      ctx.translate(p.x + s / 2 + 4, p.y - s / 2);
      if (k % 2) ctx.rotate(Math.PI / 2);
      ctx.fillStyle = "rgba(0,0,0,0.7)";
      ctx.fillRect(0, 0, width, 14);
      ctx.fillStyle = COLORS.acid;
      ctx.fillText(label, 3, 2);
      ctx.restore();
    });
    ctx.globalAlpha = 1;
  }
}

const NOISE = "+×/\\|_<>#=-*0123456789ABCDEFXZ";

export function scramble(el, text, duration = 650) {
  if (el.__scramble) cancelAnimationFrame(el.__scramble);
  if (reduced || !text) {
    el.textContent = text;
    return;
  }
  const start = performance.now();
  const n = text.length;
  const step = (now) => {
    const p = Math.min(1, (now - start) / duration);
    const fixed = Math.floor(p * n);
    let out = "";
    for (let i = 0; i < n; i++) {
      const ch = text[i];
      out += i < fixed || ch === " " ? ch : NOISE[Math.floor(Math.random() * NOISE.length)];
    }
    el.textContent = out;
    if (p < 1) {
      el.__scramble = requestAnimationFrame(step);
    } else {
      el.textContent = text;
      el.__scramble = 0;
    }
  };
  el.__scramble = requestAnimationFrame(step);
}
