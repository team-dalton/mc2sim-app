// three.js 3D view: blocks, walkable cells, closures, agents, labels, orbit and spectator (fly) camera.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { $, CAT, CLOSED_COL, GOAL_COL, ROLE, TYPE_COL, UI, b64, clamp, heatColor } from './common.js?v=8126566452';

// ----------------------------------------------------------------- 3D view ---
export class View3D {
  constructor(container) {
    this.el = container;
    this.renderer = new THREE.WebGLRenderer({ antialias: true });
    this.renderer.setPixelRatio(window.devicePixelRatio || 1);
    this.renderer.localClippingEnabled = true;
    container.prepend(this.renderer.domElement);
    this.scene = new THREE.Scene(); this.scene.background = new THREE.Color(UI.scene);
    this.camera = new THREE.PerspectiveCamera(50, 1, 0.1, 5000);
    this.controls = new OrbitControls(this.camera, this.renderer.domElement);
    this.controls.enableDamping = true;
    this.scene.add(new THREE.HemisphereLight(0xffffff, 0x667788, 1.7));
    const sun = new THREE.DirectionalLight(0xffffff, 1.3); sun.position.set(0.6, 1, 0.35); this.scene.add(sun);
    this.clip = new THREE.Plane(new THREE.Vector3(0, -1, 0), 1e4);
    this.meshes = []; this.onFrame = () => {}; this.labels = []; this.goal = null;
    this.initFly();
    new ResizeObserver(() => this.resize()).observe(container);
    let last = null;
    const loop = (t) => {
      requestAnimationFrame(loop);
      const dt = last == null ? 0 : Math.min(0.1, (t - last) / 1000); last = t;
      this.onFrame(t);
      if (this.fly) this.stepFly(dt); else this.controls.update();
      this.renderer.render(this.scene, this.camera); this.placeLabels();
    };
    requestAnimationFrame(loop);
  }
  // ---- spectator ("fly") camera: WASD + mouse look, Space/Shift up/down, like Minecraft's spectator mode
  initFly() {
    this.fly = false; this.keys = new Set(); this.yaw = 0; this.pitch = 0; this.speed = 8; this.onFlyChange = () => {};
    const dom = this.renderer.domElement;
    const typing = (e) => e.target && e.target.closest && e.target.closest('input, select, textarea');
    window.addEventListener('keydown', (e) => {
      if (!this.fly || this.el.hidden || typing(e)) return;
      if (e.code === 'Escape' && document.pointerLockElement !== dom) { this.setFly(false); return; }
      this.keys.add(e.code);
      if (['Space', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.code)) e.preventDefault();
    });
    window.addEventListener('keyup', (e) => this.keys.delete(e.code));
    window.addEventListener('blur', () => this.keys.clear());
    dom.addEventListener('pointerdown', (e) => {
      if (!this.fly) return;
      this.drag = { x: e.clientX, y: e.clientY };
      try { const p = dom.requestPointerLock && dom.requestPointerLock(); if (p && p.catch) p.catch(() => {}); } catch { /* drag-to-look still works */ }
    });
    window.addEventListener('pointerup', () => { this.drag = null; });
    window.addEventListener('pointermove', (e) => {
      if (!this.fly) return;
      const locked = document.pointerLockElement === dom;
      if (!locked && !this.drag) return;
      const dx = locked ? e.movementX : e.clientX - this.drag.x, dy = locked ? e.movementY : e.clientY - this.drag.y;
      if (!locked) this.drag = { x: e.clientX, y: e.clientY };
      this.yaw -= dx * 0.0028; this.pitch = clamp(this.pitch - dy * 0.0028, -1.55, 1.55);
    });
    dom.addEventListener('wheel', (e) => {
      if (!this.fly) return;
      e.preventDefault(); this.speed = clamp(this.speed * Math.exp(-e.deltaY * 0.002), 1, 60); this.onFlyChange();
    }, { passive: false });
    document.addEventListener('pointerlockchange', () => this.onFlyChange());
  }
  setFly(on, at = null) {
    this.fly = on; this.controls.enabled = !on; this.keys.clear(); this.drag = null;
    const d = new THREE.Vector3();
    if (on) {
      if (at) {
        this.camera.position.set(at.x, at.y, at.z); this.pitch = -0.05;
        if (at.yaw != null) this.yaw = at.yaw;
      } else {
        this.camera.getWorldDirection(d); this.yaw = Math.atan2(-d.x, -d.z); this.pitch = Math.asin(clamp(d.y, -1, 1));
      }
      this.camera.rotation.set(this.pitch, this.yaw, 0, 'YXZ');
    } else {
      if (document.pointerLockElement) document.exitPointerLock();
      this.camera.getWorldDirection(d);
      this.controls.target.copy(this.camera.position).addScaledVector(d, 20);
    }
    this.onFlyChange();
  }
  stepFly(dt) {
    const k = this.keys; const has = (...c) => c.some((x) => k.has(x));
    const f = (has('KeyW', 'ArrowUp') ? 1 : 0) - (has('KeyS', 'ArrowDown') ? 1 : 0);
    const s = (has('KeyD', 'ArrowRight') ? 1 : 0) - (has('KeyA', 'ArrowLeft') ? 1 : 0);
    const u = (has('Space', 'KeyE') ? 1 : 0) - (has('ShiftLeft', 'ShiftRight', 'KeyQ') ? 1 : 0);
    const sp = this.speed * (has('KeyR') ? 3 : 1) * dt;
    const sy = Math.sin(this.yaw), cy = Math.cos(this.yaw); const p = this.camera.position;
    p.x += (-sy * f + cy * s) * sp; p.z += (-cy * f - sy * s) * sp; p.y += u * sp;
    this.camera.rotation.set(this.pitch, this.yaw, 0, 'YXZ');
  }
  setGoal(pos) {  // assembly point marker: pole, flag and a ring on the ground
    if (this.goal) { this.scene.remove(this.goal); this.goal.traverse((o) => { if (o.geometry) { o.geometry.dispose(); o.material.dispose(); } }); this.goal = null; }
    if (!pos) return;
    const g = new THREE.Group();
    const pole = new THREE.Mesh(new THREE.CylinderGeometry(0.07, 0.07, 3.6, 8), new THREE.MeshLambertMaterial({ color: '#ffffff' }));
    pole.position.y = 1.8; g.add(pole);
    const flag = new THREE.Mesh(new THREE.BoxGeometry(1.4, 0.8, 0.06), new THREE.MeshLambertMaterial({ color: GOAL_COL, emissive: '#0b3d22' }));
    flag.position.set(0.72, 3.15, 0); g.add(flag);
    const ring = new THREE.Mesh(new THREE.RingGeometry(1.2, 1.55, 40), new THREE.MeshBasicMaterial({ color: GOAL_COL, side: THREE.DoubleSide, transparent: true, opacity: 0.8 }));
    ring.rotation.x = -Math.PI / 2; ring.position.y = 0.06; g.add(ring);
    g.position.set(pos.x, pos.y, pos.z); this.scene.add(g); this.goal = g;
  }
  resize() {
    const r = this.el.getBoundingClientRect(); if (!r.width) return;
    this.renderer.setSize(r.width, r.height); this.camera.aspect = r.width / r.height; this.camera.updateProjectionMatrix();
  }
  clear() {
    for (const m of this.meshes) { this.scene.remove(m); m.geometry.dispose(); m.material.dispose(); }
    this.meshes = []; this.agents = null; this.walk = null; this.closedMesh = null;
  }
  setClosed(nodes) {  // red 2 m walls on closed cells
    if (this.closedMesh) {
      this.scene.remove(this.closedMesh); this.closedMesh.geometry.dispose(); this.closedMesh.material.dispose();
      this.meshes = this.meshes.filter((m) => m !== this.closedMesh); this.closedMesh = null;
    }
    this.closedSet.fill(0);
    for (const i of nodes) this.closedSet[i] = 1;
    if (!nodes.length) return;
    const geo = new THREE.BoxGeometry(0.98, 2.0, 0.98);
    const mat = new THREE.MeshLambertMaterial({ color: CLOSED_COL, emissive: '#5a0010', transparent: true, opacity: 0.62, clippingPlanes: [this.clip] });
    const m = new THREE.InstancedMesh(geo, mat, nodes.length); const M = new THREE.Matrix4(); const p = this.nodeXYZ;
    nodes.forEach((i, k) => { M.makeTranslation(p[i * 3], p[i * 3 + 1] + 1.0, p[i * 3 + 2]); m.setMatrixAt(k, M); });
    this.scene.add(m); this.meshes.push(m); this.closedMesh = m;
  }
  build(B, keepCamera = false) {
    this.clear(); this.B = B;
    const r = B.render; const [X, Y, Z] = r.size;
    const blocks = b64(r.blocks, Int16Array); const pal = b64(r.block_pal, Uint16Array);
    const n = pal.length; const P = r.palette;
    const isTrans = P.map((p) => p.role === 'glass' || p.shape === 'door');
    let nt = 0; for (let i = 0; i < n; i++) if (isTrans[pal[i]]) nt++;
    const box = new THREE.BoxGeometry(1, 1, 1);
    const mk = (count, trans) => {
      const mat = new THREE.MeshLambertMaterial({ clippingPlanes: [this.clip], transparent: trans, opacity: trans ? 0.32 : 1, depthWrite: !trans });
      const m = new THREE.InstancedMesh(box.clone(), mat, Math.max(1, count)); m.count = count; this.scene.add(m); this.meshes.push(m); return m;
    };
    this.opaque = mk(n - nt, false); this.trans = mk(nt, true);
    this.opaquePal = new Uint16Array(n - nt); this.transPal = new Uint16Array(nt);
    const M = new THREE.Matrix4(), q = new THREE.Quaternion(), s = new THREE.Vector3(), v = new THREE.Vector3();
    let io = 0, it = 0;
    for (let i = 0; i < n; i++) {
      const p = P[pal[i]]; const x = blocks[i * 3], y = blocks[i * 3 + 1], z = blocks[i * 3 + 2];
      let sy = 1, sxz = 1, oy = 0.5;
      if (p.shape === 'slab') { sy = 0.5; oy = 0.25; }
      else if (p.shape === 'barrier' && p.role !== 'glass') { sxz = 0.3; }
      else if (p.shape === 'obstacle') { sy = 0.6; sxz = 0.8; oy = 0.3; }
      else if (p.shape === 'door') { sxz = 0.9; }
      M.compose(v.set(x + 0.5, y + oy, z + 0.5), q, s.set(sxz, sy, sxz));
      if (isTrans[pal[i]]) { this.trans.setMatrixAt(it, M); this.transPal[it++] = pal[i]; }
      else { this.opaque.setMatrixAt(io, M); this.opaquePal[io++] = pal[i]; }
    }
    // walkable surface
    const nodes = b64(r.nodes, Float32Array); const N = r.node_count;
    this.nodeXYZ = nodes;
    this.nodeKind = b64(r.node_kind, Uint8Array); this.nodeFlags = b64(r.node_flags, Uint8Array);
    this.nodeRoom = b64(r.node_room, Int32Array); this.nodeLevel = b64(r.node_level, Int16Array);
    this.nodeExit = b64(r.node_exit, Int16Array); this.nodeDoor = b64(r.node_door, Int16Array);
    this.nodeStair = b64(r.node_stair, Int16Array); this.closedSet = new Uint8Array(N);
    const plane = new THREE.PlaneGeometry(0.94, 0.94); plane.rotateX(-Math.PI / 2);
    const wmat = new THREE.MeshBasicMaterial({ clippingPlanes: [this.clip], transparent: true, opacity: 0.8, depthWrite: false });
    this.walk = new THREE.InstancedMesh(plane, wmat, Math.max(1, N)); this.walk.count = N;
    for (let i = 0; i < N; i++) { M.makeTranslation(nodes[i * 3], nodes[i * 3 + 1] + 0.04, nodes[i * 3 + 2]); this.walk.setMatrixAt(i, M); }
    this.scene.add(this.walk); this.meshes.push(this.walk);
    // agents
    const cyl = new THREE.CylinderGeometry(0.26, 0.26, 1.7, 10); cyl.translate(0, 0.85, 0);
    this.agents = new THREE.InstancedMesh(cyl, new THREE.MeshLambertMaterial({ clippingPlanes: [this.clip] }), 5000);
    // people move every frame, but three.js works out an instanced mesh's bounds once (here: before anyone is
    // placed) and then culls the whole crowd whenever those stale bounds leave the screen - so never cull it
    this.agents.frustumCulled = false;
    this.agents.count = 0; this.scene.add(this.agents); this.meshes.push(this.agents);
    // camera
    const ground = (B.summary.ground_h ?? B.render.origin[1]) - B.render.origin[1];
    this.size = [X, Y, Z]; this.ground = ground;
    const c = new THREE.Vector3(X / 2, ground, Z / 2); const R = Math.max(X, Z);
    if (!keepCamera) {
      if (this.fly) this.setFly(false);
      this.controls.target.copy(c); this.camera.position.set(c.x + R * 0.45, c.y + R * 0.75, c.z + R * 0.85);
    }
    this.camera.far = R * 20; this.camera.updateProjectionMatrix();
    this.resize();
  }
  setBlockMode(mode) {
    if (!this.B) return;
    const P = this.B.render.palette; const col = new THREE.Color();
    const paint = (mesh, pals) => {
      mesh.visible = mode !== 'off';
      for (let i = 0; i < pals.length; i++) {
        const p = P[pals[i]];
        if (mode === 'role') col.set(ROLE[p.role] || ROLE.solid);
        else col.setRGB(p.rgb[0] / 255, p.rgb[1] / 255, p.rgb[2] / 255, THREE.SRGBColorSpace);
        mesh.setColorAt(i, col);
      }
      if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    };
    paint(this.opaque, this.opaquePal); paint(this.trans, this.transPal);
    this.opaque.material.transparent = mode === 'ghost'; this.opaque.material.opacity = mode === 'ghost' ? 0.18 : 1;
    this.opaque.material.depthWrite = mode !== 'ghost'; this.opaque.material.needsUpdate = true;
  }
  setOverlay(mode, data) {
    if (!this.walk) return;
    this.walk.visible = mode !== 'off'; if (mode === 'off') return;
    const N = this.walk.count; const col = new THREE.Color();
    const max = data ? Math.max(1e-6, data.max) : 1;
    for (let i = 0; i < N; i++) {
      const k = this.nodeKind[i], f = this.nodeFlags[i];
      const safe = f & 1, indoor = f & 2, exit = f & 4;
      if (this.closedSet[i]) {
        col.set(CLOSED_COL);
      } else if (mode === 'type') {
        col.set(exit ? TYPE_COL.exit : k === 1 ? TYPE_COL.stairs : k === 2 ? TYPE_COL.door : safe ? TYPE_COL.safe : indoor ? TYPE_COL.indoor : TYPE_COL.outdoor);
      } else if (mode === 'floor') {
        const L = this.nodeLevel[i]; col.set(L >= 0 && indoor ? CAT[L % CAT.length] : '#3a3d42');
      } else if (mode === 'room') {
        const R = this.nodeRoom[i]; col.set(R >= 0 ? CAT[(R * 7) % CAT.length] : '#3a3d42');
      } else if (data) {
        const v = data.values[i];
        if (v <= 0) col.set(indoor ? '#2a3038' : '#232a26'); else heatColor(v / max, col);
      }
      this.walk.setColorAt(i, col);
    }
    this.walk.instanceColor.needsUpdate = true;
  }
  setCut(y) { this.clip.constant = y; }
  focus(x, y, z) {
    const off = this.camera.position.clone().sub(this.controls.target).setLength(28);
    this.controls.target.set(x, y, z); this.camera.position.set(x + off.x, y + off.y, z + off.z);
  }
  clearAgents() { if (this.agents) this.agents.count = 0; }
  setAgents(pos, colors) {
    const M = new THREE.Matrix4(); const P = colors.length; const ag = this.agents; ag.count = P;
    const hide = new THREE.Matrix4().makeScale(0, 0, 0);
    for (let i = 0; i < P; i++) {
      const x = pos[i * 3];
      if (Number.isNaN(x)) ag.setMatrixAt(i, hide); else { M.makeTranslation(x, pos[i * 3 + 2], pos[i * 3 + 1]); ag.setMatrixAt(i, M); }
      ag.setColorAt(i, colors[i]);
    }
    ag.instanceMatrix.needsUpdate = true; if (ag.instanceColor) ag.instanceColor.needsUpdate = true;
  }
  setLabels(list) {
    const host = $('#labels'); host.innerHTML = '';
    this.labels = list.map((l) => {
      const d = document.createElement('div'); d.className = 'lbl ' + (l.cls || '') + (l.onClick ? ' click' : '');
      d.textContent = l.text; if (l.title) d.title = l.title;
      if (l.onClick) d.addEventListener('click', l.onClick);
      host.appendChild(d); return { ...l, el: d };
    });
  }
  placeLabels() {
    if (!this.labels.length) return;
    const r = this.el.getBoundingClientRect(); const v = new THREE.Vector3();
    for (const l of this.labels) {
      v.set(l.x, l.y + 2.2, l.z);
      const hidden = l.y > this.clip.constant; v.project(this.camera);
      if (hidden || v.z > 1) { l.el.style.display = 'none'; continue; }
      l.el.style.display = ''; l.el.style.left = `${(v.x * 0.5 + 0.5) * r.width}px`; l.el.style.top = `${(-v.y * 0.5 + 0.5) * r.height}px`;
    }
  }
}

