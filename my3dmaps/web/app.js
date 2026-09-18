// my3dmaps web UI: Leaflet area/scale picker + Three.js colored preview + build jobs.
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';

const NICE_SCALES = [5000, 10000, 15000, 20000, 25000, 30000, 40000, 50000, 60000, 75000, 100000, 125000, 150000,
  200000, 250000, 300000, 400000, 500000, 750000, 1000000];
const $ = (id) => document.getElementById(id);

let cfg = null;          // ProjectConfig as JSON
let preview = null;      // last /api/preview payload
let exag = 1.0;
let previewTimer = null;
let previewInFlight = false;
let previewDirty = false;

// ------------------------------------------------------------------ helpers
async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : {
    method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || `${r.status} ${r.statusText}`);
  return r.json();
}
function setStatus(msg, isError = false) {
  $('status').textContent = msg; $('status').style.color = isError ? '#ff7b72' : '';
}
function b64ToArray(b64, Ctor) {
  const bin = atob(b64); const bytes = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
  return new Ctor(bytes.buffer);
}
function fmtKm(m) { return m >= 10000 ? `${(m / 1000).toFixed(0)} km` : `${(m / 1000).toFixed(1)} km`; }
function niceScale(s) {
  let best = NICE_SCALES[0], bd = Infinity;
  for (const n of NICE_SCALES) { const d = Math.abs(Math.log(n) - Math.log(Math.max(s, 1))); if (d < bd) { bd = d; best = n; } }
  return best;
}

// ------------------------------------------------------------------ map
const map = L.map('map', { zoomControl: true }).setView([46.3, 6.5], 8);
L.tileLayer('https://tile.openstreetmap.org/{z}/{x}/{y}.png', {
  maxZoom: 17, attribution: '&copy; OpenStreetMap contributors' }).addTo(map);
const gridLayer = L.layerGroup().addTo(map);
const centerMarker = L.marker([46.3, 6.5], { draggable: true, title: 'tile grid centre' }).addTo(map);
const handleIcon = L.divIcon({ className: 'handle', iconSize: [14, 14] });
const cornerHandle = L.marker([46.5, 6.9], { draggable: true, icon: handleIcon, title: 'drag to change scale' }).addTo(map);
let gridBounds = null;

centerMarker.on('drag', (e) => { const p = e.target.getLatLng(); cfg.center_lat = p.lat; cfg.center_lon = p.lng; syncCenterInputs(); });
centerMarker.on('dragend', () => { updateGrid(); schedulePreview(); });
cornerHandle.on('drag', (e) => {
  const p = e.target.getLatLng();
  const dLat = Math.abs(p.lat - cfg.center_lat), dLon = Math.abs(p.lng - cfg.center_lon);
  const halfH = dLat * 110574, halfW = dLon * 111320 * Math.cos(cfg.center_lat * Math.PI / 180);
  const needW = 2 * halfW / (cfg.tiles_x * cfg.tile_mm / 1000), needH = 2 * halfH / (cfg.tiles_y * cfg.tile_mm / 1000);
  const s = niceScale(Math.max(needW, needH));
  if (s !== cfg.scale) { cfg.scale = s; fillScaleSelect(); updateGrid(false); }
});
cornerHandle.on('dragend', () => { updateGrid(); schedulePreview(); });

async function updateGrid(moveHandle = true) {
  try {
    const g = await api('/api/grid', cfg);
    gridLayer.clearLayers();
    let minLat = 90, maxLat = -90, minLon = 180, maxLon = -180;
    for (const t of g.tiles) {
      L.polygon(t.corners, { color: '#4f9cf7', weight: 2, fillOpacity: 0.08 }).bindTooltip(`tile x${t.tx} y${t.ty}`).addTo(gridLayer);
      for (const [la, lo] of t.corners) { minLat = Math.min(minLat, la); maxLat = Math.max(maxLat, la); minLon = Math.min(minLon, lo); maxLon = Math.max(maxLon, lo); }
    }
    gridBounds = L.latLngBounds([minLat, minLon], [maxLat, maxLon]);
    if (moveHandle) cornerHandle.setLatLng([maxLat, maxLon]);
    const total = `${fmtKm(cfg.tiles_x * g.tile_km * 1000)} × ${fmtKm(cfg.tiles_y * g.tile_km * 1000)}`;
    $('areaInfo').textContent = `1:${cfg.scale.toLocaleString()} → ${g.tile_km.toFixed(g.tile_km < 10 ? 1 : 0)} km per tile edge, `
      + `${cfg.tiles_x * cfg.tiles_y} tile${cfg.tiles_x * cfg.tiles_y > 1 ? 's' : ''} covering ${total}; `
      + `mesh pitch ${cfg.pitch_mm} mm = ${g.pitch_m.toFixed(0)} m on the ground (SRTM is 30 m).`;
  } catch (e) { setStatus(`grid: ${e.message}`, true); }
}
function fitMap() { if (gridBounds) map.fitBounds(gridBounds.pad(0.25)); }
function syncCenterInputs() { $('centerLat').value = cfg.center_lat.toFixed(4); $('centerLon').value = cfg.center_lon.toFixed(4); }

// ------------------------------------------------------------------ controls
function fillScaleSelect() {
  const sel = $('scaleSelect'); sel.innerHTML = '';
  const vals = NICE_SCALES.includes(cfg.scale) ? NICE_SCALES : [...NICE_SCALES, cfg.scale].sort((a, b) => a - b);
  for (const s of vals) { const o = document.createElement('option'); o.value = s; o.textContent = s.toLocaleString(); if (s === cfg.scale) o.selected = true; sel.appendChild(o); }
}
$('scaleSelect').onchange = (e) => { cfg.scale = Number(e.target.value); updateGrid(); schedulePreview(); };
$('tilesX').onchange = (e) => { cfg.tiles_x = Math.max(1, Number(e.target.value) | 0); updateGrid(); schedulePreview(); };
$('tilesY').onchange = (e) => { cfg.tiles_y = Math.max(1, Number(e.target.value) | 0); updateGrid(); schedulePreview(); };
$('centerLat').onchange = $('centerLon').onchange = () => {
  cfg.center_lat = Number($('centerLat').value); cfg.center_lon = Number($('centerLon').value);
  centerMarker.setLatLng([cfg.center_lat, cfg.center_lon]); updateGrid(); fitMap(); schedulePreview(); };
$('btnFit').onclick = fitMap;
$('projName').onchange = (e) => { cfg.name = e.target.value.trim() || 'untitled'; };
$('baseColor').onchange = (e) => { cfg.palette.base_color = e.target.value; schedulePreview(); };
$('mono').onchange = (e) => { cfg.monochrome = e.target.checked; schedulePreview(); };
$('shrink').onchange = (e) => { cfg.shrinkage_pct = Number(e.target.value) || 0; };
$('rivers').onchange = (e) => { cfg.rivers.enabled = e.target.checked; schedulePreview(); };
$('riverStreams').onchange = (e) => { cfg.rivers.classes = e.target.checked ? ['river', 'canal', 'stream'] : ['river', 'canal']; schedulePreview(); };
$('riverWidth').onchange = (e) => { cfg.rivers.min_width_mm = Math.max(0.3, Number(e.target.value) || 1.2); e.target.value = cfg.rivers.min_width_mm; schedulePreview(); };
$('urban').onchange = (e) => { cfg.urban.enabled = e.target.checked; schedulePreview(); };
$('urbanRoads').onchange = (e) => { cfg.urban.roads = e.target.checked ? ['motorway', 'trunk', 'primary'] : []; schedulePreview(); };
$('urbanColor').onchange = (e) => { cfg.urban.color = e.target.value; schedulePreview(); };
$('riverScale').onchange = (e) => { cfg.rivers.width_scale = Math.max(0.25, Number(e.target.value) || 1); e.target.value = cfg.rivers.width_scale; schedulePreview(); };
$('overflow').onchange = (e) => { cfg.palette.overflow = e.target.value; schedulePreview(); };
$('exag').oninput = (e) => { exag = Number(e.target.value); $('exagAuto').checked = false; cfg.exaggeration = exag; applyExag(); };
$('exagAuto').onchange = (e) => { if (e.target.checked) { cfg.exaggeration = null; if (preview) { exag = preview.suggested_exaggeration; } applyExag(); } else cfg.exaggeration = exag; };
$('btnSuggest').onclick = () => { if (preview) { exag = preview.suggested_exaggeration; $('exagAuto').checked = true; cfg.exaggeration = null; applyExag(); } };

function applyExag() {
  $('exag').value = exag; $('exagVal').textContent = exag.toFixed(2);
  if (preview) {
    const h = cfg.base_mm + cfg.palette.skin_depth_mm + preview.relief_m * preview.mm_per_m * exag;
    const capped = exag > preview.max_exaggeration + 1e-9;
    $('heightInfo').innerHTML = `Relief ${preview.relief_m.toFixed(0)} m (${preview.elevation_min_m.toFixed(0)}–${preview.elevation_max_m.toFixed(0)} m a.s.l.) → `
      + `model ${h.toFixed(1)} mm tall incl. ${cfg.base_mm} mm base. Suggested ${preview.suggested_exaggeration.toFixed(2)}×; `
      + (capped ? `<b style="color:#ffb454">exceeds the ${preview.max_height_mm} mm budget – the build will cap at ${preview.max_exaggeration.toFixed(2)}×.</b>` : `max ${preview.max_exaggeration.toFixed(2)}× within the ${preview.max_height_mm} mm budget.`);
    rebuildModel();
  }
}

// ------------------------------------------------------------------ bands table
function renderBands() {
  const tb = $('bands').querySelector('tbody'); tb.innerHTML = '';
  const cls = preview ? preview.classification : null;
  const active = cls ? Object.fromEntries(cls.bands.map(b => [b.id, b])) : {};
  const mergedInto = {}; if (cls) for (const b of cls.bands) for (const m of b.merged) mergedInto[m] = b.id;
  const pruned = cls ? Object.fromEntries(cls.pruned.map(p => [p.id, p.reason])) : {};
  for (const b of cfg.palette.bands) {
    const tr = document.createElement('tr');
    const isWater = b.kind === 'water';
    const rel = b.from_water_m !== null && b.from_water_m !== undefined;
    let status = '<span class="chip">not evaluated</span>';
    if (cls) {
      if (active[b.id]) status = `<span class="chip ok">${(active[b.id].fraction * 100).toFixed(1)}% of area</span>` + (active[b.id].merged.length ? ` <span class="chip warn">+ ${active[b.id].merged.join(', ')}</span>` : '');
      else if (mergedInto[b.id]) status = `<span class="chip warn">merged into ${mergedInto[b.id]}</span>`;
      else if (pruned[b.id] !== undefined) status = `<span class="chip off" title="${pruned[b.id]}">dropped: ${pruned[b.id]}</span>`;
      else status = '<span class="chip off">absent</span>';
      if (!active[b.id]) tr.className = 'pruned';
    }
    tr.innerHTML = `<td><input type="color" value="${b.color}" data-id="${b.id}" data-k="color"></td>`
      + `<td>${b.label || b.id}<br><small style="color:#98a2b3">${b.id}</small></td>`
      + (isWater ? '<td>water surface</td>' : `<td><input class="thr" type="number" step="1" value="${rel ? b.from_water_m : (b.from_m ?? '')}" data-id="${b.id}" data-k="${rel ? 'from_water_m' : 'from_m'}">`
        + ` <select data-id="${b.id}" data-k="unit"><option value="abs" ${rel ? '' : 'selected'}>m a.s.l.</option><option value="rel" ${rel ? 'selected' : ''}>m above water</option></select></td>`)
      + (isWater ? '<td></td>' : `<td><input class="thr" type="number" step="1" min="0" max="89" placeholder="off" value="${b.slope_deg ?? ''}" data-id="${b.id}" data-k="slope_deg">°</td>`)
      + `<td>${status}</td>`;
    tb.appendChild(tr);
  }
  tb.querySelectorAll('input, select').forEach(el => el.addEventListener('change', onBandEdit));
  if (cls) {
    const merges = cls.merges.map(m => `${m.from} → ${m.into}`).join(', ');
    $('mergeInfo').textContent = `${cls.distinct_colors.length} filament color${cls.distinct_colors.length > 1 ? 's' : ''} `
      + `(${cls.distinct_colors.join(', ')})` + (merges ? `. Merged for slots: ${merges}.` : '.')
      + (cls.reference_water_m !== null ? ` Reference water level ${cls.reference_water_m.toFixed(0)} m.` : ' No water detected.')
      + riverText(cls);
    $('bandStatus').textContent = `${cls.bands.length} active`;
  }
}
function riverText(cls) {
  if (!cfg.rivers || !cfg.rivers.enabled) return ' Rivers off.';
  const r = cls.rivers;
  if (!r) return ' Rivers: not fetched.';
  if (r.error) return ` Rivers unavailable (${r.error}).`;
  const kinds = Object.entries(r.by_kind).map(([k, n]) => `${n} ${k}`).join(', ');
  const streamsNote = (cfg.rivers.classes.includes('stream') && cfg.scale > cfg.rivers.stream_max_scale) ? ` (streams skipped above 1:${cfg.rivers.stream_max_scale.toLocaleString()})` : '';
  return ` Rivers: ${r.features} OSM features${kinds ? ` (${kinds})` : ''}, ${r.length_km} km${streamsNote}, ${((cls.river_fraction || 0) * 100).toFixed(2)}% of the surface.`
    + urbanText(cls);
}
function urbanText(cls) {
  if (!cfg.urban || !cfg.urban.enabled) return '';
  const u = cls.urban;
  if (!u) return ' Urban: not fetched.';
  if (u.error) return ` Urban unavailable (${u.error}).`;
  const active = cls.bands.some(b => b.kind === 'urban');
  const dropped = cls.pruned.find(p => p.id === 'urban') || cls.merges.find(m => m.from === 'urban');
  return ` Urban: ${u.built_up_km2} km² built-up, ${u.road_km} km major roads`
    + (active ? `, ${((cls.urban_fraction || 0) * 100).toFixed(1)}% of the surface.` : ` – dropped (${dropped ? (dropped.reason || 'merged to fit the slots') : 'not present'}).`);
}
function onBandEdit(e) {
  const { id, k } = e.target.dataset; const b = cfg.palette.bands.find(x => x.id === id); if (!b) return;
  if (k === 'color') b.color = e.target.value;
  else if (k === 'unit') {
    const v = (b.from_m ?? b.from_water_m ?? 0);
    if (e.target.value === 'rel') { b.from_water_m = v; b.from_m = null; } else { b.from_m = v; b.from_water_m = null; }
  } else if (k === 'slope_deg') b.slope_deg = e.target.value === '' ? null : Number(e.target.value);
  else b[k] = Number(e.target.value);
  renderBands(); schedulePreview();
}

// ------------------------------------------------------------------ preview fetch
function schedulePreview(delay = 500) {
  clearTimeout(previewTimer); previewTimer = setTimeout(fetchPreview, delay);
}
async function fetchPreview() {
  if (previewInFlight) { previewDirty = true; return; }
  previewInFlight = true; previewDirty = false;
  setStatus('fetching elevation & water, classifying…');
  try {
    preview = await api('/api/preview', { config: cfg, nodes_per_tile: 96 });
    preview.elev = b64ToArray(preview.elev_b64, Float32Array); preview.cells = b64ToArray(preview.cells_b64, Uint8Array);
    if ($('exagAuto').checked) exag = preview.suggested_exaggeration;
    $('demInfo').textContent = `DEM tiles: ${preview.dem.tiles.join(', ') || 'none'}${preview.dem.missing_tiles.length ? ` (ocean/no data: ${preview.dem.missing_tiles.join(', ')})` : ''}; `
      + `${preview.dem.void_samples_filled} void samples filled. CRS ${preview.crs}. `
      + (preview.water ? `Water bodies: ${preview.water.bodies.map(b => `${b.kind} @ ${b.level_m.toFixed(0)} m`).join(', ') || 'none'}.` : 'water disabled.');
    renderBands(); applyExag();
    setStatus(`preview ready: ${preview.shape[1]}×${preview.shape[0]} nodes, ${preview.classification.bands.length} bands`);
  } catch (e) { setStatus(`preview failed: ${e.message}`, true); }
  previewInFlight = false;
  if (previewDirty) schedulePreview(100);
}

// ------------------------------------------------------------------ three.js viewer
const viewerEl = $('viewer');
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(window.devicePixelRatio);
viewerEl.appendChild(renderer.domElement);
const scene = new THREE.Scene(); scene.background = new THREE.Color(0x0e1115);
const camera = new THREE.PerspectiveCamera(40, 1, 1, 5000);
const controls = new OrbitControls(camera, renderer.domElement); controls.enableDamping = true;
scene.add(new THREE.HemisphereLight(0xffffff, 0x334455, 0.9));
const sun = new THREE.DirectionalLight(0xffffff, 1.1); sun.position.set(-300, 500, 200); scene.add(sun);
let modelGroup = new THREE.Group(); scene.add(modelGroup);
let fitted = false;
function resize() {
  const w = viewerEl.clientWidth, h = viewerEl.clientHeight; if (!w || !h) return;
  renderer.setSize(w, h, false); camera.aspect = w / h; camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(viewerEl); resize();
(function animate() { requestAnimationFrame(animate); controls.update(); renderer.render(scene, camera); })();

function hexToRgb(h) { const n = parseInt(h.slice(1), 16); return [((n >> 16) & 255) / 255, ((n >> 8) & 255) / 255, (n & 255) / 255]; }

function rebuildModel() {
  scene.remove(modelGroup); modelGroup = new THREE.Group(); scene.add(modelGroup);
  if (!preview) return;
  const [ny, nx] = preview.shape, n = preview.nodes_per_tile, [TX, TY] = preview.tiles;
  const pitch = preview.tile_mm / n, gap = 4, T = preview.tile_mm;
  const base = preview.base_mm, skin = preview.skin_mm, mmpm = preview.mm_per_m, datum = preview.z_datum_m;
  const zOf = (e) => base + skin + (e - datum) * mmpm * exag;
  const bands = preview.classification.bands;
  const bandRgb = bands.map(b => hexToRgb(b.color));
  const baseRgb = hexToRgb(preview.classification.base_color);
  const elevBands = bands.map((b, i) => ({ i, lower: b.lower_m })).filter(b => bands[b.i].kind === 'elevation');
  const elev = preview.elev, cells = preview.cells;
  const pos = [], col = [];
  const push = (x, z, gy, rgb) => { pos.push(x, z, -gy); col.push(rgb[0], rgb[1], rgb[2]); };

  for (let ty = 0; ty < TY; ty++) for (let tx = 0; tx < TX; tx++) {
    const ox = tx * (T + gap), oy = ty * (T + gap), j0 = ty * n, i0 = tx * n;
    // surface, flat-shaded per cell
    for (let j = j0; j < j0 + n; j++) for (let i = i0; i < i0 + n; i++) {
      const rgb = bandRgb[cells[j * (nx - 1) + i]] || [1, 0, 1];
      const x0 = ox + (i - i0) * pitch, x1 = x0 + pitch, y0 = oy + (j - j0) * pitch, y1 = y0 + pitch;
      const za = zOf(elev[j * nx + i]), zb = zOf(elev[j * nx + i + 1]), zc = zOf(elev[(j + 1) * nx + i + 1]), zd = zOf(elev[(j + 1) * nx + i]);
      if (Math.abs(za - zc) <= Math.abs(zb - zd)) {
        push(x0, za, y0, rgb); push(x1, zb, y0, rgb); push(x1, zc, y1, rgb);
        push(x0, za, y0, rgb); push(x1, zc, y1, rgb); push(x0, zd, y1, rgb);
      } else {
        push(x0, za, y0, rgb); push(x1, zb, y0, rgb); push(x0, zd, y1, rgb);
        push(x1, zb, y0, rgb); push(x1, zc, y1, rgb); push(x0, zd, y1, rgb);
      }
    }
    // walls: base color up to base_mm, then elevation slabs, then the skin color of the edge cell
    const wall = (xu, yu, xv, yv, eu, ev, cellIdx) => {
      const zu = zOf(eu), zv = zOf(ev), zmin = Math.min(zu, zv);
      const quad = (z0, z1u, z1v, rgb) => { push(xu, z0, yu, rgb); push(xv, z0, yv, rgb); push(xv, z1v, yv, rgb); push(xu, z0, yu, rgb); push(xv, z1v, yv, rgb); push(xu, z1u, yu, rgb); };
      quad(0, base, base, baseRgb);
      let zb = base, cur = 0;
      for (let k = 1; k < elevBands.length; k++) {
        const t = zOf(elevBands[k].lower);
        if (t <= zb) { cur = k; continue; }
        if (t >= zmin) break;
        quad(zb, t, t, bandRgb[elevBands[cur].i]); zb = t; cur = k;
      }
      quad(zb, zu, zv, bandRgb[cells[cellIdx]] || bandRgb[elevBands[cur].i]);
    };
    for (let i = i0; i < i0 + n; i++) {
      const x0 = ox + (i - i0) * pitch, x1 = x0 + pitch;
      wall(x0, oy, x1, oy, elev[j0 * nx + i], elev[j0 * nx + i + 1], j0 * (nx - 1) + i);                    // south
      wall(x0, oy + T, x1, oy + T, elev[(j0 + n) * nx + i], elev[(j0 + n) * nx + i + 1], (j0 + n - 1) * (nx - 1) + i); // north
    }
    for (let j = j0; j < j0 + n; j++) {
      const y0 = oy + (j - j0) * pitch, y1 = y0 + pitch;
      wall(ox, y0, ox, y1, elev[j * nx + i0], elev[(j + 1) * nx + i0], j * (nx - 1) + i0);                  // west
      wall(ox + T, y0, ox + T, y1, elev[j * nx + i0 + n], elev[(j + 1) * nx + i0 + n], j * (nx - 1) + i0 + n - 1); // east
    }
  }
  const geo = new THREE.BufferGeometry();
  geo.setAttribute('position', new THREE.Float32BufferAttribute(pos, 3));
  geo.setAttribute('color', new THREE.Float32BufferAttribute(col, 3));
  geo.computeVertexNormals();
  const mesh = new THREE.Mesh(geo, new THREE.MeshLambertMaterial({ vertexColors: true, side: THREE.DoubleSide }));
  modelGroup.add(mesh);
  const W = TX * (T + gap) - gap, H = TY * (T + gap) - gap;
  modelGroup.position.set(-W / 2, 0, H / 2);
  if (!fitted) {
    const d = Math.max(W, H) * 1.3; camera.position.set(-d * 0.5, d * 0.7, d * 0.8); controls.target.set(0, 10, 0); fitted = true;
  }
}

// ------------------------------------------------------------------ presets
async function loadPresets(selectName) {
  const list = await api('/api/presets');
  const sel = $('presetSelect'); sel.innerHTML = '<option value="">(choose)</option>';
  for (const p of list) { const o = document.createElement('option'); o.value = p.file; o.textContent = p.file; sel.appendChild(o); }
  if (selectName) sel.value = selectName;
  return list;
}
$('presetSelect').onchange = async (e) => { if (e.target.value) { applyConfig(await api(`/api/presets/${e.target.value}`)); fitted = false; await updateGrid(); fitMap(); schedulePreview(0); } };
$('btnSavePreset').onclick = async () => {
  const name = prompt('Preset name', cfg.name); if (!name) return;
  try { await api(`/api/presets/${encodeURIComponent(name)}`, cfg); await loadPresets(name); setStatus(`saved preset ${name}`); }
  catch (e) { setStatus(`save failed: ${e.message}`, true); }
};
function applyConfig(c) {
  cfg = c; preview = null;
  $('projName').value = cfg.name; $('tilesX').value = cfg.tiles_x; $('tilesY').value = cfg.tiles_y; $('tileMm').textContent = cfg.tile_mm;
  $('baseColor').value = cfg.palette.base_color; $('overflow').value = cfg.palette.overflow; $('mono').checked = !!cfg.monochrome;
  if (!cfg.rivers) cfg.rivers = { enabled: true, source: 'osm', classes: ['river', 'canal', 'stream'], stream_max_scale: 60000, min_width_mm: 1.2, width_scale: 1.0, depth_mm: 0.6, default_width_m: { river: 30, canal: 15, stream: 5 } };
  $('rivers').checked = !!cfg.rivers.enabled; $('riverStreams').checked = cfg.rivers.classes.includes('stream');
  $('riverWidth').value = cfg.rivers.min_width_mm; $('riverScale').value = cfg.rivers.width_scale;
  $('shrink').value = cfg.shrinkage_pct || 0;
  if (!cfg.urban) cfg.urban = { enabled: false, source: 'osm', color: '#4a4a4a', label: 'Urban', landuse: ['residential', 'industrial', 'commercial', 'retail'], roads: ['motorway', 'trunk', 'primary'], road_min_width_mm: 1.2, road_width_scale: 1.0, road_default_width_m: { motorway: 30, trunk: 20, primary: 12 }, depth_mm: 0.6 };
  $('urban').checked = !!cfg.urban.enabled; $('urbanRoads').checked = cfg.urban.roads.length > 0; $('urbanColor').value = cfg.urban.color;
  $('exagAuto').checked = cfg.exaggeration === null || cfg.exaggeration === undefined;
  if (!$('exagAuto').checked) exag = cfg.exaggeration;
  $('exag').value = exag; $('exagVal').textContent = exag.toFixed(2);
  centerMarker.setLatLng([cfg.center_lat, cfg.center_lon]); syncCenterInputs(); fillScaleSelect(); renderBands();
}

// ------------------------------------------------------------------ jobs
const jobsSeen = new Map();
async function startJob(slice) {
  try {
    const j = await api('/api/jobs', { config: cfg, slice });
    setStatus(`job ${j.id} started`); jobsSeen.set(j.id, j); renderJobs(); pollJobs();
  } catch (e) { setStatus(`build failed to start: ${e.message}`, true); }
}
$('btnBuild').onclick = () => startJob(false);
$('btnBuildSlice').onclick = () => startJob(true);
let polling = false;
async function pollJobs() {
  if (polling) return; polling = true;
  while ([...jobsSeen.values()].some(j => j.status === 'queued' || j.status === 'running')) {
    for (const id of jobsSeen.keys()) { try { jobsSeen.set(id, await api(`/api/jobs/${id}`)); } catch (_) { /* ignore */ } }
    renderJobs(); await new Promise(r => setTimeout(r, 1500));
  }
  polling = false;
}
function renderJobs() {
  const el = $('jobs'); el.innerHTML = '';
  for (const j of [...jobsSeen.values()].reverse()) {
    const d = document.createElement('div'); d.className = 'job';
    let html = `<b>${j.name}</b> <span class="chip ${j.status === 'done' ? 'ok' : j.status === 'error' ? 'off' : ''}">${j.status}</span> <small>${j.id}${j.slice ? ' · slicing' : ''}</small>`;
    if (j.status === 'running' || j.status === 'queued') html += `<progress value="${j.fraction}" max="1"></progress>`;
    if (j.error) html += `<div style="color:#ff7b72">${j.error}</div>`;
    if (j.summary) {
      const f = (p) => `/api/jobs/${j.id}/files/${p}`;
      html += `<div>Output: <code>${j.out_dir}</code> · <a href="${f('PRINT_PLAN.txt')}" target="_blank">PRINT_PLAN.txt</a> · <a href="${f('summary.json')}" target="_blank">summary.json</a> · exaggeration ${j.summary.exaggeration.toFixed(2)}× · ${j.summary.seconds}s</div>`;
      for (const t of j.summary.tiles) {
        html += `<div style="margin-top:6px"><b>${t.name}</b> (${t.height_mm} mm tall): <a href="${f(`${t.name}/${t.name}.3mf`)}">3MF</a>`;
        for (const o of t.objects) html += ` · <a href="${f(`${t.name}/${o.name}.stl`)}" title="slot ${o.extruder}"><span style="display:inline-block;width:10px;height:10px;background:${o.color};border:1px solid #666;vertical-align:middle"></span> ${o.name}.stl</a>`;
        const sw = t.plan.swaps;
        html += `<br><small>slots: ${t.objects.map(o => `${o.extruder}=${o.name}`).join(', ')}${sw.length ? ` · <b>${sw.length} manual swap${sw.length > 1 ? 's' : ''}</b>: ${sw.map(s => `z=${s.z_mm} mm slot ${s.slot} ${s.from_labels.join('+')}→${s.to_labels.join('+')}`).join('; ')}` : ' · no manual swaps'}</small>`;
        if (t.slice) html += `<br><small>slice: ${t.slice.ok ? `ok → <a href="${f(`${t.name}/sliced/${t.name}.gcode`)}">G-code</a>` : `<span style="color:#ffb454">${t.slice.message}</span>`} (<a href="${f(`${t.name}/sliced/slicer.log`)}" target="_blank">log</a>)</small>`;
        html += '</div>';
      }
    }
    if (j.log && j.log.length && j.status !== 'done') html += `<div class="log">${j.log.slice(-6).map(l => `[${l.stage}] ${l.message}`).join('\n')}</div>`;
    d.innerHTML = html; el.appendChild(d);
  }
}

// ------------------------------------------------------------------ boot
(async function boot() {
  try {
    const list = await loadPresets();
    const first = list.find(p => p.config);
    applyConfig(first ? first.config : await api('/api/default_config'));
    if (first) $('presetSelect').value = first.file;
    await updateGrid(); fitMap(); schedulePreview(0);
  } catch (e) { setStatus(`startup failed: ${e.message}`, true); }
})();
