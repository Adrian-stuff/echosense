// Echo-Sense console: live 3D depth viewer + device controls.
// Works with the simulator, a real Kinect, the Docker virtual Orange Pi and the real device.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/OrbitControls.js';

const $ = (id) => document.getElementById(id);
const DEG = Math.PI / 180;
const KIND = {
  body: { color: 0xffb020, label: 'Obstacle', sound: 'mid tone' },
  head: { color: 0xff4f7b, label: 'Head level', sound: 'high double-ping' },
  low: { color: 0x45c8f5, label: 'Low obstacle', sound: 'low tone' },
  drop: { color: 0xa98bff, label: 'Drop-off', sound: 'falling sweep' },
  wall: { color: 0x5b6878, label: 'Parallel wall', sound: 'suppressed' },
};
const MAT_COLOR = { concrete: 0x8a96a6, metal: 0x7fa7c9, glass: 0x9fe9ff, fabric: 0xc78bd6, foliage: 0x5fbf6a,
  wood: 0xc8a27a, skin: 0xe0b49a, asphalt: 0x555f6b };

let hello = null;
let st = null;
let ws = null;
let view = 'follow';

// ------------------------------------------------------------------ socket
function connect() {
  ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
  ws.binaryType = 'arraybuffer';
  ws.onmessage = (ev) => (typeof ev.data === 'string' ? onJson(JSON.parse(ev.data)) : onBinary(ev.data));
  ws.onopen = () => { if (audio.on) send({ t: 'audio', on: true }); };
  ws.onclose = () => { pill('p-health', 'offline', 'bad'); setTimeout(connect, 1200); };
}
const send = (o) => ws && ws.readyState === 1 && ws.send(JSON.stringify(o));
const dec = new TextDecoder();

function onBinary(buf) {
  const type = new Uint8Array(buf, 0, 1)[0];
  if (type === 1) {
    const n = new DataView(buf).getUint32(4, true);
    const state = JSON.parse(dec.decode(new Uint8Array(buf, 8, n)));
    const depth = new Uint16Array(buf, 8 + n);
    onFrame(state, depth);
  } else if (type === 2) {
    playAudio(new Int16Array(buf, 4));
  }
}

function onJson(m) {
  if (m.type === 'hello') onHello(m);
  else if (m.type === 'event') addEvent(m);
}

// ------------------------------------------------------------------- three
const canvas = $('three');
const renderer = new THREE.WebGLRenderer({ canvas, antialias: true, alpha: true });
renderer.setPixelRatio(Math.min(devicePixelRatio, 2));
const scene = new THREE.Scene();
const camera = new THREE.PerspectiveCamera(55, 1, 0.03, 300);
camera.position.set(0, 3, 4);
const controls = new OrbitControls(camera, canvas);
controls.enableDamping = true;
controls.enabled = false;
scene.add(new THREE.HemisphereLight(0xcfe3ff, 0x1a1f26, 1.1));
const sun = new THREE.DirectionalLight(0xffffff, 0.9);
sun.position.set(4, 10, 3);
scene.add(sun);
const grid = new THREE.GridHelper(120, 120, 0x2b3848, 0x18202a);
scene.add(grid);

const worldGroup = new THREE.Group();
scene.add(worldGroup);
const bodyGroup = new THREE.Group(); // sensor-centred body frame (X right, Y up, -Z forward)
scene.add(bodyGroup);
const W = (x, y, z) => new THREE.Vector3(x, y, -z);

// avatar
const avatar = new THREE.Group();
{
  const m = new THREE.MeshStandardMaterial({ color: 0x3dd6c4, roughness: 0.6, transparent: true, opacity: 0.55 });
  const body = new THREE.Mesh(new THREE.CylinderGeometry(0.2, 0.2, 1.45, 20), m);
  body.position.y = 0.725;
  const head = new THREE.Mesh(new THREE.SphereGeometry(0.12, 16, 12), m);
  head.position.y = 1.62;
  avatar.add(body, head);
}
scene.add(avatar);
const sensorBox = new THREE.Mesh(new THREE.BoxGeometry(0.25, 0.07, 0.07), new THREE.MeshStandardMaterial({ color: 0x111111 }));
bodyGroup.add(sensorBox);

// point cloud
const pts = { n: 0, geom: new THREE.BufferGeometry(), mat: new THREE.PointsMaterial({ size: 0.03, vertexColors: true, sizeAttenuation: true }) };
pts.obj = new THREE.Points(pts.geom, pts.mat);
pts.obj.frustumCulled = false;
bodyGroup.add(pts.obj);

// frustum + corridor
const frustum = new THREE.LineSegments(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({ color: 0x3dd6c4, transparent: true, opacity: 0.45 }));
frustum.geometry.setAttribute('position', new THREE.Float32BufferAttribute(new Float32Array(16 * 3), 3));
frustum.frustumCulled = false;
bodyGroup.add(frustum);
const corridor = new THREE.Mesh(new THREE.PlaneGeometry(1, 1), new THREE.MeshBasicMaterial({ color: 0x3dd6c4, transparent: true, opacity: 0.08, depthWrite: false, side: THREE.DoubleSide }));
corridor.rotation.x = -Math.PI / 2;
bodyGroup.add(corridor);
const rangeArc = new THREE.Line(new THREE.BufferGeometry(), new THREE.LineBasicMaterial({ color: 0x3dd6c4, transparent: true, opacity: 0.6 }));
bodyGroup.add(rangeArc);

// obstacle boxes
const boxPool = [];
const unitBox = new THREE.BoxGeometry(1, 1, 1);
const unitEdges = new THREE.EdgesGeometry(unitBox);
function getBox(i) {
  if (!boxPool[i]) {
    const mesh = new THREE.Mesh(unitBox, new THREE.MeshBasicMaterial({ transparent: true, opacity: 0.18, depthWrite: false }));
    const edges = new THREE.LineSegments(unitEdges, new THREE.LineBasicMaterial({ transparent: true }));
    mesh.add(edges);
    mesh.userData.edges = edges;
    bodyGroup.add(mesh);
    boxPool[i] = mesh;
  }
  return boxPool[i];
}

// scene geometry from the simulator
const movers = new Map();
function buildWorld(sceneDef) {
  worldGroup.clear();
  movers.clear();
  if (!sceneDef) return;
  const mk = (geom, material, opacity = 0.32) => {
    const m = new THREE.Mesh(geom, new THREE.MeshStandardMaterial({ color: MAT_COLOR[material] ?? 0x8a96a6, transparent: true, opacity, depthWrite: false, roughness: 0.8 }));
    m.add(new THREE.LineSegments(new THREE.EdgesGeometry(geom, 25), new THREE.LineBasicMaterial({ color: MAT_COLOR[material] ?? 0x8a96a6, transparent: true, opacity: 0.55 })));
    return m;
  };
  for (const o of sceneDef.objects) {
    let m;
    if (o.type === 'box') {
      m = mk(new THREE.BoxGeometry(o.w, o.h, o.d), o.material);
      m.position.copy(W(o.x, (o.y0 || 0) + o.h / 2, o.z));
      m.rotation.y = -(o.yaw || 0) * DEG;
    } else if (o.type === 'cylinder') {
      m = mk(new THREE.CylinderGeometry(o.r, o.r, o.y1 - o.y0, 24), o.material);
      m.position.copy(W(o.x, (o.y0 + o.y1) / 2, o.z));
    } else if (o.type === 'sphere') {
      m = mk(new THREE.SphereGeometry(o.r, 24, 16), o.material, 0.22);
      m.position.copy(W(o.x, o.y, o.z));
    } else if (o.type === 'pit') {
      const g = new THREE.BoxGeometry(o.x1 - o.x0, o.depth, o.z1 - o.z0);
      m = new THREE.Mesh(g, new THREE.MeshBasicMaterial({ color: 0x000000, transparent: true, opacity: 0.6, depthWrite: false }));
      m.add(new THREE.LineSegments(new THREE.EdgesGeometry(g), new THREE.LineBasicMaterial({ color: 0xa98bff })));
      m.position.copy(W((o.x0 + o.x1) / 2, -o.depth / 2, (o.z0 + o.z1) / 2));
    } else if (o.type === 'pedestrian') {
      m = new THREE.Group();
      const b = mk(new THREE.CylinderGeometry(0.22, 0.22, 1.45, 20), 'fabric', 0.45);
      b.position.y = 0.725;
      const h = mk(new THREE.SphereGeometry(0.12, 16, 12), 'skin', 0.55);
      h.position.y = 1.6;
      m.add(b, h);
      m.position.copy(W(o.path[0][0], 0, o.path[0][1]));
      movers.set(o.name, m);
    }
    if (m) worldGroup.add(m);
  }
}

// ------------------------------------------------------------ frame update
const turboLUT = new Uint8Array(256 * 3);
for (let i = 0; i < 256; i++) {
  const t = i / 255;
  const r = 34.61 + t * (1172.33 + t * (-10793.56 + t * (33300.12 + t * (-38394.49 + t * 14825.05))));
  const g = 23.31 + t * (557.33 + t * (1225.33 + t * (-3574.96 + t * (1073.77 + t * 707.56))));
  const b = 27.2 + t * (3211.1 + t * (-15327.97 + t * (27814 + t * (-22569.18 + t * 6838.66))));
  turboLUT.set([Math.max(0, Math.min(255, r)), Math.max(0, Math.min(255, g)), Math.max(0, Math.min(255, b))], i * 3);
}
const dctx = $('depth').getContext('2d');
let dimg = null;

function onFrame(s, depth) {
  st = s;
  const pose = s.pose || { x: 0, z: 0, yaw: 0, pitch: s.mount.pitch, height: s.mount.height, body_x: 0, body_z: -0.12 };
  bodyGroup.position.set(pose.x, 0, -pose.z);
  bodyGroup.rotation.y = -pose.yaw * DEG;
  avatar.position.set(pose.body_x ?? pose.x, 0, -(pose.body_z ?? pose.z));
  avatar.rotation.y = -pose.yaw * DEG;
  avatar.visible = !!s.pose && view !== 'sensor';
  sensorBox.position.set(0, pose.height ?? s.mount.height, 0);
  for (const mv of s.movers || []) {
    const m = movers.get(mv.name);
    if (m) m.position.set(mv.x, 0, -mv.z);
  }
  updatePoints(s, depth);
  updateDepthImage(s, depth);
  updateBoxes(s);
  updateFrustum(s, pose);
  drawRadar(s);
  updatePanels(s);
}

function updatePoints(s, depth) {
  const { fx, fy, cx, cy, stride, w, h } = s.intr;
  const N = w * h;
  if (pts.n !== N) {
    pts.n = N;
    pts.pos = new Float32Array(N * 3);
    pts.col = new Float32Array(N * 3);
    pts.geom.setAttribute('position', new THREE.BufferAttribute(pts.pos, 3));
    pts.geom.setAttribute('color', new THREE.BufferAttribute(pts.col, 3));
  }
  const P = hello.processing;
  const gtol = P.ground_tolerance, dthr = P.drop_threshold, top = P.user_height + P.head_margin;
  const a = -s.mount.pitch * DEG, c = Math.cos(a), sn = Math.sin(a), H = s.mount.height;
  const [nx, ny, nz, nd] = s.ground.plane;
  const byDepth = $('t-depthcolor').checked;
  const pos = pts.pos, col = pts.col;
  let k = 0;
  for (let v = 0; v < h; v++) {
    const yr = (v * stride - cy) / fy;
    for (let u = 0; u < w; u++) {
      const mm = depth[v * w + u];
      if (!mm) continue;
      const z = mm * 0.001;
      const xc = ((u * stride - cx) / fx) * z, yc = yr * z;
      const X = xc, Y = -c * yc - sn * z + H, Z = -sn * yc + c * z;
      const hp = nx * X + ny * Y + nz * Z + nd;
      const i = k * 3;
      pos[i] = X; pos[i + 1] = hp; pos[i + 2] = -Z;
      let r, g, b;
      if (byDepth) {
        const t = Math.max(0, Math.min(255, ((z - 0.5) / 4.0) * 255)) | 0;
        r = turboLUT[t * 3] / 255; g = turboLUT[t * 3 + 1] / 255; b = turboLUT[t * 3 + 2] / 255;
      } else if (hp < -dthr) { r = 0.66; g = 0.55; b = 1.0; }
      else if (hp <= gtol) { const f = 0.55 - Math.min(0.3, Z * 0.06); r = 0.27 * f * 2; g = 0.34 * f * 2; b = 0.42 * f * 2; }
      else if (hp > top) { r = 0.25; g = 0.28; b = 0.33; }
      else {
        const t = Math.min(1, hp / 1.9);
        if (t < 0.5) { const q = t / 0.5; r = 0.27 + q * 0.73; g = 0.78 - q * 0.09; b = 0.96 - q * 0.84; }
        else { const q = (t - 0.5) / 0.5; r = 1.0; g = 0.69 - q * 0.38; b = 0.12 + q * 0.36; }
      }
      col[i] = r; col[i + 1] = g; col[i + 2] = b;
      k++;
    }
  }
  pts.geom.setDrawRange(0, k);
  pts.geom.attributes.position.needsUpdate = true;
  pts.geom.attributes.color.needsUpdate = true;
  pts.obj.visible = $('t-points').checked;
}

function updateDepthImage(s, depth) {
  const { w, h } = s.intr;
  const cv = $('depth');
  if (cv.width !== w || cv.height !== h) { cv.width = w; cv.height = h; dimg = null; }
  if (!dimg) dimg = dctx.createImageData(w, h);
  const d = dimg.data;
  for (let i = 0; i < w * h; i++) {
    const mm = depth[i];
    const j = i * 4;
    if (!mm) { d[j] = d[j + 1] = d[j + 2] = 0; }
    else {
      const t = Math.max(0, Math.min(255, ((mm - 500) / 4000) * 255)) | 0;
      d[j] = turboLUT[t * 3]; d[j + 1] = turboLUT[t * 3 + 1]; d[j + 2] = turboLUT[t * 3 + 2];
    }
    d[j + 3] = 255;
  }
  dctx.putImageData(dimg, 0, 0);
}

function updateBoxes(s) {
  const cued = new Map((s.cues || []).map((c) => [c.id, c.rank]));
  const reported = new Set(s.reported);
  let i = 0;
  if ($('t-boxes').checked) {
    for (const t of s.tracks) {
      const m = getBox(i++);
      const [x0, x1, z0, z1] = t.box;
      const y0 = t.kind === 'drop' ? Math.min(t.h_min, -0.05) : t.h_min, y1 = t.kind === 'drop' ? 0.02 : t.h_max;
      m.position.set((x0 + x1) / 2, (y0 + y1) / 2, -(z0 + z1) / 2);
      m.scale.set(Math.max(0.05, x1 - x0), Math.max(0.03, y1 - y0), Math.max(0.05, z1 - z0));
      const col = (t.in_path || reported.has(t.id)) ? KIND[t.kind]?.color ?? 0xffffff : 0x5b6878;
      m.material.color.setHex(col);
      m.userData.edges.material.color.setHex(cued.has(t.id) ? 0xffffff : col);
      m.material.opacity = cued.has(t.id) ? 0.35 : t.confirmed ? 0.16 : 0.05;
      m.userData.edges.material.opacity = t.confirmed ? 1 : 0.3;
      m.visible = true;
    }
  }
  for (; i < boxPool.length; i++) boxPool[i].visible = false;
}

function updateFrustum(s, pose) {
  const { fx, fy, cx, cy, stride, w, h } = s.intr;
  const W0 = w * stride, H0 = h * stride;
  const D = hello.sensor.max_depth;
  const a = -(pose.pitch ?? s.mount.pitch) * DEG, c = Math.cos(a), sn = Math.sin(a), Hh = pose.height ?? s.mount.height;
  const toB = (u, v) => { const xc = (u - cx) / fx * D, yc = (v - cy) / fy * D; return [xc, -c * yc - sn * D + Hh, -(-sn * yc + c * D)]; };
  const o = [0, Hh, 0];
  const cs = [toB(0, 0), toB(W0, 0), toB(W0, H0), toB(0, H0)];
  const arr = frustum.geometry.attributes.position.array;
  let k = 0;
  const put = (p) => { arr[k++] = p[0]; arr[k++] = p[1]; arr[k++] = p[2]; };
  for (let i = 0; i < 4; i++) { put(o); put(cs[i]); put(cs[i]); put(cs[(i + 1) % 4]); }
  frustum.geometry.attributes.position.needsUpdate = true;
  frustum.visible = $('t-frustum').checked;
  const cw = hello.processing.corridor_half_width, rng = s.range_m;
  corridor.scale.set(cw * 2, rng, 1);
  corridor.position.set(0, 0.01, -rng / 2);
  const ap = [];
  const half = Math.atan2(W0 / 2, fx);
  for (let i = 0; i <= 24; i++) { const t = -half + (2 * half * i) / 24; ap.push(new THREE.Vector3(Math.sin(t) * rng, 0.02, -Math.cos(t) * rng)); }
  rangeArc.geometry.setFromPoints(ap);
}

// ----------------------------------------------------------------- radar
const rctx = $('radar').getContext('2d');
function cueInterval(r, rank, rng) {
  const base = r <= 0.6 ? 0.075 : 0.11 + 0.79 * Math.pow(Math.min(1, Math.max(0, (r - 0.6) / Math.max(0.3, rng - 0.6))), 1.2);
  return base * (rank ? 1.35 : 1);
}
function drawRadar(s) {
  const cv = $('radar'), g = rctx, Wd = cv.width, Hd = cv.height;
  const ox = Wd / 2, oy = Hd - 10, scale = (Hd - 22) / 4.5;
  g.clearRect(0, 0, Wd, Hd);
  const half = Math.atan2((s.intr.w * s.intr.stride) / 2, s.intr.fx);
  g.fillStyle = 'rgba(61,214,196,0.06)';
  g.beginPath(); g.moveTo(ox, oy); g.arc(ox, oy, 4.5 * scale, -Math.PI / 2 - half, -Math.PI / 2 + half); g.closePath(); g.fill();
  g.strokeStyle = '#243040'; g.fillStyle = '#5b6878'; g.font = '10px ui-sans-serif'; g.lineWidth = 1;
  for (let r = 1; r <= 4; r++) { g.beginPath(); g.arc(ox, oy, r * scale, Math.PI, 2 * Math.PI); g.stroke(); g.fillText(`${r} m`, ox + r * scale * 0.71 + 2, oy - r * scale * 0.71); }
  const cw = hello.processing.corridor_half_width * scale;
  g.strokeStyle = 'rgba(61,214,196,0.35)'; g.setLineDash([3, 3]);
  g.beginPath(); g.moveTo(ox - cw, oy); g.lineTo(ox - cw, 6); g.moveTo(ox + cw, oy); g.lineTo(ox + cw, 6); g.stroke();
  g.setLineDash([]);
  g.strokeStyle = 'rgba(61,214,196,0.9)'; g.lineWidth = 1.5;
  g.beginPath(); g.arc(ox, oy, s.range_m * scale, -Math.PI / 2 - half, -Math.PI / 2 + half); g.stroke();
  if (s.settings.mode === 'warning') {
    g.strokeStyle = 'rgba(255,84,104,0.8)'; g.setLineDash([4, 3]);
    g.beginPath(); g.arc(ox, oy, s.settings.warning_distance * scale, -Math.PI / 2 - half, -Math.PI / 2 + half); g.stroke(); g.setLineDash([]);
  }
  const cued = new Map((s.cues || []).map((c) => [c.id, c]));
  const now = performance.now() / 1000;
  for (const t of s.tracks) {
    const px = ox + t.x * scale, py = oy - t.z * scale;
    const col = '#' + ((t.in_path ? KIND[t.kind]?.color : 0x5b6878) ?? 0xffffff).toString(16).padStart(6, '0');
    const wpx = Math.max(5, t.width * scale);
    g.globalAlpha = t.confirmed ? 1 : 0.35;
    g.fillStyle = col;
    g.fillRect(px - wpx / 2, py - 3, wpx, 6);
    const c = cued.get(t.id);
    if (c) {
      const per = cueInterval(t.r, c.rank, s.range_m);
      const ph = (now % per) / per;
      g.strokeStyle = col; g.lineWidth = 2; g.globalAlpha = 1 - ph;
      g.beginPath(); g.arc(px, py, 6 + ph * 14, 0, 2 * Math.PI); g.stroke();
    }
    g.globalAlpha = 1;
  }
  g.fillStyle = '#3dd6c4'; g.beginPath(); g.arc(ox, oy, 4, 0, 2 * Math.PI); g.fill();
}

// ----------------------------------------------------------------- panels
function pill(id, text, cls = '') { const el = $(id); el.textContent = text; el.className = 'pill ' + cls; }
const sideTxt = (az) => (Math.abs(az) < 4 ? 'ahead' : `${Math.abs(az).toFixed(0)}° ${az > 0 ? 'right' : 'left'}`);

function updatePanels(s) {
  const m = s.metrics;
  const hl = { ok: ['sensor ok', 'ok'], blocked: ['SENSOR BLOCKED', 'bad'], no_data: ['NO SENSOR DATA', 'bad'], starting: ['starting', 'warn'] }[s.health] || [s.health, ''];
  pill('p-health', hl[0], hl[1]);
  pill('p-fps', `${m.fps.toFixed(0)} fps`, m.fps >= 15 ? '' : 'warn');
  pill('p-proc', `${m.process_ms ? m.process_ms.mean.toFixed(1) : '—'} ms`);
  const e = m.e2e_ms;
  pill('p-e2e', `e2e ${e ? e.mean.toFixed(0) + ' ms' : '—'}`, e ? (e.p95 <= m.budget_ms ? 'ok' : 'bad') : '');
  pill('p-batt', `${s.battery.percent}%${s.battery.charging ? ' ⚡' : ''}`, s.battery.percent <= 10 ? 'bad' : s.battery.percent <= 20 ? 'warn' : '');
  for (const b of $('sources').children) b.classList.toggle('on', b.dataset.id === s.source);

  // cue HUD
  const hud = $('cue-hud');
  const set = s.settings;
  let html;
  if (!set.power) html = `<div class="cue quiet"><div class="big">Device off</div><div class="sub">press Power (P)</div></div>`;
  else if (s.health === 'blocked' || s.health === 'no_data') html = `<div class="cue alert"><div class="big">${s.health === 'blocked' ? 'Sensor blocked' : 'No sensor data'}</div><div class="sub">alarm playing — user falls back to the cane</div></div>`;
  else if (s.training.active) {
    const t = s.training.target;
    html = `<div class="cue"><div class="big">Training ${t ? `· ${t.kind} · ${t.r} m · ${sideTxt(t.az)}` : '· blind trial'}</div><div class="sub">${t ? 'stationary practice target' : 'click the dial where the sound seems to be'}</div></div>`;
  } else if (s.cues.length) {
    const c = s.cues[0], k = KIND[c.kind] || KIND.body;
    const per = cueInterval(c.r, 0, s.range_m);
    html = `<div class="cue" style="border-left-color:#${k.color.toString(16).padStart(6, '0')}"><div class="big">${k.label} · ${c.r.toFixed(2)} m · ${sideTxt(c.az)}</div>` +
      `<div class="sub">${k.sound}, every ${per.toFixed(2)} s · elevation ${c.el >= 0 ? '+' : ''}${c.el.toFixed(0)}°</div>` +
      (s.cues[1] ? `<div class="sub">also: ${KIND[s.cues[1].kind]?.label} ${s.cues[1].r.toFixed(2)} m ${sideTxt(s.cues[1].az)} (quieter)</div>` : '') + '</div>';
  } else html = `<div class="cue quiet"><div class="big">Path clear</div><div class="sub">${set.mode === 'warning' ? `silent until something is within ${set.warning_distance} m` : `nothing in the corridor within ${s.range_m} m`}</div></div>`;
  hud.innerHTML = html;

  const t = s.timings;
  $('frame-meta').textContent = `#${s.seq} · ${s.intr.w}×${s.intr.h} (÷${s.intr.stride}) · valid ${(s.valid_ratio * 100).toFixed(0)}% · ` +
    `project ${t.project} · ground ${t.ground} · cluster ${t.cluster} · track ${t.track} ms` +
    (m.data_age_ms ? ` · cue data age ${m.data_age_ms.mean.toFixed(0)} ms` : '');

  $('settings-kv').innerHTML = [
    ['Power', set.power ? 'on' : 'off'], ['Volume', `${set.volume} / 10`], ['Range', `${set.range} (${s.range_m} m)`],
    ['Mode', set.mode], ['Sound', set.profile], ['Audio out', s.audio.driver], ['Speech', s.audio.speech],
    ['Peak', `${s.audio.peak_dbfs.toFixed(1)} dBFS${s.audio.limiting ? ' · limiting' : ''}`],
  ].map(([a, b]) => `<dt>${a}</dt><dd>${b}</dd>`).join('');
  $('b-power').classList.toggle('on', set.power);
  $('b-training').classList.toggle('on', s.training.active);
  $('b-range').innerHTML = `Range: ${set.range} <kbd>R</kbd>`;
  $('b-mode').innerHTML = `Mode: ${set.mode} <kbd>M</kbd>`;
  $('b-profile').innerHTML = `Sound: ${set.profile} <kbd>O</kbd>`;

  $('sim-card').hidden = s.source !== 'sim';
  $('contacts').textContent = `${s.contacts} contact${s.contacts === 1 ? '' : 's'} with obstacles`;
  syncSlider('m-height', s.mount.height, 'o-height', (v) => `${(+v).toFixed(2)} m`);
  syncSlider('m-pitch', s.mount.pitch, 'o-pitch', (v) => `${(+v).toFixed(1)}°`);
  syncSlider('c-warn', set.warning_distance, 'o-warn', (v) => `${(+v).toFixed(1)} m`);
  syncSlider('s-batt', s.battery.percent, 'o-batt', (v) => `${(+v).toFixed(0)}%`);
  $('c-offpath').checked = set.report_off_path;
  $('mirror-wrap').hidden = s.source !== 'kinect2';
  $('ground-est').textContent = s.ground.ok ? `floor says ${s.ground.height.toFixed(2)} m, ${s.ground.pitch.toFixed(1)}°` : 'floor not visible';
  $('b-log').textContent = s.logging ? 'Stop logging' : 'Start logging';
  $('b-log').classList.toggle('on', s.logging);
  $('log-path').textContent = s.logging ? s.log_path : '';

  const tr = s.training;
  $('t-start').textContent = tr.active ? 'Stop' : 'Start';
  $('t-start').classList.toggle('on', tr.active);
  const sm = tr.summary;
  $('t-summary').textContent = sm.trials ? `${sm.trials} trial${sm.trials > 1 ? 's' : ''} · mean angular error ${sm.mae_deg}°` : 'No trials yet';
  drawDial(tr);
}

const editing = new Set();
function syncSlider(id, val, outId, fmt) {
  const el = $(id);
  if (!editing.has(id)) el.value = val;
  $(outId).textContent = fmt(el.value);
}

// ------------------------------------------------------------- training
const dctxDial = $('dial').getContext('2d');
let lastResponse = null;
function drawDial(tr) {
  const cv = $('dial'), g = dctxDial, Wd = cv.width, Hd = cv.height, ox = Wd / 2, oy = Hd - 12, R = Hd - 26;
  g.clearRect(0, 0, Wd, Hd);
  g.strokeStyle = '#243040'; g.lineWidth = 1;
  g.beginPath(); g.arc(ox, oy, R, Math.PI, 2 * Math.PI); g.stroke();
  g.fillStyle = '#5b6878'; g.font = '10px ui-sans-serif'; g.textAlign = 'center';
  for (const a of [-90, -60, -45, -30, -15, 0, 15, 30, 45, 60, 90]) {
    const t = (a - 90) * DEG;
    g.beginPath(); g.moveTo(ox + Math.cos(t) * (R - 5), oy + Math.sin(t) * (R - 5)); g.lineTo(ox + Math.cos(t) * R, oy + Math.sin(t) * R); g.stroke();
    if (a % 30 === 0) g.fillText(`${a > 0 ? '+' : ''}${a}`, ox + Math.cos(t) * (R + 9), oy + Math.sin(t) * (R + 9) + 3);
  }
  const ray = (az, color, w) => { const t = (az - 90) * DEG; g.strokeStyle = color; g.lineWidth = w; g.beginPath(); g.moveTo(ox, oy); g.lineTo(ox + Math.cos(t) * R, oy + Math.sin(t) * R); g.stroke(); };
  if (tr.target) ray(tr.target.az, '#3dd6c4', 2);
  if (lastResponse) ray(lastResponse, '#ffb020', 2);
  g.fillStyle = '#dbe4ee'; g.beginPath(); g.arc(ox, oy, 4, 0, 2 * Math.PI); g.fill();
  g.textAlign = 'left';
  g.fillStyle = '#8796a8';
  g.fillText(tr.active ? (tr.blind ? 'blind trial: click where you hear it' : 'teal = target, amber = last answer') : 'start training to use', 6, 12);
}
$('dial').addEventListener('click', (e) => {
  if (!st || !st.training.active) return;
  const r = $('dial').getBoundingClientRect();
  const x = ((e.clientX - r.left) / r.width) * $('dial').width - $('dial').width / 2;
  const y = $('dial').height - 12 - ((e.clientY - r.top) / r.height) * $('dial').height;
  const az = Math.atan2(x, Math.max(0, y)) / DEG;
  lastResponse = az;
  send({ t: 'training', cmd: 'respond', az });
});
for (const a of [-60, -45, -30, -15, 0, 15, 30, 45, 60]) {
  const b = document.createElement('button');
  b.textContent = `${a > 0 ? '+' : ''}${a}°`;
  b.onclick = () => send({ t: 'training', cmd: 'target', az: a, r: +$('t-r').value, kind: $('t-kind').value });
  $('t-az').appendChild(b);
}
$('t-r').oninput = () => { $('o-tr').textContent = `${$('t-r').value} m`; send({ t: 'training', cmd: 'target', r: +$('t-r').value }); };
$('t-kind').onchange = () => send({ t: 'training', cmd: 'target', kind: $('t-kind').value });
$('t-start').onclick = () => send({ t: 'training', cmd: st && st.training.active ? 'stop' : 'start' });
$('t-blind').onclick = () => { if (!st?.training.active) send({ t: 'training', cmd: 'start' }); lastResponse = null; send({ t: 'training', cmd: 'random' }); };

// --------------------------------------------------------------- hello
function onHello(h) {
  hello = h;
  $('profile').textContent = h.profile || 'dev';
  const seg = $('sources');
  seg.innerHTML = '';
  for (const s of h.sources) {
    const b = document.createElement('button');
    b.textContent = s.label;
    b.dataset.id = s.id;
    b.disabled = !s.available;
    b.title = s.available ? `Use ${s.label}` : `${s.label}: ${s.note}`;
    b.classList.toggle('on', s.id === h.source);
    b.onclick = () => { toast(`Opening ${s.label}…`); send({ t: 'source', value: s.id }); };
    seg.appendChild(b);
  }
  const sel = $('scene');
  sel.innerHTML = h.scenes.map((s) => `<option value="${s.id}">${s.name}</option>`).join('');
  if (h.scene) {
    sel.value = h.scene.id;
    $('scene-desc').textContent = h.scene.description || '';
  }
  if (h.sim) {
    $('s-autowalk').checked = h.sim.autowalk;
    $('s-bob').checked = h.sim.head_bob;
    $('s-blocked').checked = h.sim.blocked;
    $('s-sun').value = h.sim.sun; $('o-sun').textContent = h.sim.sun;
  }
  buildWorld(h.scene);
  grid.visible = true;
  for (const ev of h.events || []) addEvent(ev, true);
}

// --------------------------------------------------------------- events
function addEvent(ev, quiet = false) {
  if (ev.kind === 'scene' && ev.scene) { buildWorld(ev.scene); }
  const li = document.createElement('li');
  li.className = ev.kind;
  const d = new Date(ev.t * 1000);
  li.innerHTML = `<time>${d.toTimeString().slice(0, 8)}</time>`;
  li.appendChild(document.createTextNode(ev.text));
  const list = $('events');
  list.prepend(li);
  while (list.children.length > 120) list.lastChild.remove();
  if (!quiet && (ev.kind === 'error' || ev.kind === 'warning' || ev.kind === 'contact')) toast(ev.text);
}
let toastTimer;
function toast(text) {
  const el = $('toast');
  el.textContent = text;
  el.classList.add('show');
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.classList.remove('show'), 2600);
}

// ------------------------------------------------------------- controls
document.querySelectorAll('[data-action]').forEach((b) => (b.onclick = () => send({ t: 'action', action: b.dataset.action })));
$('scene').onchange = () => send({ t: 'sim', key: 'scene', value: $('scene').value });
$('s-autowalk').onchange = (e) => send({ t: 'sim', key: 'autowalk', value: e.target.checked });
$('s-bob').onchange = (e) => send({ t: 'sim', key: 'head_bob', value: e.target.checked });
$('s-blocked').onchange = (e) => send({ t: 'sim', key: 'blocked', value: e.target.checked });
$('s-sun').oninput = (e) => { $('o-sun').textContent = e.target.value; send({ t: 'sim', key: 'sun', value: +e.target.value }); };
$('s-reset').onclick = () => send({ t: 'sim', key: 'reset' });
$('m-cal').onclick = () => send({ t: 'setting', key: 'calibrate_mount' });
$('c-offpath').onchange = (e) => send({ t: 'setting', key: 'report_off_path', value: e.target.checked });
$('c-mirror').onchange = (e) => send({ t: 'setting', key: 'flip_horizontal', value: e.target.checked });
const sliderSetting = (id, key, simKey) => {
  const el = $(id);
  el.addEventListener('pointerdown', () => editing.add(id));
  el.addEventListener('pointerup', () => setTimeout(() => editing.delete(id), 400));
  el.addEventListener('input', () => send(simKey ? { t: 'sim', key: simKey, value: +el.value } : { t: 'setting', key, value: +el.value }));
};
sliderSetting('m-height', 'mount_height');
sliderSetting('m-pitch', 'mount_pitch');
sliderSetting('c-warn', 'warning_distance');
sliderSetting('s-batt', 'battery');

document.querySelectorAll('#views button').forEach((b) => (b.onclick = () => setView(b.dataset.view)));
function setView(v) {
  view = v;
  document.querySelectorAll('#views button').forEach((b) => b.classList.toggle('on', b.dataset.view === v));
  controls.enabled = v === 'free';
  if (v === 'free' && st) {
    const p = bodyGroup.localToWorld(new THREE.Vector3(0, 0.8, -1.5));
    controls.target.copy(p);
  }
}

// keyboard: walking (simulator) + device buttons
const keys = new Set();
const KEY_ACTION = { p: 'power', '=': 'volume_up', '+': 'volume_up', '-': 'volume_down', r: 'range_next', m: 'mode_next', b: 'battery', i: 'status', t: 'training', o: 'profile_next' };
let lastMove = '';
function sendMove(force = false) {
  const f = (keys.has('w') || keys.has('arrowup') ? 1 : 0) - (keys.has('s') || keys.has('arrowdown') ? 1 : 0);
  const sd = (keys.has('d') ? 1 : 0) - (keys.has('a') ? 1 : 0);
  const turn = (keys.has('e') || keys.has('arrowright') ? 1 : 0) - (keys.has('q') || keys.has('arrowleft') ? 1 : 0);
  const k = `${f},${sd},${turn}`;
  if (k !== lastMove || force) { lastMove = k; send({ t: 'sim', key: 'move', value: { f, s: sd, turn } }); }
}
const isTyping = (e) => ['INPUT', 'SELECT', 'TEXTAREA'].includes(e.target.tagName) && e.target.type !== 'checkbox' && e.target.type !== 'range';
addEventListener('keydown', (e) => {
  if (isTyping(e) || e.metaKey || e.ctrlKey) return;
  const k = e.key.toLowerCase();
  if (['w', 'a', 's', 'd', 'q', 'e', 'arrowup', 'arrowdown', 'arrowleft', 'arrowright'].includes(k)) {
    e.preventDefault();
    keys.add(k);
    sendMove();
  } else if (KEY_ACTION[k] && !e.repeat) {
    send({ t: 'action', action: KEY_ACTION[k] });
  } else if (k === 'v' && !e.repeat) {
    const order = ['follow', 'sensor', 'top', 'free'];
    setView(order[(order.indexOf(view) + 1) % order.length]);
  }
});
addEventListener('keyup', (e) => { keys.delete(e.key.toLowerCase()); sendMove(); });
addEventListener('blur', () => { keys.clear(); sendMove(); });
setInterval(() => { if (keys.size) sendMove(true); }, 300);

// ------------------------------------------------------- browser audio
const audio = { on: false, ctx: null, next: 0 };
$('listen').onclick = async () => {
  audio.on = !audio.on;
  if (audio.on) {
    audio.ctx = audio.ctx || new AudioContext();
    await audio.ctx.resume();
    audio.next = 0;
    if (st && st.audio.driver.startsWith('local')) toast('The device audio is also playing on this computer\'s output');
  }
  $('listen').classList.toggle('on', audio.on);
  send({ t: 'audio', on: audio.on });
};
function playAudio(i16) {
  if (!audio.on || !audio.ctx) return;
  const n = i16.length / 2;
  const buf = audio.ctx.createBuffer(2, n, 24000);
  const L = buf.getChannelData(0), R = buf.getChannelData(1);
  for (let i = 0; i < n; i++) { L[i] = i16[2 * i] / 32768; R[i] = i16[2 * i + 1] / 32768; }
  const src = audio.ctx.createBufferSource();
  src.buffer = buf;
  src.connect(audio.ctx.destination);
  const now = audio.ctx.currentTime;
  if (audio.next < now + 0.03 || audio.next > now + 0.6) audio.next = now + 0.12;
  src.start(audio.next);
  audio.next += buf.duration;
}

// ---------------------------------------------------------------- render
function resize() {
  const r = $('viewport').getBoundingClientRect();
  renderer.setSize(r.width, r.height, false);
  camera.aspect = r.width / Math.max(1, r.height);
  camera.updateProjectionMatrix();
}
addEventListener('resize', resize);
resize();

const tmpA = new THREE.Vector3(), tmpB = new THREE.Vector3();
function animate() {
  requestAnimationFrame(animate);
  worldGroup.visible = $('t-world').checked;
  if (st) {
    const sh = st.pose?.height ?? st.mount.height;
    if (view === 'follow') {
      bodyGroup.localToWorld(tmpA.set(0.0, 2.6, 3.4));
      bodyGroup.localToWorld(tmpB.set(0, 0.9, -2.5));
      camera.position.lerp(tmpA, 0.15);
      camera.lookAt(tmpB);
    } else if (view === 'sensor') {
      const pitch = (st.pose?.pitch ?? st.mount.pitch) * DEG;
      bodyGroup.localToWorld(tmpA.set(0, sh, 0));
      bodyGroup.localToWorld(tmpB.set(0, sh + Math.sin(pitch) * 3, -Math.cos(pitch) * 3));
      camera.position.copy(tmpA);
      camera.lookAt(tmpB);
    } else if (view === 'top') {
      bodyGroup.localToWorld(tmpA.set(0, 9, -1.8));
      bodyGroup.localToWorld(tmpB.set(0, 0, -1.8));
      camera.position.lerp(tmpA, 0.2);
      camera.up.set(0, 1, 0);
      const fwd = bodyGroup.localToWorld(new THREE.Vector3(0, 0, -5)).sub(bodyGroup.position).normalize();
      camera.up.copy(fwd);
      camera.lookAt(tmpB);
    }
    if (view !== 'top') camera.up.set(0, 1, 0);
    camera.fov = view === 'sensor' ? 62 : 55;
    camera.updateProjectionMatrix();
  }
  if (controls.enabled) controls.update();
  renderer.render(scene, camera);
}
animate();
connect();
