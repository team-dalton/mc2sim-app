// Reads a Minecraft world the user picked — a folder or a .zip — inside the browser (used by worker.js).
// Only what is asked for gets read: level.dat, a few player files and the region files under the map tiles or
// the building. Nothing is uploaded or stored.
//
// A world is { name, level, regions: Map('rx,rz' -> entry), mcc: Map('cx,cz' -> entry), players: [entry] },
// an entry is { size, modified, read(): Promise<Uint8Array> }.

const REGION_DIRS = ['dimensions/minecraft/overworld/region', 'region'];   // 26.x layout first, then classic
const PLAYER_DIRS = ['players/data', 'playerdata'];
const REGION_RE = /^r\.(-?\d+)\.(-?\d+)\.mca$/;
const MCC_RE = /^c\.(-?\d+)\.(-?\d+)\.mcc$/;

// Is this path (inside the picked folder) one the app could need? Used to thin out a folder's file list.
export function wanted(path) {
  const p = path.replace(/\\/g, '/'); const base = p.slice(p.lastIndexOf('/') + 1);
  if (base === 'level.dat') return true;
  if (REGION_DIRS.some((d) => p.includes(d + '/'))) return REGION_RE.test(base) || MCC_RE.test(base);
  return PLAYER_DIRS.some((d) => p.includes(d + '/')) && base.endsWith('.dat');
}

// source: { kind: 'dir', handle } (File System Access API) | { kind: 'files', files: [{ path, file }] }
// (a folder from <input webkitdirectory>) | { kind: 'zip', file }; plus an optional fallback `name`.
export async function openWorld(source) {
  if (source.kind === 'dir') return fromDirectory(source.handle);
  const entries = source.kind === 'zip' ? await zipEntries(source.file) : fileEntries(source.files);
  return organise(entries, source.name);
}

const fileEntry = (file) => ({ size: file.size, modified: file.lastModified, read: async () => new Uint8Array(await file.arrayBuffer()) });

function fileEntries(files) {
  const out = new Map();
  for (const { path, file } of files) out.set(path.replace(/\\/g, '/'), fileEntry(file));
  return out;
}

// --------------------------------------------------------------- a folder ---
async function child(dir, path, kind) {
  const parts = path.split('/'); let h = dir;
  try {
    for (let i = 0; i < parts.length; i++) {
      h = i === parts.length - 1 && kind === 'file' ? await h.getFileHandle(parts[i]) : await h.getDirectoryHandle(parts[i]);
    }
  } catch { return null; }
  return h;
}

async function fromDirectory(handle) {
  let root = handle;
  if (!(await child(root, 'level.dat', 'file'))) {   // the saves folder (or a parent) was picked: look one level down
    const worlds = [];
    for await (const h of root.values()) if (h.kind === 'directory' && (await child(h, 'level.dat', 'file'))) worlds.push(h);
    if (worlds.length !== 1) {
      throw new Error(worlds.length ? `This folder holds ${worlds.length} worlds (${worlds.slice(0, 3).map((w) => w.name).join(', ')}…). Pick one world folder.`
        : 'No Minecraft world here: pick the world folder, the one that contains level.dat.');
    }
    root = worlds[0];
  }
  const entries = new Map();
  const handleEntry = async (h) => {   // read through the handle each time: Minecraft may have saved since
    const f = await h.getFile();
    return { size: f.size, modified: f.lastModified, read: async () => new Uint8Array(await (await h.getFile()).arrayBuffer()) };
  };
  const add = async (dir, prefix, test) => {
    for await (const h of dir.values()) {
      if (h.kind === 'file' && test(h.name)) entries.set(prefix + h.name, await handleEntry(h));
    }
  };
  entries.set('level.dat', await handleEntry(await root.getFileHandle('level.dat')));
  for (const d of [...REGION_DIRS, ...PLAYER_DIRS]) {
    const dir = await child(root, d, 'dir');
    if (dir) await add(dir, d + '/', (n) => (PLAYER_DIRS.includes(d) ? n.endsWith('.dat') : REGION_RE.test(n) || MCC_RE.test(n)));
  }
  return organise(entries, root.name);
}

// ------------------------------------------------------------------ a zip ---
async function zipEntries(file) {
  const tailLen = Math.min(file.size, 22 + 65535);
  const tail = new DataView(await file.slice(file.size - tailLen).arrayBuffer());
  let e = -1;
  for (let i = tailLen - 22; i >= 0; i--) if (tail.getUint32(i, true) === 0x06054b50) { e = i; break; }
  if (e < 0) throw new Error('This is not a .zip file, or it is damaged.');
  let count = tail.getUint16(e + 10, true), cdSize = tail.getUint32(e + 12, true), cdOff = tail.getUint32(e + 16, true);
  if (count === 0xffff || cdSize === 0xffffffff || cdOff === 0xffffffff) {   // zip64
    const loc = e - 20;
    if (loc < 0 || tail.getUint32(loc, true) !== 0x07064b50) throw new Error('Unsupported .zip (zip64 without its locator).');
    const at = Number(tail.getBigUint64(loc + 8, true));
    const rec = new DataView(await file.slice(at, at + 56).arrayBuffer());
    if (rec.getUint32(0, true) !== 0x06064b50) throw new Error('Damaged .zip (zip64 record).');
    count = Number(rec.getBigUint64(32, true)); cdSize = Number(rec.getBigUint64(40, true)); cdOff = Number(rec.getBigUint64(48, true));
  }
  const cd = new DataView(await file.slice(cdOff, cdOff + cdSize).arrayBuffer());
  const names = new TextDecoder();
  const out = new Map();
  for (let p = 0, i = 0; i < count && p + 46 <= cd.byteLength && cd.getUint32(p, true) === 0x02014b50; i++) {
    const flags = cd.getUint16(p + 8, true), method = cd.getUint16(p + 10, true);
    const modified = cd.getUint16(p + 14, true) * 65536 + cd.getUint16(p + 12, true);   // DOS date, time: sortable
    let csize = cd.getUint32(p + 20, true), usize = cd.getUint32(p + 24, true), off = cd.getUint32(p + 42, true);
    const nlen = cd.getUint16(p + 28, true), xlen = cd.getUint16(p + 30, true), clen = cd.getUint16(p + 32, true);
    const name = names.decode(new Uint8Array(cd.buffer, p + 46, nlen)).replace(/\\/g, '/');
    for (let x = p + 46 + nlen; x + 4 <= p + 46 + nlen + xlen; x += 4 + cd.getUint16(x + 2, true)) {
      if (cd.getUint16(x, true) !== 1) continue;   // zip64 sizes / offset
      let q = x + 4;
      if (usize === 0xffffffff) { usize = Number(cd.getBigUint64(q, true)); q += 8; }
      if (csize === 0xffffffff) { csize = Number(cd.getBigUint64(q, true)); q += 8; }
      if (off === 0xffffffff) { off = Number(cd.getBigUint64(q, true)); }
    }
    p += 46 + nlen + xlen + clen;
    if (!name.endsWith('/') && wanted(name)) out.set(name, { size: usize, modified, read: () => zipRead(file, name, off, csize, method, flags) });
  }
  return out;
}

async function zipRead(file, name, off, csize, method, flags) {
  if (flags & 1) throw new Error(`${name} is encrypted in the .zip.`);
  const h = new DataView(await file.slice(off, off + 30).arrayBuffer());
  if (h.getUint32(0, true) !== 0x04034b50) throw new Error(`Damaged .zip entry: ${name}`);
  const start = off + 30 + h.getUint16(26, true) + h.getUint16(28, true);
  const blob = file.slice(start, start + csize);
  if (method === 0) return new Uint8Array(await blob.arrayBuffer());
  if (method === 8) return new Uint8Array(await new Response(blob.stream().pipeThrough(new DecompressionStream('deflate-raw'))).arrayBuffer());
  throw new Error(`${name} uses a .zip compression the browser cannot read (method ${method}); zip the world with standard settings.`);
}

// ---------------------------------------------------------------- common ---
function organise(entries, fallbackName) {
  const levels = [...entries.keys()].filter((p) => p === 'level.dat' || p.endsWith('/level.dat'));
  if (!levels.length) throw new Error('No Minecraft world here: there is no level.dat. Pick the world folder (or a .zip of it).');
  const depth = (p) => p.split('/').length;
  levels.sort((a, b) => depth(a) - depth(b));
  if (levels.length > 1 && depth(levels[0]) === depth(levels[1])) {
    throw new Error(`This holds several worlds (${levels.slice(0, 3).map((p) => p.split('/').slice(-2, -1)[0]).join(', ')}…). Pick one world.`);
  }
  const prefix = levels[0].slice(0, -'level.dat'.length);
  const world = { name: prefix ? prefix.slice(0, -1).split('/').pop() : fallbackName || 'World', level: entries.get(levels[0]),
    regions: new Map(), mcc: new Map(), players: [] };
  for (const d of REGION_DIRS) {
    for (const [p, e] of entries) {
      if (!p.startsWith(prefix + d + '/') || p.slice(prefix.length + d.length + 1).includes('/')) continue;
      const base = p.slice(p.lastIndexOf('/') + 1); let m;
      if ((m = REGION_RE.exec(base))) world.regions.set(`${m[1]},${m[2]}`, e);
      else if ((m = MCC_RE.exec(base))) world.mcc.set(`${m[1]},${m[2]}`, e);
    }
    if (world.regions.size) break;
  }
  if (!world.regions.size) throw new Error('This world has no overworld region files (nothing has been built in it yet?).');
  world.players = [...entries].filter(([p]) => PLAYER_DIRS.some((d) => p.startsWith(prefix + d + '/')) && p.endsWith('.dat'))
    .map(([, e]) => e).sort((a, b) => b.modified - a.modified).slice(0, 3);
  return world;
}
