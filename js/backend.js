// Where the app's work runs. Same calls either way:
//  - 'server': the local mc2sim server's /api/* (`mc2sim serve`), which reads worlds from the Minecraft saves folder;
//  - 'local' (browser-only): the same Python code runs in a Web Worker with Pyodide (js/local/worker.js). Worlds are
//    picked as a folder or .zip and read in this tab; buildings and results never leave the browser.
// Choose with ?mode=local or ?mode=server; a static build (`mc2sim site`) sets <meta name="mc2sim-mode" content="local">.
// Otherwise the server is used when one answers. Imports only common.js, so any module may import it.
import { getJSON, postJSON } from './common.js?v=07c5c62b44';

export const api = { mode: null };
let statusListener = () => {};
export function onStatus(fn) { statusListener = fn; }

export async function initBackend() {
  let mode = new URLSearchParams(location.search).get('mode') || document.querySelector('meta[name="mc2sim-mode"]')?.content || 'auto';
  if (mode === 'auto') {
    try { const r = await fetch('/api/worlds'); mode = r.ok && (r.headers.get('content-type') || '').includes('json') ? 'server' : 'local'; } catch { mode = 'local'; }
  }
  Object.assign(api, mode === 'local' ? localBackend() : serverBackend());
  return api.mode;
}

function serverBackend() {
  const binary = async (url, what) => { const r = await fetch(url); if (!r.ok) throw new Error(`Could not load ${what} (${r.status}).`); return r.arrayBuffer(); };
  return {
    mode: 'server',
    ready: Promise.resolve(true),
    worlds: () => getJSON('/api/worlds'),
    open: (name, refresh) => postJSON('/api/open', { name, refresh }),
    tile: (world, rx, rz) => binary(`/api/tile?world=${encodeURIComponent(world)}&rx=${rx}&rz=${rz}`, `map tile ${rx},${rz}`),
    convert: (body) => postJSON('/api/convert', body),
    demo: (body) => postJSON('/api/demo', body),
    reanalyze: (body) => postJSON('/api/reanalyze', body),
    async job(kind, body, progress) {   // background job on the server, polled for progress
      const { job } = await postJSON(`/api/jobs/${kind}`, body);
      for (;;) {
        await new Promise((r) => setTimeout(r, 500));
        const j = await getJSON(`/api/job?id=${job}`);
        if (progress) progress(j.progress);
        if (j.status === 'done') return j.result;
        if (j.status === 'error') throw new Error(j.error);
      }
    },
    sim: (id) => getJSON(`/api/sim?id=${id}`),
    frames: (id) => binary(`/api/frames?sim=${id}`, 'the playback for this run'),
    download(kind, id) { window.location.href = kind === 'building' ? `/api/export/building?id=${id}` : `/api/export/results?sim=${id}`; },
  };
}

function localBackend() {
  const worker = new Worker(new URL('./local/worker.js?v=07c5c62b44', import.meta.url), { type: 'module' });
  const pending = new Map(); let next = 1;
  worker.onmessage = ({ data }) => {
    if (data.status !== undefined) { statusListener(data.status); return; }
    const p = pending.get(data.id); if (!p) return;
    if (data.progress !== undefined) { if (p.progress) p.progress(data.progress); return; }
    pending.delete(data.id);
    if (data.error !== undefined) p.reject(new Error(data.error)); else p.resolve(data.result);
  };
  worker.onerror = (e) => {
    const err = new Error(`The in-browser simulator stopped (${e.message || 'worker error'}). Reload the page.`);
    for (const p of pending.values()) p.reject(err);
    pending.clear(); statusListener('error');
  };
  const call = (op, args, progress) => new Promise((resolve, reject) => {
    const id = next++; pending.set(id, { resolve, reject, progress });
    worker.postMessage({ id, op, args });
  });
  const custom = document.querySelector('meta[name="mc2sim-pyodide"]')?.content;
  const ready = call('init', { pyodideURL: custom ? new URL(custom, location.href).href : null });
  ready.catch((e) => statusListener('error: ' + e.message));
  return {
    mode: 'local',
    ready,
    worlds: async () => ({ saves: null, worlds: [] }),
    open: (source) => call('open', { source }),
    tile: async (world, rx, rz) => (await call('tile', { rx, rz })).buffer,
    convert: (body) => call('convert', body),
    demo: (body) => call('demo', body),
    reanalyze: (body) => call('reanalyze', body),
    job: (kind, body, progress) => call('job', { kind, body }, progress),
    sim: (id) => call('sim', { id }),
    frames: async (id) => (await call('frames', { id })).buffer,
    async download(kind, id) {   // made in the worker, saved straight from this tab
      const text = await call('export', { kind, id });
      const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
      const a = document.createElement('a'); a.href = url; a.download = kind === 'building' ? 'building.json' : 'results.json';
      document.body.appendChild(a); a.click(); a.remove();
      setTimeout(() => URL.revokeObjectURL(url), 30000);
    },
  };
}
