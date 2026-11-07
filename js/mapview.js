// Top-down world map (region tiles) with pan, zoom and box selection.
import { $, UI, outlinedText } from './common.js?v=8126566452';
import { api } from './backend.js?v=8126566452';

// ---------------------------------------------------------------- map view ---
export class MapView {
  constructor(canvas) {
    this.c = canvas; this.ctx = canvas.getContext('2d');
    this.cx = 0; this.cz = 0; this.scale = 1.5; this.tiles = new Map(); this.regions = new Set();
    this.queue = []; this.loading = 0; this.sel = null; this.mode = 'pan'; this.world = null; this.player = null;
    this.onSelect = () => {}; this.onHover = () => {};
    new ResizeObserver(() => this.resize()).observe(canvas);
    canvas.addEventListener('pointerdown', (e) => this.down(e));
    canvas.addEventListener('pointermove', (e) => this.move(e));
    canvas.addEventListener('pointerup', (e) => this.up(e));
    canvas.addEventListener('pointerleave', () => this.onHover(''));
    canvas.addEventListener('wheel', (e) => this.wheel(e), { passive: false });
  }
  setWorld(name, regions, center, player) {
    this.world = name; this.tiles.clear(); this.queue = [];
    this.regions = new Set(regions.map(([x, z]) => `${x},${z}`));
    this.cx = center[0]; this.cz = center[2]; this.player = player; this.scale = 1.5;
    this.resize();
  }
  resize() {
    const r = this.c.getBoundingClientRect(); const dpr = window.devicePixelRatio || 1;
    this.dpr = dpr; this.w = r.width; this.h = r.height;
    this.c.width = Math.max(1, r.width * dpr); this.c.height = Math.max(1, r.height * dpr);
    this.draw();
  }
  toWorld(sx, sy) { return [(sx - this.w / 2) / this.scale + this.cx, (sy - this.h / 2) / this.scale + this.cz]; }
  toScreen(x, z) { return [(x - this.cx) * this.scale + this.w / 2, (z - this.cz) * this.scale + this.h / 2]; }
  ensureTiles() {
    if (!this.world) return;
    const [x0, z0] = this.toWorld(0, 0), [x1, z1] = this.toWorld(this.w, this.h);
    const want = [];
    for (let rz = Math.floor(z0 / 512); rz <= Math.floor(z1 / 512); rz++) {
      for (let rx = Math.floor(x0 / 512); rx <= Math.floor(x1 / 512); rx++) {
        const k = `${rx},${rz}`;
        if (this.regions.has(k) && !this.tiles.has(k) && !this.queue.includes(k)) want.push(k);
      }
    }
    const d = (k) => { const [rx, rz] = k.split(',').map(Number); return Math.hypot(rx * 512 + 256 - this.cx, rz * 512 + 256 - this.cz); };
    this.queue.push(...want); this.queue.sort((a, b) => d(a) - d(b));
    this.pump();
  }
  pump() {
    while (this.loading < 3 && this.queue.length) {
      const k = this.queue.shift(); this.loading++;
      this.fetchTile(k).catch((e) => console.warn('tile', k, e)).finally(() => { this.loading--; this.pump(); });
    }
  }
  async fetchTile(k) {
    const world = this.world; const [rx, rz] = k.split(',').map(Number);
    const tile = { state: 'loading' }; this.tiles.set(k, tile); this.draw();
    let buf;
    try { buf = await api.tile(world, rx, rz); } catch (e) { tile.state = 'error'; throw e; }
    if (world !== this.world) return;
    const rgb = new Uint8Array(buf, 0, 512 * 512 * 3);
    tile.top = new Int16Array(buf, 512 * 512 * 3, 512 * 512);
    const cv = document.createElement('canvas'); cv.width = cv.height = 512;
    const cx = cv.getContext('2d'); const img = cx.createImageData(512, 512);
    for (let i = 0, j = 0; i < 512 * 512; i++, j += 3) {
      const empty = tile.top[i] <= -9999;
      img.data[i * 4] = rgb[j]; img.data[i * 4 + 1] = rgb[j + 1]; img.data[i * 4 + 2] = rgb[j + 2];
      img.data[i * 4 + 3] = empty ? 0 : 255;
    }
    cx.putImageData(img, 0, 0);
    tile.canvas = cv; tile.state = 'ok'; this.draw();
  }
  draw() {
    const g = this.ctx; g.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    g.fillStyle = UI.canvas; g.fillRect(0, 0, this.w, this.h);
    if (!this.world) {
      g.fillStyle = UI.muted; g.font = '14px system-ui'; g.textAlign = 'center';
      g.fillText('Open a world (left panel) to see its map here.', this.w / 2, this.h / 2); return;
    }
    g.imageSmoothingEnabled = false;
    for (const [k, t] of this.tiles) {
      const [rx, rz] = k.split(',').map(Number); const [sx, sy] = this.toScreen(rx * 512, rz * 512);
      const size = 512 * this.scale;
      if (sx > this.w || sy > this.h || sx + size < 0 || sy + size < 0) continue;
      if (t.state === 'ok') g.drawImage(t.canvas, sx, sy, size, size);
      else { g.fillStyle = '#1a1a1d'; g.fillRect(sx, sy, size, size); }
    }
    if (this.scale >= 4) {  // chunk grid
      g.strokeStyle = 'rgba(255,255,255,0.07)'; g.lineWidth = 1; g.beginPath();
      const [x0, z0] = this.toWorld(0, 0), [x1, z1] = this.toWorld(this.w, this.h);
      for (let x = Math.floor(x0 / 16) * 16; x <= x1; x += 16) { const [sx] = this.toScreen(x, 0); g.moveTo(sx, 0); g.lineTo(sx, this.h); }
      for (let z = Math.floor(z0 / 16) * 16; z <= z1; z += 16) { const [, sy] = this.toScreen(0, z); g.moveTo(0, sy); g.lineTo(this.w, sy); }
      g.stroke();
    }
    if (this.player) {
      const [px, py] = this.toScreen(this.player[0], this.player[2]);
      g.fillStyle = '#ff5a5f'; g.beginPath(); g.arc(px, py, 5, 0, Math.PI * 2); g.fill();
      g.strokeStyle = '#fff'; g.lineWidth = 1.5; g.stroke();
      outlinedText(g, 'player', px + 8, py + 4, '12px system-ui', UI.ink);
    }
    if (this.sel) {
      const { x0, z0, x1, z1 } = this.sel;
      const [ax, ay] = this.toScreen(x0, z0), [bx, by] = this.toScreen(x1 + 1, z1 + 1);
      g.fillStyle = 'rgba(204,85,0,0.22)'; g.fillRect(ax, ay, bx - ax, by - ay);
      g.strokeStyle = UI.brand; g.lineWidth = 2; g.strokeRect(ax, ay, bx - ax, by - ay);
      outlinedText(g, `${x1 - x0 + 1} × ${z1 - z0 + 1} blocks`, ax + 4, ay - 6, '12px system-ui', UI.ink);
    }
    this.ensureTiles();
  }
  down(e) {
    this.c.setPointerCapture(e.pointerId);
    const r = this.c.getBoundingClientRect(); const p = [e.clientX - r.left, e.clientY - r.top];
    if (e.shiftKey || this.mode === 'select') {
      const [x, z] = this.toWorld(...p).map(Math.floor); this.drag = { kind: 'sel', x, z };
      this.sel = { x0: x, z0: z, x1: x, z1: z };
    } else this.drag = { kind: 'pan', p };
  }
  move(e) {
    const r = this.c.getBoundingClientRect(); const p = [e.clientX - r.left, e.clientY - r.top];
    const [wx, wz] = this.toWorld(...p).map(Math.floor);
    const k = `${Math.floor(wx / 512)},${Math.floor(wz / 512)}`; const t = this.tiles.get(k);
    let top = '';
    if (t && t.top) { const v = t.top[(((wz % 512) + 512) % 512) * 512 + (((wx % 512) + 512) % 512)]; if (v > -9999) top = ` · top block y ${v}`; }
    this.onHover(`x ${wx}, z ${wz}${top}`);
    if (!this.drag) return;
    if (this.drag.kind === 'pan') {
      this.cx -= (p[0] - this.drag.p[0]) / this.scale; this.cz -= (p[1] - this.drag.p[1]) / this.scale; this.drag.p = p;
    } else {
      this.sel = { x0: Math.min(this.drag.x, wx), z0: Math.min(this.drag.z, wz), x1: Math.max(this.drag.x, wx), z1: Math.max(this.drag.z, wz) };
    }
    this.draw();
  }
  up() { if (this.drag && this.drag.kind === 'sel') this.onSelect(this.sel); this.drag = null; }
  wheel(e) {
    e.preventDefault();
    const r = this.c.getBoundingClientRect(); const p = [e.clientX - r.left, e.clientY - r.top];
    const before = this.toWorld(...p);
    this.scale = Math.min(24, Math.max(0.08, this.scale * Math.exp(-e.deltaY * 0.0015)));
    const after = this.toWorld(...p);
    this.cx += before[0] - after[0]; this.cz += before[1] - after[1]; this.draw();
  }
}

