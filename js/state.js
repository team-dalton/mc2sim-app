// Shared app state and the three views (created once, used by app.js and sim.js).
import { $ } from './common.js';
import { MapView } from './mapview.js';
import { View3D } from './view3d.js';
import { PlanView } from './planview.js';

// -------------------------------------------------------------------- app ---
export const newClosed = () => ({ exit: new Set(), door: new Set(), stairs: new Set() });
export const S = {
  world: null, B: null, sim: null, frames: null, playing: false, t: 0, closed: newClosed(), use: null, stress: null,
  tab: 'map', areaKey: null, fixes: [], appliedKeys: new Set(), goal: null,
};
export const map = new MapView($('#map'));
export const view = new View3D($('#view3d'));
export const plan = new PlanView($('#plan'));
