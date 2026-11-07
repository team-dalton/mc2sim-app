// Shared app state and the three views (created once, used by app.js and sim.js).
import { $ } from './common.js?v=8126566452';
import { MapView } from './mapview.js?v=8126566452';
import { View3D } from './view3d.js?v=8126566452';
import { PlanView } from './planview.js?v=8126566452';

// -------------------------------------------------------------------- app ---
export const newClosed = () => ({ exit: new Set(), door: new Set(), stairs: new Set() });
export const S = {
  world: null, B: null, sim: null, frames: null, playing: false, t: 0, closed: newClosed(), use: null, stress: null,
  tab: 'map', areaKey: null, fixes: [], appliedKeys: new Set(), goal: null,
  mode: 'normal',   // simulation mode: 'normal' | 'blocked' (your closures) | 'stress' (one closure at a time)
  byMode: {},       // last run shown in each mode: { res, frames, t }
};
// Closures drawn in the views: yours in Blocked exits, the run's own in Weak points, none in Normal.
export function shownClosed() {
  if (S.mode === 'blocked') return S.closed;
  const c = newClosed();
  if (S.mode === 'stress' && S.sim) for (const x of S.sim.stats.scenario.closed) c[x.type].add(x.id);
  return c;
}
export const map = new MapView($('#map'));
export const view = new View3D($('#view3d'));
export const plan = new PlanView($('#plan'));
