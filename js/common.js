// Shared helpers: DOM shortcuts, storage, fetch, toasts, base64, colours and the Team Dalton theme.
import * as THREE from 'three';

// ------------------------------------------------------------------ utils ---
export const $ = (s) => document.querySelector(s);
export const fmt = (v, d = 0) => (v == null || Number.isNaN(v) ? '–' : Number(v).toFixed(d));
export const store = {
  get(k, d) { try { const v = localStorage.getItem('mc2sim.' + k); return v == null ? d : JSON.parse(v); } catch { return d; } },
  set(k, v) { try { localStorage.setItem('mc2sim.' + k, JSON.stringify(v)); } catch { /* storage unavailable */ } },
};
export async function getJSON(url) {
  const r = await fetch(url); const j = await r.json();
  if (!r.ok) throw new Error(j.error || r.statusText); return j;
}
export async function postJSON(url, body) {
  const r = await fetch(url, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
  const j = await r.json(); if (!r.ok) throw new Error(j.error || r.statusText); return j;
}
export function toast(msg, info = false) {
  const t = $('#toast'); t.textContent = msg; t.className = info ? 'info' : '';
  clearTimeout(toast.timer); toast.timer = setTimeout(() => { t.textContent = ''; }, info ? 4000 : 10000);
}
export async function busy(text, fn) {
  $('#busytext').textContent = text; $('#busytext').dataset.title = text; $('#busy').hidden = false;
  try { return await fn(); } catch (e) { toast(e.message); console.error(e); return null; } finally { $('#busy').hidden = true; }
}
// second, smaller line under the busy message (progress of a long job, Python loading in browser-only mode)
export function busyNote(note) {
  const t = $('#busytext');
  t.innerHTML = esc(t.dataset.title || '') + (note ? `<br><small>${esc(note)}</small>` : '');
}
export function b64(s, T) {
  const bin = atob(s); const u = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) u[i] = bin.charCodeAt(i);
  return new T(u.buffer);
}

export const CAT = ['#4f9cf9', '#f28e2b', '#59a14f', '#e15759', '#b07aa1', '#76b7b2', '#edc948', '#ff9da7', '#9c755f', '#bab0ac'];
export const EXIT_COL = ['#4f9cf9', '#f28e2b', '#59a14f', '#b07aa1', '#76b7b2', '#edc948', '#9c755f', '#bab0ac'];  // no reds
export const CLOSED_COL = '#ff2d55';
export const GOAL_COL = '#2fbf71';
// Team Dalton theme (black with orange + white accents) for canvases; style.css holds the same values for the page
export const UI = { brand: '#cc5500', brandDark: '#e0681c', ink: '#ffffff', muted: '#a1a1aa', line: '#2a2a2f', canvas: '#0d0d0f', scene: '#0c0d10', tag: '#16161a' };
export function outlinedText(g, text, x, y, font, color, align = 'left') {
  g.font = font; g.textAlign = align; g.lineJoin = 'round';
  g.strokeStyle = 'rgba(0,0,0,0.85)'; g.lineWidth = 3.5; g.strokeText(text, x, y);
  g.fillStyle = color; g.fillText(text, x, y);
}
export const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
export const esc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
export const ROLE = {
  solid: '#9aa0a6', glass: '#9fd8ff', door: '#c47a3a', door_controlled: '#8a5a3a', gate: '#a8743f', stairs: '#f29e4c',
  slab: '#c8bfa8', barrier: '#7b8494', furniture: '#6fbf73', seat: '#6fbf73', table: '#8bc34a', bed: '#e57373',
  storage: '#a1887f', workstation: '#4db6ac', kitchen: '#ba68c8', light: '#ffd54f', leaves: '#4caf50', water: '#4f83cc',
  hazard: '#ff5722', decor: '#bdbdbd', sign: '#bdbdbd', carpet: '#bdbdbd', plate: '#bdbdbd', climbable: '#8d6e63',
};
export const TYPE_COL = { indoor: '#4f9cf9', outdoor: '#5b6b5f', safe: '#3c9a5f', stairs: '#f2a33a', door: '#d08a4a', exit: '#e5484d' };
export const HEAT = ['#fff3b0', '#fdc35c', '#f3782b', '#d63a32', '#8e1650'];
export function heatColor(t, out) {
  t = Math.min(1, Math.max(0, t)) * (HEAT.length - 1);
  const i = Math.min(HEAT.length - 2, Math.floor(t));
  const a = new THREE.Color(HEAT[i]), b = new THREE.Color(HEAT[i + 1]);
  return out.copy(a).lerp(b, t - i);
}

