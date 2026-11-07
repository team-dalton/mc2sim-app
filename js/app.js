// Entry point: world, selection, building panel, floors, corrections, floor-plan tools, assembly point.
import { $, CAT, GOAL_COL, HEAT, TYPE_COL, UI, busy, busyNote, esc, store, toast } from './common.js';
import { api, initBackend, onStatus } from './backend.js';
import { wanted } from './local/worldfiles.js';
import { S, map, newClosed, plan, view } from './state.js';
import { FIX_INFO, OBJ_STYLE, PLAN_COL, SHAPES, SHAPE_WORD, TOOL_HINT, groundLevel } from './planview.js';
import { renderClosures, resultsTitle, updateLabels } from './sim.js';

map.onHover = (s) => { $('#hover').textContent = s; };
map.onSelect = (sel) => { setSelInputs(sel); };

export function tab(name) {
  S.tab = name;
  document.querySelectorAll('#tabs button').forEach((b) => b.classList.toggle('on', b.dataset.tab === name));
  $('#mapwrap').hidden = name !== 'map'; $('#planwrap').hidden = name !== 'plan'; $('#view3d').hidden = name !== '3d';
  $('#legend').hidden = name === 'map';
  if (name === 'map') map.resize(); else if (name === 'plan') plan.resize(); else view.resize();
  $('#hover').textContent = '';
  if (S.B) applyView();
}
document.querySelectorAll('#tabs button').forEach((b) => b.addEventListener('click', () => tab(b.dataset.tab)));

// ---- floor plan: floors, tools, inspection
export function renderPlanLevels() {
  const host = $('#planlevels'); host.innerHTML = '';
  if (!S.B) return;
  S.B.levels.forEach((L, i) => {
    const b = document.createElement('button'); b.textContent = L.name; b.title = `y ${L.elevation} · ${L.indoor_m2} m² indoors`;
    b.classList.toggle('on', i === plan.level);
    b.onclick = () => { plan.level = i; renderPlanLevels(); plan.draw(); };
    host.appendChild(b);
  });
  const fit = document.createElement('button'); fit.textContent = 'Fit'; fit.className = 'ghost'; fit.title = 'Show the whole floor';
  fit.onclick = () => { plan.fit(); plan.draw(); }; host.appendChild(fit);
}
export function setTool(t) {
  plan.tool = t;
  document.querySelectorAll('#tools button').forEach((b) => b.classList.toggle('on', b.dataset.tool === t));
  $('#plan').classList.toggle('tool', t !== 'look');
  $('#toolhint').textContent = TOOL_HINT[t] || '';
}
document.querySelectorAll('#tools button').forEach((b) => b.addEventListener('click', () => setTool(b.dataset.tool)));
setTool('look');

export function planCell(x, z) {  // everything known about one plan cell on the current floor
  const L = plan.level, k = x * plan.Z + z, o = S.B.render.origin, lv = plan.levels[L];
  const n = plan.nodeAt[L][k];
  const wx = x + o[0], wz = z + o[2];
  const objs = plan.objects[L].filter((ob) => ob.x === wx && ob.z === wz);
  const code = lv.shape[k];
  return { L, k, n, wx, wz, feetY: lv.feet_y, shape: SHAPES[code & 7], head: (code & 8) > 0 && (code & 7) > 0,
    block: plan.names[lv.name[k]], objs, h: n >= 0 ? view.nodeXYZ[n * 3 + 1] + o[1] : null };
}
plan.onHover = (g) => {
  if (!g || !S.B) { $('#planinfo').innerHTML = ''; return; }
  const c = planCell(g.x, g.z); const B = S.B; const parts = [`<b>x ${c.wx}, z ${c.wz}</b> · ${B.levels[c.L].name}`];
  if (c.n >= 0) {
    const f = view.nodeFlags[c.n], ex = view.nodeExit[c.n], st = view.nodeStair[c.n], rm = view.nodeRoom[c.n];
    let what = view.nodeKind[c.n] === 1 ? 'stairs' : view.nodeKind[c.n] === 2 ? `door (${B.doors[view.nodeDoor[c.n]]?.name || ''})` : 'floor';
    if (ex >= 0) what = `<span class="bad">${B.exits[ex].name}</span> threshold`;
    if (f & 1) what = 'open ground (safe)';
    parts.push(`${what}${f & 2 ? ', indoors' : ''}${rm >= 0 ? ` · ${esc(B.rooms[rm].name)}` : ''}${st >= 0 ? ` · ${esc(B.stairs[st].label)}` : ''} · stand at y ${c.h}`);
  } else parts.push('not walkable');
  if (c.shape !== 'pass' || !/air$/.test(c.block)) parts.push(`block at body height: ${esc(c.block.replace(/_/g, ' '))} (${SHAPE_WORD[c.shape]})`);
  for (const ob of c.objs) parts.push(`detected: <b>${ob.type}</b>${ob.corrected ? ' (your correction)' : ''}`);
  for (const f of fixesAt(c.wx, c.wz, c.L)) parts.push(`<span style="color:${UI.brandDark}">correction: ${FIX_INFO[f.kind][0]}</span>`);
  $('#planinfo').innerHTML = parts.join('<br>');
};
plan.onDbl = (x, z) => {  // stand there in the 3D view (spectator)
  if (plan.tool !== 'look' || !S.B) return;
  const c = planCell(x, z); const y = c.n >= 0 ? view.nodeXYZ[c.n * 3 + 1] : plan.elev[c.L];
  tab('3d'); setCut(view.size[1]); markChip(document.querySelector('#levels button'));
  view.setFly(true, { x: x + 0.5, y: y + 1.62, z: z + 0.5 });
};
plan.onClick = (x, z) => {
  if (!S.B) return;
  const t = plan.tool; const c = planCell(x, z); const B = S.B;
  if (t === 'look') return;
  if (t === 'erase') {
    const hit = fixesAt(c.wx, c.wz, c.L);
    if (!hit.length) { toast('There is no correction on this cell.', true); return; }
    S.fixes = S.fixes.filter((f) => !hit.includes(f)); fixesChanged(); return;
  }
  if (t === 'goal') {
    if (c.n < 0 || !(view.nodeFlags[c.n] & 1)) { toast('Pick a spot on the green open ground outside the building.', true); return; }
    setGoal({ x: c.wx + 0.5, z: c.wz + 0.5 }); return;
  }
  if (t === 'exit') {
    if (c.n < 0) { toast('Pick a walkable cell (people must be able to stand there).', true); return; }
    addFix({ kind: 'exit', x: c.wx, y: c.h, z: c.wz }); return;
  }
  if (t === 'not_exit') {
    const ex = c.n >= 0 ? view.nodeExit[c.n] : -1;
    if (ex < 0) { toast("That cell isn't a detected exit — pick a red exit cell.", true); return; }
    const o = B.render.origin; const cells = [];
    for (let i = 0; i < view.nodeExit.length; i++) if (view.nodeExit[i] === ex) cells.push([Math.floor(view.nodeXYZ[i * 3]) + o[0], view.nodeXYZ[i * 3 + 1] + o[1], Math.floor(view.nodeXYZ[i * 3 + 2]) + o[2]]);
    addFix({ kind: 'not_exit', x: cells[0][0], y: cells[0][1], z: cells[0][2], cells, label: B.exits[ex].name }); return;
  }
  // block corrections: the detected object here, else the block at body height
  const ob = c.objs[0];
  const y = ob ? ob.y : c.feetY + (c.head ? 1 : 0);
  if (t === 'stairs' && !(ob && ob.type === 'chair')) { toast('Pick a detected chair (green C) that is really part of a staircase.', true); return; }
  if (t === 'open' && !ob && c.shape === 'pass') { toast('Nothing blocks this cell already.', true); return; }
  addFix({ kind: t, x: c.wx, y, z: c.wz });
};

// ---- corrections (remembered per world area in this browser)
export const fixKeyOf = (f) => (f.kind === 'levels' ? `l:${(f.levels || []).map((l) => `${l.name || ''}@${l.elevation}`).join(';')}`
  : f.kind === 'not_exit' ? `n:${(f.cells || [[f.x, f.y, f.z]]).map((c) => `${c[0]},${c[2]}`).sort()[0]}`
    : f.kind === 'exit' ? `c:${f.x},${f.z},${Math.round(f.y)}` : `b:${f.x},${f.y},${f.z}`);
export function fixLevel(f) {
  const y = f.kind === 'exit' || f.kind === 'not_exit' ? f.y : f.y;
  let best = 0, d = Infinity;
  (S.B ? plan.levels : []).forEach((lv, i) => { const e = Math.min(Math.abs(lv.feet_y - y), Math.abs(S.B.levels[i].elevation - y)); if (e < d) { d = e; best = i; } });
  return best;
}
export function fixesAt(wx, wz, L) {
  return S.fixes.filter((f) => f.kind !== 'levels' && fixLevel(f) === L && (f.cells || [[f.x, f.y, f.z]]).some((c) => Math.floor(c[0]) === wx && Math.floor(c[2]) === wz));
}
export function addFix(f) {
  const key = fixKeyOf(f);
  S.fixes = S.fixes.filter((g) => fixKeyOf(g) !== key && !(f.kind !== 'not_exit' && g.kind !== 'not_exit' && g.x === f.x && g.z === f.z && fixLevel(g) === fixLevel(f)));
  S.fixes.push(f); fixesChanged();
}
export function fixesChanged() {
  if (S.areaKey) store.set('fixes.' + S.areaKey, S.fixes);
  renderFixes(); renderFloors(); plan.draw(); plan.onHover(null);
}
export function fixText(f) {
  if (f.kind === 'levels') return `Floor heights · ${(f.levels || []).map((l) => `${l.name || 'auto'} y ${l.elevation}`).join(', ')}`;
  return `${FIX_INFO[f.kind][0]} · ${f.label ? f.label : `x ${f.x}, z ${f.z}`}`;
}

// ---- floors: edit the height (and name) of each floor; stored as one 'levels' correction
export function floorsDraft() {
  const f = S.fixes.find((x) => x.kind === 'levels');
  return f ? f.levels.map((l) => ({ ...l })) : S.B.levels.map((l) => ({ name: l.name, elevation: l.elevation }));
}
export function setFloors(list, detected = false) {
  S.fixes = S.fixes.filter((x) => x.kind !== 'levels');
  if (!detected) {
    const clean = list.filter((l) => Number.isFinite(l.elevation)).sort((a, b) => a.elevation - b.elevation);
    S.fixes.push({ kind: 'levels', levels: clean });
  }
  fixesChanged(); renderFloors();
}
export function renderFloors() {
  if (!S.B) return;
  const draft = floorsDraft(); const sm = S.B.summary;
  const area = (e) => { const L = S.B.levels.find((l) => Math.abs(l.elevation - e) < 0.01); return L ? `${L.indoor_m2.toLocaleString()} m²` : 'new'; };
  const t = $('#floortable');
  t.innerHTML = '<tr><th>Name</th><th>Stand at y</th><th class="n">Indoors</th><th></th></tr>' + [...draft].reverse().map((l, ri) => {
    const i = draft.length - 1 - ri;
    return `<tr><td><input class="fname" data-i="${i}" value="${esc(l.name || '')}" placeholder="auto" maxlength="24"></td>
      <td><input class="fy" data-i="${i}" type="number" step="0.5" value="${l.elevation}"></td>
      <td class="n">${area(l.elevation)}</td><td class="n"><button class="ghost xs" data-del="${i}" title="Remove this floor">✕</button></td></tr>`;
  }).join('');
  t.querySelectorAll('input.fname').forEach((el) => el.addEventListener('change', () => { const d = floorsDraft(); d[+el.dataset.i].name = el.value.trim(); setFloors(d); }));
  t.querySelectorAll('input.fy').forEach((el) => el.addEventListener('change', () => {
    const v = Number(el.value); if (!Number.isFinite(v)) { renderFloors(); return; }
    const d = floorsDraft(); d[+el.dataset.i].elevation = Math.round(v * 2) / 2; setFloors(d);
  }));
  t.querySelectorAll('button[data-del]').forEach((el) => el.addEventListener('click', () => {
    const d = floorsDraft(); if (d.length <= 1) { toast('A building needs at least one floor.', true); return; }
    d.splice(+el.dataset.del, 1); d.forEach((l) => { if (/^(B?\d+F|B\d+)$/.test(l.name || '')) l.name = ''; }); setFloors(d);
  }));
  const used = new Set(draft.map((l) => l.elevation));
  const sh = (sm.standing_heights || []).filter(([y]) => !used.has(y));
  $('#heights').innerHTML = sh.length ? 'Common standing heights (click to add a floor there): '
    + sh.map(([y, c]) => `<button class="ghost xs" data-y="${y}">y ${y} · ${c.toLocaleString()} m²</button>`).join(' ') : '';
  $('#heights').querySelectorAll('button[data-y]').forEach((el) => el.addEventListener('click', () => {
    const d = floorsDraft(); d.push({ name: '', elevation: Number(el.dataset.y) });
    d.forEach((l) => { if (/^(B?\d+F|B\d+)$/.test(l.name || '')) l.name = ''; }); setFloors(d);
  }));
  const isDetected = !S.fixes.some((x) => x.kind === 'levels');
  $('#floor-reset').disabled = isDetected;
}
$('#floor-add').onclick = () => {
  if (!S.B) return;
  const d = floorsDraft(); const top = d.length ? Math.max(...d.map((l) => l.elevation)) : (S.B.summary.ground_h ?? 0);
  d.push({ name: '', elevation: top + 5 });
  d.forEach((l) => { if (/^(B?\d+F|B\d+)$/.test(l.name || '')) l.name = ''; });  // automatic names are re-numbered
  setFloors(d);
};
$('#floor-reset').onclick = () => setFloors([], true);
export function renderFixes() {
  const host = $('#fixlist'); host.innerHTML = '';
  for (const f of S.fixes) {
    const b = document.createElement('button'); const pending = !S.appliedKeys.has(fixKeyOf(f));
    b.className = 'fix' + (pending ? ' pending' : ''); b.textContent = `${fixText(f)} ✕`;
    b.title = pending ? 'Not applied yet — press Apply corrections. Click to remove.' : 'Applied. Click to remove.';
    b.onclick = () => { S.fixes = S.fixes.filter((g) => g !== f); fixesChanged(); };
    host.appendChild(b);
  }
  const now = [...S.fixes.map(fixKeyOf)].sort().join('|'), applied = [...S.appliedKeys].sort().join('|');
  const changed = now !== applied;
  $('#fix-apply').disabled = !changed;
  $('#fix-apply').textContent = changed ? `Apply corrections (${S.fixes.length})` : 'Corrections applied';
}
$('#fix-apply').onclick = () => busy('Re-reading the building with your corrections…', async () => {
  loadBuilding(await api.reanalyze({ id: S.B.id, fixes: S.fixes }), { keep: true });
  toast('Corrections applied. Earlier simulation results were cleared because the building changed.', true);
});
$('#fix-plan').onclick = () => tab('plan');

// ---- assembly point (remembered per world area)
export function setGoal(g, quiet = false) {
  S.goal = null;
  if (g && S.B) {
    const o = S.B.render.origin; const x = Math.floor(g.x) - o[0], z = Math.floor(g.z) - o[2];
    let found = null;
    if (x >= 0 && z >= 0 && x < plan.X && z < plan.Z) {
      plan.nodeAt.forEach((at, L) => { const n = at[x * plan.Z + z]; if (!found && n >= 0 && (view.nodeFlags[n] & 1)) found = { n, L }; });
    }
    if (found) S.goal = { x: Math.floor(g.x) + 0.5, z: Math.floor(g.z) + 0.5, y: view.nodeXYZ[found.n * 3 + 1] + o[1], level: found.L };
    else if (!quiet) toast('The saved assembly point is not on open ground in this area any more — set it again.', true);
  }
  if (S.areaKey) store.set('goal.' + S.areaKey, S.goal ? { x: S.goal.x, z: S.goal.z } : null);
  const o = S.B ? S.B.render.origin : [0, 0, 0];
  view.setGoal(S.goal ? { x: S.goal.x - o[0], y: S.goal.y - o[1], z: S.goal.z - o[2] } : null);
  $('#goalinfo').innerHTML = S.goal ? `<b>x ${Math.floor(S.goal.x)}, z ${Math.floor(S.goal.z)}</b> — once outside, everyone walks here and gathers.`
    : 'None — people stop as soon as they are outside.';
  $('#goal-clear').disabled = !S.goal;
  $('#goal-set').textContent = S.goal ? 'Move on floor plan' : 'Set on floor plan';
  plan.draw(); updateLabels(); resultsTitle();
}
$('#goal-set').onclick = () => {
  if (!S.B) return;
  plan.level = Math.max(0, groundLevel(S.B)); renderPlanLevels(); tab('plan'); setTool('goal');
};
$('#goal-clear').onclick = () => setGoal(null);

// ---- spectator camera buttons + help line
view.onFlyChange = () => {
  $('#cam-orbit').classList.toggle('on', !view.fly); $('#cam-fly').classList.toggle('on', view.fly);
  const hud = $('#flyhud'); hud.hidden = !view.fly;
  if (view.fly) {
    const locked = document.pointerLockElement === view.renderer.domElement;
    hud.innerHTML = `<b>Spectator</b> · W A S D move · Space up · Shift down · R faster · speed ${view.speed.toFixed(0)} m/s (scroll) · `
      + (locked ? 'Esc frees the mouse' : 'click or drag to look around · Esc: back to orbit');
  }
};
$('#cam-orbit').onclick = () => view.setFly(false);
$('#cam-fly').onclick = () => view.setFly(true);

export function setMode(m) {
  map.mode = m; $('#mode-pan').classList.toggle('on', m === 'pan'); $('#mode-select').classList.toggle('on', m === 'select');
  $('#map').classList.toggle('selecting', m === 'select');
}
$('#mode-pan').onclick = () => setMode('pan');
$('#mode-select').onclick = () => setMode('select');
$('#goto-player').onclick = () => { if (map.player) { map.cx = map.player[0]; map.cz = map.player[2]; map.scale = Math.max(map.scale, 3); map.draw(); } };

export function setSelInputs(sel) {
  if (!sel) return;
  for (const k of ['x0', 'z0', 'x1', 'z1']) $('#' + k).value = sel[k];
  selInfo();
  if (S.world) store.set('sel.' + S.world, sel);
}
export function readSel() {
  const v = ['x0', 'z0', 'x1', 'z1'].map((k) => parseInt($('#' + k).value, 10));
  if (v.some(Number.isNaN)) return null;
  return { x0: Math.min(v[0], v[2]), z0: Math.min(v[1], v[3]), x1: Math.max(v[0], v[2]), z1: Math.max(v[1], v[3]) };
}
export function selInfo() {
  const s = readSel();
  $('#selinfo').textContent = s ? `${s.x1 - s.x0 + 1} × ${s.z1 - s.z0 + 1} blocks selected (+6 blocks of surroundings are added automatically).` : '';
}
['x0', 'z0', 'x1', 'z1'].forEach((k) => $('#' + k).addEventListener('change', () => { map.sel = readSel(); map.draw(); selInfo(); }));

export async function loadWorlds() {
  const res = await api.worlds();
  const sel = $('#world'); sel.innerHTML = '';
  for (const w of res.worlds) {
    const o = document.createElement('option'); o.value = w.name;
    o.textContent = `${w.name}  (${w.version}, ${w.size_mb} MB)`; if (!w.regions) o.disabled = true; sel.appendChild(o);
  }
  const last = store.get('world', null);
  if (last && res.worlds.some((w) => w.name === last)) sel.value = last;
  if (!res.worlds.length) $('#worldinfo').textContent = `No worlds found in ${res.saves}. Set MC_SAVES to your saves folder.`;
}

$('#open').onclick = () => busy('Copying the save and reading the map…', async () => {
  const name = $('#world').value;
  showWorld(name, await api.open(name, $('#refresh').checked));
});
function showWorld(name, res) {
  S.world = name; store.set('world', name);
  const center = res.player || res.spawn;
  map.setWorld(name, res.regions, center, res.player);
  const sel = store.get('sel.' + name, null);
  if (sel) { map.sel = sel; setSelInputs(sel); map.cx = (sel.x0 + sel.x1) / 2; map.cz = (sel.z0 + sel.z1) / 2; map.scale = 3; }
  map.draw();
  const p = res.player ? `player at ${res.player.map((v) => Math.round(v)).join(', ')}` : 'no player position';
  $('#worldinfo').textContent = `${res.info.version} · ${res.regions.length} regions · ${p}`;
  $('#s-select').classList.remove('disabled'); tab('map');
}

// ---- browser-only mode: the world is a folder or .zip picked here, read in this tab and never uploaded
function setupLocalWorlds() {
  $('#world').closest('.row').style.display = 'none'; $('#refresh').closest('label').style.display = 'none';   // CSS display beats [hidden]
  $('#localworld').hidden = false;
  $('#worldinfo').textContent = 'Everything runs in this browser tab: the world you pick is read on this computer and never uploaded.';
  const openLocal = (source) => busy('Reading the world in this browser tab…', async () => {
    await api.ready;
    const res = await api.open(source);
    showWorld(res.name, res);
  });
  $('#pick-folder').onclick = async () => {
    if (!window.showDirectoryPicker) { $('#folder-input').click(); return; }
    let handle;
    try { handle = await window.showDirectoryPicker({ id: 'mc2sim-world', mode: 'read' }); } catch (e) { if (e.name !== 'AbortError') toast(e.message); return; }
    openLocal({ kind: 'dir', handle });
  };
  $('#folder-input').onchange = (e) => {
    const files = [...e.target.files].filter((f) => wanted(f.webkitRelativePath)).map((f) => ({ path: f.webkitRelativePath, file: f }));
    e.target.value = '';
    if (files.length) openLocal({ kind: 'files', files }); else toast('No Minecraft world in that folder: pick the folder that contains level.dat.');
  };
  $('#pick-zip').onclick = () => $('#zip-input').click();
  $('#zip-input').onchange = (e) => {
    const f = e.target.files[0]; e.target.value = '';
    if (f) openLocal({ kind: 'zip', file: f, name: f.name.replace(/\.zip$/i, '') });
  };
  onStatus((text) => {
    const ready = text === 'ready';
    $('#pystatus').textContent = ready ? 'Simulator ready — running in this browser tab.'
      : text.startsWith('error') ? 'The in-browser simulator could not start: ' + text.replace(/^error:? ?/, '') : text;
    $('#pystatus').classList.toggle('bad', text.startsWith('error'));
    if (!$('#busy').hidden && !ready) busyNote(text);
  });
}

$('#convert').onclick = () => {
  const s = readSel(); if (!s) { toast('Select an area on the map first (Shift + drag).'); return; }
  const key = `${S.world}|${s.x0},${s.z0},${s.x1},${s.z1}`;
  const fixes = store.get('fixes.' + key, []);
  const body = { world: S.world, ...s, ymin: $('#ymin').value || null, ymax: $('#ymax').value || null, fixes };
  busy('Reading blocks and detecting floors, rooms, doors, stairs and furniture…', async () => {
    const B = await api.convert(body);
    S.areaKey = key; loadBuilding(B);
    if (fixes.length) toast(`Applied ${fixes.length} saved correction${fixes.length > 1 ? 's' : ''} for this area.`, true);
  });
};
$('#demo').onclick = () => busy('Building the demo…', async () => {
  const fixes = store.get('fixes.demo', []);
  const B = await api.demo({ fixes });
  S.areaKey = 'demo'; loadBuilding(B);
});

export function kpi(v, label, cls = '') { return `<div class="kpi ${cls}"><b>${v}</b><span>${label}</span></div>`; }

export function loadBuilding(B, opts = {}) {
  if (!B) return;
  const keep = !!opts.keep;
  S.B = B; S.sim = null; S.frames = null; S.playing = false; plan.agents = null;
  view.build(B, keep);
  plan.setBuilding(B, keep);
  renderPlanLevels();
  S.fixes = (B.fixes || []).map((f) => ({ ...f }));
  S.appliedKeys = new Set(S.fixes.map(fixKeyOf));
  renderFixes(); renderFloors();
  const sm = B.summary;
  $('#bsummary').innerHTML = kpi(sm.levels.length, 'floors') + kpi(sm.rooms, 'rooms') + kpi(sm.indoor_m2.toLocaleString(), 'm² indoors')
    + kpi(sm.doors, 'doors') + kpi(sm.exits, 'exits to outside') + kpi(sm.seats, 'seats');
  const lv = $('#levels'); lv.innerHTML = '';
  const all = document.createElement('button'); all.textContent = 'All floors'; all.className = 'on'; lv.appendChild(all);
  all.onclick = () => { setCut(view.size[1]); markChip(all); };
  for (const L of B.levels) {
    const b = document.createElement('button'); b.textContent = `${L.name} · y ${L.elevation}`;
    b.title = `${L.indoor_m2} m² indoors`;
    b.onclick = () => { setCut(L.elevation - B.render.origin[1] + 2.2); markChip(b); };
    lv.appendChild(b);
  }
  $('#warnings').innerHTML = (sm.warnings || []).map((w) => `<p class="warn">⚠ ${w}</p>`).join('');
  const objs = Object.entries(sm.objects).map(([k, v]) => `${v} ${k}`).join(', ') || 'none';
  const rooms = [...B.rooms].sort((a, b) => b.area_m2 - a.area_m2).slice(0, 60);
  const levelName = (i) => (B.levels[i] ? B.levels[i].name : '?');
  $('#details').innerHTML = `<p class="hint">Furniture: ${objs}.<br>Signs read: ${B.signs.length}. Converted in ${B.convert_s} s.</p>
    <table><tr><th>Room</th><th>Floor</th><th class="n">m²</th><th class="n">Seats</th></tr>
    ${rooms.map((r) => `<tr><td>${r.name}</td><td>${levelName(r.level)}</td><td class="n">${r.area_m2}</td><td class="n">${r.seats}</td></tr>`).join('')}</table>`;
  const cut = $('#cut'); cut.max = view.size[1]; cut.value = view.size[1]; setCut(view.size[1]);
  $('#overlay').value = 'type';
  $('#overlay').querySelector('[value=density]').disabled = true; $('#overlay').querySelector('[value=congestion]').disabled = true;
  applyView();
  $('#people').placeholder = 'auto: seats + 1 per 12 m²';
  $('#results').hidden = true;
  ['#s-building', '#s-sim'].forEach((s) => $(s).classList.remove('disabled'));
  document.querySelector('#tabs [data-tab="3d"]').disabled = false;
  document.querySelector('#tabs [data-tab="plan"]').disabled = false;
  S.closed = newClosed(); S.use = null; S.stress = null; $('#stressout').hidden = true;
  setGoal(S.areaKey ? store.get('goal.' + S.areaKey, null) : null);
  renderClosures();
  tab(keep ? S.tab : '3d');
  if (sm.warnings && sm.warnings.length) toast(sm.warnings[0]);
}
export function markChip(btn) { document.querySelectorAll('#levels button').forEach((b) => b.classList.toggle('on', b === btn)); }
export function setCut(y) {
  $('#cut').value = y; view.setCut(Number(y));
  $('#cutval').textContent = S.B ? `(world y ${Math.round(Number(y) + S.B.render.origin[1])})` : '';
}
$('#cut').addEventListener('input', (e) => { setCut(e.target.value); markChip(null); });
$('#blockmode').onchange = applyView;
$('#overlay').onchange = applyView;

export function overlayData(mode) {
  if (!S.sim || (mode !== 'density' && mode !== 'congestion')) return null;
  const values = mode === 'density' ? S.sim.density : S.sim.congestion;
  return { values, max: mode === 'density' ? 5 : Math.max(10, ...values) };
}
export function applyView() {
  view.setBlockMode($('#blockmode').value);
  const mode = $('#overlay').value;
  const data = overlayData(mode);
  view.setOverlay(mode, data);
  plan.invalidate();
  legend(mode, data);
}
export function legend(mode, data) {
  const L = $('#legend');
  const sw = (c, t) => `<div><span class="swatch" style="background:${c}"></span>${t}</div>`;
  if (mode === 'type') {
    L.innerHTML = sw(TYPE_COL.indoor, 'Indoor floor') + sw(TYPE_COL.stairs, 'Stairs') + sw(TYPE_COL.door, 'Door')
      + sw(TYPE_COL.exit, 'Exit threshold') + sw(TYPE_COL.safe, 'Safe outdoor area') + sw(TYPE_COL.outdoor, 'Other outdoor');
  } else if (data) {
    const title = mode === 'density' ? 'Highest crowd density (people/m²)' : 'Time at ≥ 2 people/m² (s)';
    L.innerHTML = `<div>${title}</div><div class="ramp" style="background:linear-gradient(90deg,${HEAT.join(',')})"></div>
      <div class="ends"><span>0</span><span>${mode === 'density' ? '5+' : Math.round(data.max)}</span></div>`;
  } else if (mode === 'floor' && S.B) {
    L.innerHTML = S.B.levels.map((lv, i) => sw(CAT[i % CAT.length], `${lv.name} (y ${lv.elevation})`)).join('');
  } else L.innerHTML = '';
  if (S.tab === 'plan') {
    if (mode === 'type') {
      L.innerHTML = sw(PLAN_COL.indoor, 'Indoor floor') + sw(PLAN_COL.stairs, 'Stairs') + sw(PLAN_COL.door, 'Doorway')
        + sw(PLAN_COL.exit, 'Exit threshold') + sw(PLAN_COL.safe, 'Open ground (safe)') + sw(PLAN_COL.outdoor, 'Other outdoor');
    }
    L.innerHTML += sw(PLAN_COL.wall, 'Wall at body height') + sw(PLAN_COL.glass, 'Glass') + sw(OBJ_STYLE.chair[0], 'Chair (white bar = back)')
      + sw(OBJ_STYLE.table[0], 'Table') + sw(UI.brand, 'Your correction') + sw(GOAL_COL, 'Assembly point');
  }
}

$('#dl-building').onclick = () => { if (S.B) busy('Preparing building.json…', () => api.download('building', S.B.id)); };
$('#dl-results').onclick = () => { if (S.sim) busy('Preparing results.json…', () => api.download('results', S.sim.sim_id)); };


// ---- start
initBackend().then((mode) => (mode === 'local' ? setupLocalWorlds() : loadWorlds())).catch((e) => toast(e.message));
map.draw();
window.mc2sim = { S, map, plan, view };  // handle for debugging from the browser console
