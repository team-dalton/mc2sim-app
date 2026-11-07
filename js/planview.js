// Per-floor 2D plan: cached floor image, furniture, labels, corrections, assembly point, people.
import * as THREE from 'three';
import { $, CAT, CLOSED_COL, GOAL_COL, UI, b64, clamp, heatColor, outlinedText } from './common.js';
import { S, shownClosed, view } from './state.js';
import { fixKeyOf, fixLevel, overlayData } from './app.js';

// ------------------------------------------------------------- floor plan ---
export const SHAPES = ['pass', 'solid', 'slab', 'stairs', 'barrier', 'obstacle', 'door', 'liquid'];
export const SHAPE_WORD = { solid: 'wall / solid block', slab: 'slab', stairs: 'stair block', barrier: 'fence / wall / pane',
  obstacle: 'obstacle / furniture', door: 'door', liquid: 'water / lava', pass: 'walk-through' };
export const PLAN_COL = {   // black theme: dark floors, white walls, orange stairs
  bg: '#0d0d0f', indoor: '#1e2836', outdoor: '#17181b', safe: '#14301f', stairs: '#a8611c', door: '#6e4a2c', exit: '#b8312f',
  between: '#1a212b', wall: '#ececef', glass: '#5aa9d6', barrier: '#8e949c', slab: '#5e5647', stairblock: '#d9832e',
  liquid: '#2f5f9a', furniture: '#3f4a38', doorblock: '#c58a4e',
};
export const OBJ_STYLE = { chair: ['#2e9e5b', 'C'], table: ['#8d6e3f', 'T'], bed: ['#d9534f', 'B'], storage: ['#a1887f', 'S'],
  workstation: ['#26a69a', 'W'], kitchen: ['#9c64c4', 'K'] };
export const FIX_INFO = {
  exit: ['Real exit', 'E'], not_exit: ['Not an exit', '✕'], chair: ['Chair', 'C'], table: ['Table', 'T'],
  stairs: ['Stairs, not a chair', 'S'], obstacle: ['Just an obstacle', '■'], open: ['Walk-through', '○'],
  levels: ['Floor heights', 'F'],
};
export const TOOL_HINT = {
  look: 'Drag to pan, scroll to zoom, hover to inspect. Double-click a spot to stand there in the 3D view (spectator).',
  exit: 'Click a walkable spot that really is a way out (for example a fire escape that was missed).',
  not_exit: 'Click a red exit cell that people cannot really use (a window, a fenced edge…). The whole exit is marked.',
  chair: 'Click the block that is a chair.', table: 'Click the block that is a table.',
  stairs: 'Click a "chair" (green C) that is really part of a staircase.',
  obstacle: 'Click a block that is just an obstacle (wrongly read as furniture).',
  open: 'Click a block people can walk through (it was read as a wall or obstacle).',
  goal: 'Click a spot on the green open ground where everyone should gather.',
  erase: 'Click a correction (orange mark) to remove it.',
};
export const pastelCache = new Map();
export function pastel(hex, amt = 0.5) {  // muted version of a category colour for the black plan
  if (!pastelCache.has(hex)) pastelCache.set(hex, '#' + new THREE.Color(hex).lerp(new THREE.Color('#111114'), amt).getHexString());
  return pastelCache.get(hex);
}
export const _hc = new THREE.Color();
export function planCellColor(n, mode, data, between) {
  if (view.closedSet[n]) return CLOSED_COL;
  const k = view.nodeKind[n], f = view.nodeFlags[n]; const safe = f & 1, indoor = f & 2, exit = f & 4;
  if (data) { const v = data.values[n]; return v <= 0 ? (indoor ? PLAN_COL.indoor : PLAN_COL.outdoor) : '#' + heatColor(v / data.max, _hc).getHexString(); }
  if (mode === 'room') { const R = view.nodeRoom[n]; if (R >= 0) return pastel(CAT[(R * 7) % CAT.length]); }
  if (mode === 'floor') { const L = view.nodeLevel[n]; if (L >= 0 && indoor) return pastel(CAT[L % CAT.length]); }
  if (mode === 'off') return safe ? PLAN_COL.safe : '#222327';
  if (exit) return PLAN_COL.exit;
  if (k === 1) return PLAN_COL.stairs;
  if (k === 2) return PLAN_COL.door;
  if (safe) return PLAN_COL.safe;
  if (between) return PLAN_COL.between;
  return indoor ? PLAN_COL.indoor : PLAN_COL.outdoor;
}
export function groundLevel(B) {
  if (!B.levels.length) return -1;
  const g = B.summary.ground_h ?? B.levels[0].elevation;
  return B.levels.reduce((best, l, i) => (Math.abs(l.elevation - g) < Math.abs(B.levels[best].elevation - g) ? i : best), 0);
}
export function nearestLevel(y) {
  const lv = S.B ? S.B.levels : [];
  let best = -1, d = Infinity;
  lv.forEach((l, i) => { const e = Math.abs(l.elevation - y); if (e < d) { d = e; best = i; } });
  return best;
}

export class PlanView {
  constructor(canvas) {
    this.c = canvas; this.ctx = canvas.getContext('2d'); this.K = 10; this.level = 0; this.tool = 'look';
    this.scale = 6; this.cx = 0; this.cz = 0; this.cache = []; this.levels = []; this.agents = null; this.hover = null; this.B = null;
    this.onClick = () => {}; this.onDbl = () => {}; this.onHover = () => {};
    new ResizeObserver(() => this.resize()).observe(canvas);
    canvas.addEventListener('pointerdown', (e) => { this.drag = { x: e.clientX, y: e.clientY, moved: 0 }; });
    canvas.addEventListener('pointermove', (e) => this.move(e));
    window.addEventListener('pointerup', (e) => {
      const d = this.drag; this.drag = null;
      if (d && d.moved < 5 && e.target === this.c) { const g = this.cellAt(e); if (g) this.onClick(g.x, g.z); }
    });
    canvas.addEventListener('pointerleave', () => { this.hover = null; this.onHover(null); this.draw(); });
    canvas.addEventListener('dblclick', (e) => { const g = this.cellAt(e); if (g) this.onDbl(g.x, g.z); });
    canvas.addEventListener('wheel', (e) => this.wheel(e), { passive: false });
  }
  resize() {
    const r = this.c.getBoundingClientRect(); const dpr = window.devicePixelRatio || 1;
    this.dpr = dpr; this.w = r.width; this.h = r.height;
    this.c.width = Math.max(1, r.width * dpr); this.c.height = Math.max(1, r.height * dpr);
    if ((this.needFit || !this.userMoved) && r.width) { this.needFit = false; this.fit(); }
    this.draw();
  }
  setBuilding(B, keepView = false) {
    this.B = B; const p = B.render.plan; [this.X, this.Z] = p.size; this.names = p.names; this.roles = p.roles;
    this.levels = p.levels.map((l) => ({ ...l, shape: b64(l.shape, Uint8Array), name: b64(l.name, Uint16Array) }));
    const N = B.render.node_count, xyz = view.nodeXYZ, lev = view.nodeLevel, X = this.X, Z = this.Z;
    this.elev = B.levels.map((l) => l.elevation - B.render.origin[1]);
    this.nodeAt = this.levels.map(() => new Int32Array(X * Z).fill(-1));
    this.between = this.levels.map(() => new Uint8Array(X * Z));
    for (let i = 0; i < N; i++) {
      const gx = Math.floor(xyz[i * 3]), gz = Math.floor(xyz[i * 3 + 2]), h = xyz[i * 3 + 1], k = gx * Z + gz;
      let L = lev[i];
      if (L >= 0 && L < this.levels.length) { this.nodeAt[L][k] = i; this.between[L][k] = 0; continue; }
      for (L = this.elev.length - 1; L >= 0 && !(h > this.elev[L]); L--);  // landings / half-levels: the floor below
      if (L < 0 || h - this.elev[L] > 4.5 || this.nodeAt[L][k] >= 0) continue;
      this.nodeAt[L][k] = i; this.between[L][k] = 1;
    }
    this.objects = this.levels.map((lv) => B.objects.filter((o) => o.y >= lv.feet_y - 1 && o.y <= lv.feet_y + 1));
    if (!keepView || this.level >= this.levels.length) this.level = Math.max(0, groundLevel(B));
    this.cache = [];
    if (!keepView) { if (this.w) this.fit(); else this.needFit = true; }
    this.draw();
  }
  invalidate() { this.cache = []; this.draw(); }
  fit() {
    if (!this.X || !this.w) return;
    this.userMoved = false;
    this.scale = clamp(Math.min((this.w - 40) / this.X, (this.h - 170) / this.Z), 1, 40);
    this.cx = this.X / 2; this.cz = this.Z / 2 - 60 / this.scale;
  }
  toScreen(gx, gz) { return [(gx - this.cx) * this.scale + this.w / 2, (gz - this.cz) * this.scale + this.h / 2]; }
  toGrid(sx, sy) { return [(sx - this.w / 2) / this.scale + this.cx, (sy - this.h / 2) / this.scale + this.cz]; }
  cellAt(e) {
    if (!this.B) return null;
    const r = this.c.getBoundingClientRect(); const [gx, gz] = this.toGrid(e.clientX - r.left, e.clientY - r.top);
    const x = Math.floor(gx), z = Math.floor(gz);
    return x >= 0 && z >= 0 && x < this.X && z < this.Z ? { x, z } : null;
  }
  move(e) {
    if (this.drag) {
      const dx = e.clientX - this.drag.x, dy = e.clientY - this.drag.y;
      this.drag.moved += Math.abs(dx) + Math.abs(dy);
      if (this.drag.moved >= 5) { this.cx -= dx / this.scale; this.cz -= dy / this.scale; this.userMoved = true; }
      this.drag.x = e.clientX; this.drag.y = e.clientY;
    }
    const g = this.cellAt(e); const key = g ? g.x * this.Z + g.z : null;
    if (key !== this.hover) { this.hover = key; this.onHover(g); }
    this.draw();
  }
  wheel(e) {
    e.preventDefault();
    const r = this.c.getBoundingClientRect(); const p = [e.clientX - r.left, e.clientY - r.top];
    const before = this.toGrid(...p);
    this.scale = clamp(this.scale * Math.exp(-e.deltaY * 0.0015), 1, 60);
    const after = this.toGrid(...p);
    this.cx += before[0] - after[0]; this.cz += before[1] - after[1]; this.userMoved = true; this.draw();
  }
  layer(L) {  // static image of one floor at K px per block (re-made when overlay, closures or the building change)
    if (this.cache[L]) return this.cache[L];
    const K = this.K, X = this.X, Z = this.Z;
    const cv = document.createElement('canvas'); cv.width = X * K; cv.height = Z * K;
    const g = cv.getContext('2d'); const lv = this.levels[L]; const at = this.nodeAt[L], btw = this.between[L];
    const mode = $('#overlay').value; const data = overlayData(mode);
    for (let x = 0; x < X; x++) {
      for (let z = 0; z < Z; z++) {
        const k = x * Z + z; const n = at[k];
        if (n >= 0) { g.fillStyle = planCellColor(n, mode, data, btw[k]); g.fillRect(x * K, z * K, K, K); }
        const s = SHAPES[lv.shape[k] & 7]; if (s === 'pass') continue;
        const glass = this.roles[lv.name[k]] === 'glass';
        let col = null, inset = 0;
        if (s === 'solid') col = glass ? PLAN_COL.glass : PLAN_COL.wall;
        else if (s === 'barrier') { col = glass ? PLAN_COL.glass : PLAN_COL.barrier; inset = K * 0.3; }
        else if (s === 'slab' && n < 0) col = PLAN_COL.slab;
        else if (s === 'stairs' && n < 0) { col = PLAN_COL.stairblock; inset = 1; }
        else if (s === 'obstacle') { col = PLAN_COL.furniture; inset = 1; }
        else if (s === 'door') { col = PLAN_COL.doorblock; inset = K * 0.3; }
        else if (s === 'liquid') col = PLAN_COL.liquid;
        if (col) { g.fillStyle = col; g.fillRect(x * K + inset, z * K + inset, K - 2 * inset, K - 2 * inset); }
      }
    }
    g.strokeStyle = 'rgba(0,0,0,0.5)'; g.lineWidth = 1;  // faint cell grid
    g.beginPath();
    for (let x = 0; x <= X; x++) { g.moveTo(x * K + 0.5, 0); g.lineTo(x * K + 0.5, Z * K); }
    for (let z = 0; z <= Z; z++) { g.moveTo(0, z * K + 0.5); g.lineTo(X * K, z * K + 0.5); }
    g.stroke();
    this.cache[L] = cv;
    return cv;
  }
  draw() {
    const g = this.ctx; if (!this.w) return;
    g.setTransform(this.dpr, 0, 0, this.dpr, 0, 0);
    g.fillStyle = PLAN_COL.bg; g.fillRect(0, 0, this.w, this.h);
    if (!this.B || !this.levels.length) {
      outlinedText(g, this.B ? 'No floors were detected in this building.' : 'Convert a building to see its floor plans.', this.w / 2, this.h / 2, '14px system-ui', UI.muted, 'center');
      return;
    }
    const L = this.level, s = this.scale, B = this.B, o = B.render.origin;
    const [ox, oy] = this.toScreen(0, 0);
    g.imageSmoothingEnabled = s < this.K;
    g.drawImage(this.layer(L), ox, oy, this.X * s, this.Z * s);
    const at = (wx, wz) => this.toScreen(wx - o[0], wz - o[2]);
    // furniture
    if (s >= 3) {
      for (const ob of this.objects[L]) {
        const st = OBJ_STYLE[ob.type]; if (!st) continue;
        const [sx, sy] = at(ob.x, ob.z); const m = s * 0.14;
        g.fillStyle = st[0]; g.beginPath(); g.roundRect(sx + m, sy + m, s - 2 * m, s - 2 * m, s * 0.18); g.fill();
        if (ob.type === 'chair' && ob.facing && (ob.facing[0] || ob.facing[1])) {  // back of the chair
          g.fillStyle = '#ffffff'; const fx = ob.facing[0], fz = ob.facing[1]; const bw = s * 0.16;
          const bx = fx > 0 ? sx + m : fx < 0 ? sx + s - m - bw : sx + m, bz = fz > 0 ? sy + m : fz < 0 ? sy + s - m - bw : sy + m;
          g.fillRect(bx, bz, fx ? bw : s - 2 * m, fz ? bw : s - 2 * m);
        }
        if (s >= 12) { g.fillStyle = '#fff'; g.font = `600 ${Math.round(s * 0.42)}px system-ui`; g.textAlign = 'center'; g.fillText(st[1], sx + s / 2, sy + s * 0.66); }
      }
    }
    // labels: rooms, exits, stairs
    if (s >= 7) {
      for (const r of B.rooms) if (r.level === L && r.area_m2 >= 10) { const [sx, sy] = at(r.x, r.z); outlinedText(g, r.name, sx, sy, '11px system-ui', UI.muted, 'center'); }
    }
    const placed = [];   // label boxes already drawn: later labels that would overlap are skipped
    const tag = (text, sx, sy, border, fill = UI.tag, color = UI.ink, force = false) => {
      g.font = '600 11px system-ui'; const w = g.measureText(text).width + 10;
      const r = [sx - w / 2, sy - 20, w, 16];
      if (!force && placed.some((q) => r[0] < q[0] + q[2] + 2 && q[0] < r[0] + r[2] + 2 && r[1] < q[1] + q[3] + 2 && q[1] < r[1] + r[3] + 2)) return;
      placed.push(r);
      g.fillStyle = fill; g.strokeStyle = border; g.lineWidth = 1.5;
      g.beginPath(); g.roundRect(r[0], r[1], w, 16, 4); g.fill(); g.stroke();
      g.fillStyle = color; g.textAlign = 'center'; g.fillText(text, sx, sy - 8);
    };
    const floorName = B.levels[L].name; const shut = shownClosed();
    for (const st of B.stairs) {   // closed things and staircases first, then exits from widest to narrowest
      if (!st.levels.includes(floorName)) continue;
      const [sx, sy] = at(st.x, st.z);
      if (shut.stairs.has(st.id)) tag(`✕ ${st.name} closed`, sx, sy, CLOSED_COL, CLOSED_COL, '#fff', true); else tag(st.name, sx, sy, UI.brand, UI.tag, UI.ink, true);
    }
    const exits = B.exits.filter((e) => nearestLevel(e.y) === L)
      .sort((a, b) => shut.exit.has(b.id) - shut.exit.has(a.id) || b.is_door - a.is_door || b.width_m - a.width_m);
    for (const e of exits) {
      const [sx, sy] = at(e.x, e.z);
      if (shut.exit.has(e.id)) tag(`✕ ${e.name} closed`, sx, sy, CLOSED_COL, CLOSED_COL, '#fff', true);
      else tag(e.name, sx, sy, '#ff6b6b');
    }
    // corrections
    for (const f of S.fixes) {
      if (f.kind === 'levels' || fixLevel(f) !== L) continue;
      const pending = !S.appliedKeys.has(fixKeyOf(f));
      const cells = f.cells || [[f.x, f.y, f.z]];
      for (const c of cells) {
        const [sx, sy] = at(Math.floor(c[0]), Math.floor(c[2]));
        g.strokeStyle = UI.brandDark; g.lineWidth = 2; g.setLineDash(pending ? [3, 2] : []);
        g.strokeRect(sx + 1, sy + 1, s - 2, s - 2); g.setLineDash([]);
      }
      const [sx, sy] = at(Math.floor(cells[0][0]), Math.floor(cells[0][2]));
      const rr = Math.max(7, s * 0.42);
      g.fillStyle = UI.brand; g.beginPath(); g.arc(sx + s / 2, sy - rr * 0.2, rr, 0, Math.PI * 2); g.fill();
      g.fillStyle = '#fff'; g.font = `700 ${Math.round(rr * 1.1)}px system-ui`; g.textAlign = 'center';
      g.fillText(FIX_INFO[f.kind][1], sx + s / 2, sy - rr * 0.2 + rr * 0.4);
    }
    // assembly point
    if (S.goal && S.goal.level === L) {
      const [sx, sy] = at(S.goal.x, S.goal.z);
      g.strokeStyle = GOAL_COL; g.lineWidth = 2.5; g.beginPath(); g.arc(sx, sy, Math.max(8, 1.5 * s), 0, Math.PI * 2); g.stroke();
      g.fillStyle = GOAL_COL; g.beginPath(); g.arc(sx, sy, 4, 0, Math.PI * 2); g.fill();
      tag('Assembly point', sx, sy - Math.max(8, 1.5 * s) + 4, GOAL_COL, GOAL_COL, '#fff');
    }
    // people (during playback)
    if (this.agents && S.sim) {
      const p = this.agents, css = S.sim.css, el = this.elev, rad = Math.max(1.6, 0.3 * s);
      for (let i = 0; i < css.length; i++) {
        const x = p[i * 3]; if (Number.isNaN(x)) continue;
        const h = p[i * 3 + 2]; let best = 0;
        for (let j = 1; j < el.length; j++) if (Math.abs(el[j] - h) < Math.abs(el[best] - h)) best = j;
        if (best !== L || Math.abs(el[best] - h) > 3) continue;
        const [sx, sy] = this.toScreen(x, p[i * 3 + 1]);
        g.fillStyle = css[i]; g.beginPath(); g.arc(sx, sy, rad, 0, Math.PI * 2); g.fill();
        if (rad > 3) { g.strokeStyle = 'rgba(0,0,0,0.35)'; g.lineWidth = 1; g.stroke(); }
      }
    }
    // hovered cell
    if (this.hover != null) {
      const x = Math.floor(this.hover / this.Z), z = this.hover % this.Z; const [sx, sy] = this.toScreen(x, z);
      g.strokeStyle = this.tool === 'look' ? UI.ink : UI.brand; g.lineWidth = 2; g.strokeRect(sx, sy, s, s);
    }
  }
}

