// Closures, evacuation runs, one-at-a-time test, results, chart and playback.
import * as THREE from 'three';
import { $, CLOSED_COL, EXIT_COL, GOAL_COL, UI, b64, busy, busyNote, esc, fmt } from './common.js';
import { api } from './backend.js';
import { S, newClosed, plan, view } from './state.js';
import { applyView, kpi, markChip, setCut } from './app.js';

// -------------------------------------------------------------- scenarios ---
export const KINDS = { exit: 'exits', door: 'doors', stairs: 'stairs' };
export const itemName = (type, id) => { const it = S.B[KINDS[type]][id]; return it ? (type === 'stairs' ? it.label : it.name) : `${type} ${id + 1}`; };
export function lvName(y) {
  const L = (S.B.levels || []).reduce((b, l) => (!b || Math.abs(l.elevation - y) < Math.abs(b.elevation - y) ? l : b), null);
  return L ? L.name : '';
}
export function closedList() {
  const out = [];
  for (const type of ['exit', 'stairs', 'door']) for (const id of [...S.closed[type]].sort((a, b) => a - b)) out.push({ type, id });
  return out;
}
export const closureKey = (list) => list.map((c) => `${c.type}:${c.id}`).sort().join(',');
export function toggleClosure(type, id, on) {
  const set = S.closed[type]; const want = on ?? !set.has(id);
  if (want) set.add(id); else set.delete(id);
  renderClosures();
}
export function closedNodes() {
  const v = view; const N = v.walk ? v.walk.count : 0; const out = [];
  for (let i = 0; i < N; i++) {
    if ((v.nodeExit[i] >= 0 && S.closed.exit.has(v.nodeExit[i])) || (v.nodeDoor[i] >= 0 && S.closed.door.has(v.nodeDoor[i]))
      || (v.nodeStair[i] >= 0 && S.closed.stairs.has(v.nodeStair[i]))) out.push(i);
  }
  return out;
}
export function renderClosures() {
  if (!S.B) return;
  const host = $('#closed'); host.innerHTML = '';
  for (const c of closedList()) {
    const b = document.createElement('button'); b.className = 'closed'; b.textContent = `${itemName(c.type, c.id)} ✕`;
    b.title = 'Open it again'; b.onclick = () => toggleClosure(c.type, c.id, false); host.appendChild(b);
  }
  buildPicker();
  view.setClosed(closedNodes());
  applyView();
  updateLabels();
  resultsTitle();
}
export function buildPicker() {
  const B = S.B; const use = S.use || { exits: new Map(), stairs: new Map() };
  const opt = (v, t) => `<option value="${v}">${esc(t)}</option>`;
  const people = (n) => (n ? ` · ${n} people` : '');
  const exits = B.exits.filter((e) => !S.closed.exit.has(e.id))
    .sort((a, b) => (use.exits.get(b.id) || 0) - (use.exits.get(a.id) || 0) || b.width_m - a.width_m);
  const stairs = B.stairs.filter((s) => !S.closed.stairs.has(s.id));
  const doors = B.doors.filter((d) => !d.is_exit && !S.closed.door.has(d.id));
  let h = opt('', 'Close an exit, door or staircase…');
  h += '<optgroup label="Exits to outside">' + exits.map((e) => opt(`exit:${e.id}`,
    `${e.name} · ${e.width_m} m ${e.is_door ? 'door' : 'opening'} · ${lvName(e.y)}${people(use.exits.get(e.id))}`)).join('') + '</optgroup>';
  if (stairs.length) h += '<optgroup label="Staircases">' + stairs.map((s) => opt(`stairs:${s.id}`, s.label + people(use.stairs.get(s.id)))).join('') + '</optgroup>';
  if (doors.length) h += '<optgroup label="Doors inside">' + doors.map((d) => opt(`door:${d.id}`, `${d.name} · ${lvName(d.y)} · ${d.block.replace(/_/g, ' ')}, ${d.width_m} m`)).join('') + '</optgroup>';
  $('#close-pick').innerHTML = h;
}
$('#close-pick').onchange = (e) => { const v = e.target.value; if (!v) return; const [t, id] = v.split(':'); toggleClosure(t, Number(id), true); };

export function updateLabels() {
  const B = S.B; if (!B) return;
  const o = B.render.origin; const L = [];
  const at = (it) => ({ x: it.x - o[0], y: it.y - o[1], z: it.z - o[2] });
  const used = S.sim ? new Map(S.sim.stats.exits.filter((e) => e.people > 0).map((e) => [e.id, e.people])) : null;
  let exits = used ? B.exits.filter((e) => used.has(e.id))
    : B.exits.filter((e) => e.is_door).sort((a, b) => b.width_m - a.width_m).slice(0, 12);
  for (const id of S.closed.exit) if (!exits.some((e) => e.id === id)) exits.push(B.exits[id]);
  for (const e of exits) {
    const shut = S.closed.exit.has(e.id);
    L.push({ ...at(e), text: shut ? `✕ ${e.name} closed` : used ? `${e.name} · ${used.get(e.id) || 0}` : e.name,
      cls: shut ? 'closed' : 'exit', title: shut ? 'Click to open it again' : 'Click to close this exit', onClick: () => toggleClosure('exit', e.id) });
  }
  const sUse = S.sim ? new Map((S.sim.stats.stairs || []).map((s) => [s.id, s.people])) : null;
  for (const s of B.stairs) {
    const shut = S.closed.stairs.has(s.id);
    L.push({ ...at(s), text: shut ? `✕ ${s.name} closed` : sUse ? `${s.name} · ${sUse.get(s.id) || 0}` : s.name,
      cls: shut ? 'closed' : 'stairs', title: shut ? 'Click to open it again' : `${s.label} — click to close it`, onClick: () => toggleClosure('stairs', s.id) });
  }
  for (const id of S.closed.door) {
    const d = B.doors[id];
    L.push({ ...at(d), text: `✕ ${d.name} closed`, cls: 'closed', title: 'Click to open it again', onClick: () => toggleClosure('door', id) });
  }
  if (S.goal) L.push({ x: S.goal.x - o[0], y: S.goal.y - o[1] + 1.6, z: S.goal.z - o[2], text: 'Assembly point', cls: 'goal' });
  view.setLabels(L);
}

export function simBody() {
  const num = (id) => { const v = $('#' + id).value; return v === '' ? null : Number(v); };
  return {
    id: S.B.id, people: num('people'), placement: $('#placement').value, premove_max: num('premove_max'),
    speed_mean: num('speed_mean'), stair_factor: num('stair_factor'), seed: num('seed'),
    reroute_s: $('#reroute').checked ? 5 : 0, awareness: $('#awareness').value,
    closed_exits: [...S.closed.exit], closed_doors: [...S.closed.door], closed_stairs: [...S.closed.stairs],
    goal: S.goal ? [S.goal.x, S.goal.z] : null,
  };
}
export async function runJob(kind, body, title) {
  $('#busytext').dataset.title = title;
  return api.job(kind, body, busyNote);
}

// ------------------------------------------------------------- simulation ---
$('#run').onclick = () => busy('Simulating the evacuation…', async () => {
  const res = await runJob('simulate', simBody(), 'Simulating the evacuation…');
  await loadSim(res);
  markStressRow(res.sim_id);
});
$('#stress').onclick = () => busy('Closing each main exit and staircase in turn…', async () => {
  S.stress = await runJob('stress', simBody(), 'Closing each main exit and staircase in turn…');
  showStress();
  if (S.stress.rows.length) await openStressRow(0);
  else await openStressRow(-1);
});

export async function loadSim(res) {
  S.frames = new Uint16Array(await api.frames(res.sim_id));   // 16-bit positions; × res.frame_step = metres, 65535 = hidden
  S.sim = res;
  res.density = b64(res.node_max_density, Float32Array);
  res.congestion = b64(res.node_congested_s, Float32Array);
  res.exitOf = b64(res.exit_of, Int16Array);
  res.statusArr = b64(res.status, Int8Array);
  res.T = Math.max(0, (res.frames - 1) * res.frame_dt);
  const exitIdx = new Map(res.stats.exits.filter((e) => e.people > 0).sort((a, b) => b.people - a.people).map((e, i) => [e.id, i]));
  res.exitColorIdx = exitIdx;
  res.colors = Array.from(res.exitOf, (e, i) => new THREE.Color(res.statusArr[i] === 1 ? CLOSED_COL
    : res.statusArr[i] === 3 ? '#9aa0a6' : e >= 0 && exitIdx.has(e) ? EXIT_COL[exitIdx.get(e) % EXIT_COL.length] : '#9aa0a6'));
  res.css = res.colors.map((c) => '#' + c.getHexString());
  // the scenario on screen is the one that was run
  const sc = res.stats.scenario;
  S.closed = newClosed(); for (const c of sc.closed) S.closed[c.type].add(c.id);
  $('#awareness').value = sc.awareness;
  if (!sc.closed.length) {
    S.use = { exits: new Map(res.stats.exits.map((e) => [e.id, e.people])), stairs: new Map((res.stats.stairs || []).map((s) => [s.id, s.people])) };
  }
  showResults(res);
  renderClosures();
  S.t = 0; S.playing = res.T > 0; $('#play').textContent = S.playing ? '❚❚' : '▶';
  $('#time').max = res.T;
  $('#overlay').querySelector('[value=density]').disabled = false; $('#overlay').querySelector('[value=congestion]').disabled = false;
}

export function resultsTitle() {
  if (!S.sim) return;
  const sc = S.sim.stats.scenario;
  const names = sc.closed.map((c) => itemName(c.type, c.id));
  let h = names.length
    ? `Closed: ${esc(names.join(', '))} <span class="hint">· people find out ${sc.awareness === 'known' ? 'before they set off' : 'when they get there'}</span>`
    : 'Nothing closed';
  if (S.sim.stats.assembly) h += ' <span class="hint">· with assembly point</span>';
  const g = S.sim.params.goal; const goalNow = S.goal ? [S.goal.x, S.goal.z] : null;
  const goalChanged = JSON.stringify(g || null) !== JSON.stringify(goalNow);
  if (closureKey(closedList()) !== closureKey(sc.closed) || goalChanged) {
    h += `<br><span class="hint warn">${goalChanged ? 'Assembly point' : 'Closures'} changed since this run — press Run evacuation to update.</span>`;
  }
  $('#rtitle').innerHTML = h;
}

export function showStress() {
  const s = S.stress; $('#stressout').hidden = false;
  const b = s.baseline;
  const delta = (v) => (v == null ? '–' : `<span class="${v > 10 ? 'bad' : ''}">${v > 0 ? '+' : ''}${fmt(v)} s</span>`);
  const change = (r) => (r.no_way_out ? `<span class="bad">${r.no_way_out} can't get out</span>` : delta(r.delta_total_s));
  $('#stresstable').innerHTML = '<tr><th>Closed</th><th class="n">Everyone out</th><th class="n">Change</th><th class="n">90% out</th></tr>'
    + `<tr class="srow base" data-sim="${s.baseline_sim}"><td>Nothing closed</td><td class="n">${fmt(b.total_time_s)} s</td><td class="n"></td><td class="n">${fmt(b.t90_s)} s</td></tr>`
    + s.rows.map((r) => `<tr class="srow" data-sim="${r.sim_id}"><td>${esc(itemName(r.closed.type, r.closed.id))}</td>
      <td class="n">${r.no_way_out ? 'never' : fmt(r.total_time_s) + ' s'}</td><td class="n">${change(r)}</td><td class="n">${fmt(r.t90_s)} s</td></tr>`).join('');
  $('#stresstable').querySelectorAll('tr.srow').forEach((tr) => {
    tr.onclick = () => busy('Loading this scenario…', async () => { await loadSim(await api.sim(tr.dataset.sim)); markStressRow(tr.dataset.sim); });
  });
  const w = s.rows[0]; const aware = s.awareness === 'known' ? 'before they set off' : 'only when they get there';
  let sum;
  if (!w) sum = 'No exit or staircase carries enough people to be worth testing.';
  else if (w.no_way_out) sum = `Worst: closing <b>${esc(itemName(w.closed.type, w.closed.id))}</b> leaves <b class="bad">${w.no_way_out} of ${w.people} people</b> with no way out.`;
  else sum = `Worst: closing <b>${esc(itemName(w.closed.type, w.closed.id))}</b> — everyone out after ${fmt(w.total_time_s)} s instead of ${fmt(b.total_time_s)} s (${delta(w.delta_total_s)}).`;
  $('#stresssum').innerHTML = `${sum} ${s.rows.length} closures tested; people find out ${aware}.`;
}
export async function openStressRow(i) {
  const id = i < 0 ? S.stress.baseline_sim : S.stress.rows[i].sim_id;
  await loadSim(await api.sim(id));
  markStressRow(id);
}
export function markStressRow(simId) {
  document.querySelectorAll('#stresstable tr.srow').forEach((tr) => tr.classList.toggle('on', tr.dataset.sim === simId));
}

export function showResults(res) {
  const st = res.stats; $('#results').hidden = false;
  const stuck = st.trapped_no_route + st.not_out_by_end;
  const a = st.assembly;
  $('#rkpis').innerHTML = kpi(st.people, 'people') + kpi(fmt(st.total_time_s) + ' s', stuck ? 'last one out' : 'everyone out')
    + kpi(fmt(st.t90_s) + ' s', '90% out')
    + (a ? kpi(fmt(a.total_time_s) + ' s', a.reached === st.people ? 'all at assembly point' : 'last at assembly point', 'goal')
      : kpi(fmt(st.t50_s) + ' s', '50% out'))
    + kpi(stuck, 'no way out', stuck ? 'bad' : '') + kpi(fmt(st.mean_walk_m) + ' m', 'avg. walk to exit');
  $('#agentkey').innerHTML = 'People are coloured by the exit they use; <span class="bad">red</span> = no way out.'
    + (a ? ` <span class="good">Assembly point:</span> ${a.reached} of ${st.people} there, the last after ${fmt(a.total_time_s)} s`
      + ` (on average ${fmt(a.mean_extra_s)} s walk from the exit)${a.cannot_reach ? `; <span class="bad">${a.cannot_reach} came out where it cannot be reached</span> (grey)` : ''}.` : '');
  const c = res.compare; const cb = $('#compare');
  if (c) {
    const d = (v) => `<b class="${v > 10 ? 'bad' : v < -10 ? 'good' : ''}">${v > 0 ? '+' : ''}${fmt(v)} s</b>`;
    let h = '<b>Compared with nothing closed</b> (same people): ';
    if (c.no_way_out) h += `<span class="bad">${c.no_way_out} of ${c.people} people have no way out</span>`;
    else h += `everyone out ${d(c.delta_total_s)} (${fmt(c.baseline.total_time_s)} → ${fmt(c.total_time_s)} s), 90% out ${d(c.delta_t90_s)}`;
    if (st.turned_back) h += ` · ${st.turned_back} people headed for a closed spot and had to turn back`;
    cb.innerHTML = h; cb.hidden = false;
  } else cb.hidden = true;
  const used = st.exits.filter((e) => e.people > 0 || e.closed).sort((a, b) => b.people - a.people);
  $('#exits').innerHTML = '<tr><th>Exit</th><th class="n">People</th><th class="n">Width</th><th class="n">Last out</th><th class="n">Peak p/s</th></tr>'
    + used.map((e) => (e.closed
      ? `<tr class="isclosed"><td><span class="swatch" style="background:${CLOSED_COL}"></span>${e.name}</td><td class="n bad">closed</td><td class="n">${e.width_m} m</td><td class="n">–</td><td class="n">–</td></tr>`
      : `<tr><td><span class="swatch" style="background:${EXIT_COL[res.exitColorIdx.get(e.id) % EXIT_COL.length]}"></span>${e.name}</td>
      <td class="n">${e.people}</td><td class="n">${e.width_m} m</td><td class="n">${fmt(e.last_s)} s</td><td class="n">${fmt(e.peak_flow_ps, 1)}</td></tr>`)).join('');
  const stairs = (st.stairs || []);
  $('#stairsout').innerHTML = stairs.length
    ? '<tr><th>Staircase</th><th class="n">People</th></tr>' + stairs.map((s) => `<tr class="${s.closed ? 'isclosed' : ''}"><td>${esc(s.label)}</td>
      <td class="n ${s.closed ? 'bad' : ''}">${s.closed ? 'closed' : s.people}</td></tr>`).join('')
    : '<tr><td class="hint">No staircases between floors.</td></tr>';
  const anyStuck = st.levels.some((l) => l.trapped);
  $('#levelsout').innerHTML = `<tr><th>Floor</th><th class="n">People</th><th class="n">Empty after</th>${anyStuck ? '<th class="n">No way out</th>' : ''}</tr>`
    + st.levels.filter((l) => l.people_start > 0).map((l) => `<tr><td>${l.name}</td><td class="n">${l.people_start}</td>
      <td class="n">${l.trapped ? 'never' : fmt(l.clear_s) + ' s'}</td>${anyStuck ? `<td class="n ${l.trapped ? 'bad' : ''}">${l.trapped}</td>` : ''}</tr>`).join('');
  const hs = $('#hotspots'); hs.innerHTML = '';
  const seen = [];
  for (const h of st.hotspots) {
    if (seen.some((s) => Math.hypot(s.x - h.x, s.z - h.z) < 6 && Math.abs(s.y - h.y) < 2)) continue;
    seen.push(h); if (seen.length > 6) break;
    const li = document.createElement('li');
    const where = h.door >= 0 ? 'at a door' : h.room >= 0 ? `in ${S.B.rooms[h.room].name}` : 'in a passage';
    li.textContent = `${lvName(h.y)} ${where} (x ${Math.round(h.x)}, z ${Math.round(h.z)}) — ${fmt(h.congested_s)} s crowded, up to ${h.max_density} p/m²`;
    li.onclick = () => { const o = S.B.render.origin; view.focus(h.x - o[0], h.y - o[1], h.z - o[2]); setCut(h.y - o[1] + 2.2); markChip(null); };
    hs.appendChild(li);
  }
  drawChart();
}

export function drawChart() {
  const cv = $('#chart'); const g = cv.getContext('2d'); const W = cv.width, H = cv.height;
  g.clearRect(0, 0, W, H);
  if (!S.sim) return;
  const curve = S.sim.stats.curve; const P = S.sim.stats.people;
  const base = S.sim.compare ? S.sim.compare.baseline_curve : null;
  const asm = S.sim.stats.assembly ? S.sim.stats.assembly.curve : null;
  const end = (c) => (c && c.length ? c[c.length - 1][0] : 10);
  const T = Math.max(10, end(curve), base ? end(base) : 0, asm ? end(asm) : 0);
  const L = 54, R = 14, Tp = 16, Bt = 40; const w = W - L - R, h = H - Tp - Bt;
  const X = (t) => L + (t / T) * w, Y = (n) => Tp + h - (n / Math.max(1, P)) * h;
  g.font = '20px system-ui'; g.fillStyle = UI.muted; g.strokeStyle = UI.line; g.lineWidth = 1;
  g.textAlign = 'right';
  for (let i = 0; i <= 4; i++) { const n = (P * i) / 4; const y = Y(n); g.beginPath(); g.moveTo(L, y); g.lineTo(W - R, y); g.stroke(); g.fillText(Math.round(n), L - 8, y + 6); }
  g.textAlign = 'center';
  const step = T > 400 ? 120 : T > 150 ? 60 : T > 60 ? 20 : 10;
  for (let t = 0; t <= T; t += step) g.fillText(`${t}s`, X(t), H - 12);
  if (base) {
    g.strokeStyle = '#71717a'; g.lineWidth = 2; g.setLineDash([9, 7]); g.beginPath();
    base.forEach(([t, n], i) => (i ? g.lineTo(X(t), Y(n)) : g.moveTo(X(t), Y(n)))); g.stroke(); g.setLineDash([]);
  }
  if (asm) {
    g.strokeStyle = GOAL_COL; g.lineWidth = 2.5; g.beginPath();
    asm.forEach(([t, n], i) => (i ? g.lineTo(X(t), Y(n)) : g.moveTo(X(t), Y(n)))); g.stroke();
  }
  g.strokeStyle = UI.brand; g.lineWidth = 3; g.beginPath();
  curve.forEach(([t, n], i) => (i ? g.lineTo(X(t), Y(n)) : g.moveTo(X(t), Y(n)))); g.stroke();
  const stuck = S.sim.stats.trapped_no_route + S.sim.stats.not_out_by_end;
  if (stuck) {  // the people who never get out
    g.strokeStyle = CLOSED_COL; g.lineWidth = 1.5; g.setLineDash([4, 4]); g.beginPath();
    g.moveTo(L, Y(P - stuck)); g.lineTo(W - R, Y(P - stuck)); g.stroke(); g.setLineDash([]);
    g.fillStyle = CLOSED_COL; g.textAlign = 'right'; g.fillText(`${stuck} never get out`, W - R - 4, Y(P - stuck) - 8);
  }
  g.strokeStyle = UI.ink; g.lineWidth = 1.5; g.beginPath(); g.moveTo(X(S.t), Tp); g.lineTo(X(S.t), Tp + h); g.stroke();
  g.textAlign = 'left'; let ly = Tp + 18;
  g.fillStyle = UI.brandDark; g.fillText('out of the building', L + 6, ly);
  if (asm) { ly += 24; g.fillStyle = GOAL_COL; g.fillText('at the assembly point', L + 6, ly); }
  if (base) { ly += 24; g.fillStyle = UI.muted; g.fillText('- - nothing closed', L + 6, ly); }
}

$('#play').onclick = () => { if (!S.sim) return; if (S.t >= S.sim.T) S.t = 0; S.playing = !S.playing; $('#play').textContent = S.playing ? '❚❚' : '▶'; };
$('#time').addEventListener('input', (e) => { S.t = Number(e.target.value); S.playing = false; $('#play').textContent = '▶'; });

export let lastT = null;
view.onFrame = (now) => {
  const dt = lastT == null ? 0 : (now - lastT) / 1000; lastT = now;
  if (!S.sim || !S.frames) return;
  if (S.playing) {
    S.t += dt * Number($('#speed').value);
    if (S.t >= S.sim.T) { S.t = S.sim.T; S.playing = false; $('#play').textContent = '▶'; }
  }
  const P = S.sim.people, F = S.sim.frames, fd = S.sim.frame_dt;
  const f = Math.min(F - 1, S.t / fd); const i0 = Math.floor(f), i1 = Math.min(F - 1, i0 + 1), a = f - i0;
  const out = drawFrame.buf && drawFrame.buf.length === P * 3 ? drawFrame.buf : (drawFrame.buf = new Float32Array(P * 3));
  const A = i0 * P * 3, Bo = i1 * P * 3, st = S.sim.frame_step;
  for (let i = 0; i < P * 3; i++) {
    const p = S.frames[A + i], q = S.frames[Bo + i];
    out[i] = p === 65535 ? NaN : q === 65535 ? p * st : (p + (q - p) * a) * st;
  }
  view.setAgents(out, S.sim.colors);
  $('#time').value = S.t; $('#clock').textContent = `${S.t.toFixed(0)} s`;
  if (S.playing || drawFrame.lastDrawn !== S.t) {
    drawChart(); drawFrame.lastDrawn = S.t;
    plan.agents = out; if (S.tab === 'plan') plan.draw();
  }
};
export function drawFrame() {}

