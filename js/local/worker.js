// Browser-only mode: runs mc2sim's Python code (mc2sim/browser.py) with Pyodide in this Web Worker.
// The page sends requests as messages ({ id, op, args }); answers come back as { id, result } or { id, error },
// with { id, progress } for long jobs and { status } while Python loads. World files are read here, from the
// folder or .zip the user picked; nothing is uploaded, and nothing is stored once the tab is closed.
import { openWorld } from './worldfiles.js';

const PYODIDE = 'https://cdn.jsdelivr.net/pyodide/v314.0.7/full/';   // pinned; a static build may point elsewhere
const MODULES = ['__init__', 'nbt', 'blocks', 'world', 'analyze', 'sim', 'export', 'scenarios', 'demo', 'api', 'browser'];
let py = null, mc = null, loading = null, world = null;

async function load(url) {
  postMessage({ status: 'Loading Python in this browser tab…' });
  const { loadPyodide } = await import(url + 'pyodide.mjs');
  py = await loadPyodide({ indexURL: url });
  postMessage({ status: 'Loading numpy and scipy…' });
  await py.loadPackage(['numpy', 'scipy'], { messageCallback: () => {} });
  postMessage({ status: 'Loading the mc2sim code…' });
  const base = new URL('../../py/mc2sim/', import.meta.url);
  py.FS.mkdirTree('/home/pyodide/mc2sim');
  await Promise.all(MODULES.map(async (m) => {
    const r = await fetch(new URL(m + '.py', base));
    if (!r.ok) throw new Error(`Could not load ${m}.py (${r.status}).`);
    py.FS.writeFile(`/home/pyodide/mc2sim/${m}.py`, await r.text());
  }));
  mc = py.pyimport('mc2sim.browser');
  postMessage({ status: 'ready' });
}

const toPy = (v) => py.toPy(v, { depth: 1 });
function bytesOut(proxy) {   // Python bytes -> a Uint8Array with its own buffer, which the page takes over
  try { const u = proxy.toJs(); return u.byteOffset === 0 && u.byteLength === u.buffer.byteLength ? u : u.slice(); } finally { proxy.destroy(); }
}
async function read(entry) {
  try { return await entry.read(); } catch (e) {
    if (e.name !== 'NotFoundError' && e.name !== 'NotReadableError') throw e;
    throw new Error('A world file changed or went missing since the world was opened (did Minecraft save it?). Open the world again.');
  }
}
async function region(rx, rz) {
  const e = world.regions.get(`${rx},${rz}`);
  if (!e) return [new Uint8Array(0), {}];
  const mcc = {};
  for (const [k, m] of world.mcc) {
    const [cx, cz] = k.split(',').map(Number);
    if (cx >> 5 === rx && cz >> 5 === rz) mcc[`c.${cx}.${cz}.mcc`] = await read(m);
  }
  return [await read(e), mcc];
}

const ops = {
  async init({ pyodideURL }) { await (loading ||= load(pyodideURL || PYODIDE)); return true; },
  async open({ source }) {
    const w = await openWorld(source);
    const players = [];
    for (const p of w.players) { try { players.push(await p.read()); } catch { /* unreadable player file: no position */ } }
    const regions = [...w.regions].map(([k, e]) => [...k.split(',').map(Number), e.size]);
    const res = JSON.parse(mc.open_world(w.name, await read(w.level), JSON.stringify(regions), toPy(players)));
    world = w;
    return res;
  },
  async tile({ rx, rz }) {
    const [data, mcc] = await region(rx, rz);
    return bytesOut(mc.tile(rx, rz, data, toPy(mcc)));
  },
  async convert(body) {
    for (const [rx, rz] of JSON.parse(mc.regions_needed(JSON.stringify(body)))) {
      const [data, mcc] = await region(rx, rz);
      mc.add_region(rx, rz, data, toPy(mcc));
    }
    return JSON.parse(mc.call('convert', JSON.stringify(body)));
  },
  demo: (body) => JSON.parse(mc.call('demo', JSON.stringify(body))),
  reanalyze: (body) => JSON.parse(mc.call('reanalyze', JSON.stringify(body))),
  sim: ({ id }) => JSON.parse(mc.call('sim', JSON.stringify({ id }))),
  frames: ({ id }) => bytesOut(mc.frames(id)),
  job: ({ kind, body }, progress) => JSON.parse(mc.call('job', JSON.stringify({ kind, body }), progress)),
  export: ({ kind, id, paths }) => mc.call(kind === 'building' ? 'export_building' : 'export_results', JSON.stringify({ id, paths })),
};

// Python errors arrive with the whole traceback; the page gets the last line, like the server's error text.
function message(e) {
  const lines = String(e && e.message ? e.message : e).trim().split('\n');
  const m = lines[lines.length - 1].match(/^(\w+): (.*)$/s);
  if (!m) return lines[lines.length - 1];
  return ['ValueError', 'KeyError', 'FileNotFoundError'].includes(m[1]) ? m[2].replace(/^'(.*)'$/s, '$1') : `${m[1]}: ${m[2]}`;
}

let queue = Promise.resolve();   // one request at a time: Python runs on this one thread anyway
onmessage = ({ data: { id, op, args } }) => {
  queue = queue.then(async () => {
    try {
      if (op !== 'init') await loading;
      const progress = (text) => postMessage({ id, progress: String(text) });
      const result = await ops[op](args || {}, progress);
      postMessage({ id, result }, result instanceof Uint8Array ? [result.buffer] : []);
    } catch (e) {
      console.error(e);
      postMessage({ id, error: message(e) });
    }
  });
};
