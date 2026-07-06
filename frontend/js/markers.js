import { bucketColor } from "./confidence.js";
import { openPlacePanel, panelOnceClose } from "./panel.js";
import { onThemeChange } from "./theme.js";
import { bubbleSize, computeDiff, expandBbox, formatCount, prefersReducedMotion, sanitizeBbox } from "./viewport.js";

// ONE clustering engine: Mapbox Supercluster (vendored UMD -> global `Supercluster`), client-side.
// The whole active set is loaded once (main.js) and clustered per view here: clusters sit at the
// weighted CENTROID of their members and merge by proximity as you zoom, so there is no server grid,
// no fixed lattice, and one consistent behaviour at every zoom. Individual pins draw on a shared
// canvas (one paint pass, flat hit-testing); the few pins with an open community proposal draw on a
// shared SVG renderer so their .odc-pending keyframe (an animated SVG filter) still runs — canvas has
// no per-marker DOM node to animate.

let map = null;
let index = null;        // the Supercluster index (rebuilt on load / type-filter change)
let clusterLayer = null; // cluster bubbles (DOM divIcons)
let pointLayer = null;   // individual pins (canvas circleMarkers; svg for pending)
let canvasRenderer = null;
let svgRenderer = null;
let loadedFeatures = []; // the last feature set handed to loadPoints — kept so a breakpoint change
                         // (mobile<->desktop) can rebuild the index at the new cluster radius.

// id -> the live point circleMarker currently drawn, so consecutive renders DIFF instead of rebuild
// (a pan reuses survivors untouched) and the selection ring can be re-found by id after a rebuild.
const idMap = new Map();
let selectedId = null;

// Supercluster tuning. maxZoom 16 = clusters fully break into individual pins at street level;
// minPoints 2 = never bubble a single pin. The cluster radius is COARSER on a narrow phone (80px vs
// the desktop 60px) so a wide-zoom mobile view collapses into fewer, well-separated bubbles instead
// of an overlapping sprawl — paired with the smaller mobile bubble diameters (viewport.bubbleSize).
const SC_MAX_ZOOM = 16;
const MOBILE_MQ = (typeof window !== "undefined" && window.matchMedia)
  ? window.matchMedia("(max-width: 1023px)") : null;
const isMobile = () => !!(MOBILE_MQ && MOBILE_MQ.matches);
const scOpts = () => ({ radius: isMobile() ? 80 : 60, maxZoom: SC_MAX_ZOOM, minPoints: 2 });
// Cluster/paint a slightly larger box than the viewport so pins just off the edge exist for short
// pans (matches the canvas renderer's padding); the list stays honest to the exact viewport (main.js).
const RENDER_MARGIN = 1.4;

export function initMarkers(m) {
  map = m;
  // padding 0.2: pre-paint a small margin around the viewport without the 4x-area canvas that
  // padding 0.5 balloons to on retina screens (the 1.4x render margin covers longer off-edge pins).
  canvasRenderer = L.canvas({ tolerance: 6, padding: 0.2 }).addTo(map);
  svgRenderer = L.svg({ padding: 0.2 }).addTo(map);
  clusterLayer = L.layerGroup().addTo(map);
  pointLayer = L.layerGroup().addTo(map);
  index = null; idMap.clear(); selectedId = null;
  // A theme swap changes --accent; the canvas won't repaint the selected ring on its own, so re-style
  // the live selection when the theme flips.
  onThemeChange(() => { const m2 = idMap.get(selectedId); if (m2) m2.setStyle({ color: accentHex(), weight: 3.5 }); });
  // Crossing the mobile/desktop breakpoint changes the cluster radius, so rebuild the index and
  // repaint (bubble DIAMETER already adapts per-render via bubbleSize). Rare — a rotate or a resize.
  if (MOBILE_MQ) {
    const onBp = () => { if (loadedFeatures.length) { loadPoints(loadedFeatures); rerenderCurrent(); } };
    if (MOBILE_MQ.addEventListener) MOBILE_MQ.addEventListener("change", onBp);
    else if (MOBILE_MQ.addListener) MOBILE_MQ.addListener(onBp); // Safari <14
  }
}

// Repaint for the map's CURRENT bounds (used by the breakpoint rebuild; the normal path is main.js
// calling renderView on moveend).
function rerenderCurrent() {
  const b = map.getBounds();
  renderView(sanitizeBbox({ west: b.getWest(), south: b.getSouth(), east: b.getEast(), north: b.getNorth() }),
    map.getZoom());
}

// (Re)build the clustering index from a point-feature array. Called once after the initial load and
// again when the type filter changes the visible set — load() is a fresh kdbush build, a few ms for
// the whole US set, so re-filtering is instant with no server hit. Does NOT paint; the caller renders.
export function loadPoints(features) {
  loadedFeatures = features || [];
  const SC = window.Supercluster;
  index = SC ? new SC(scOpts()).load(loadedFeatures) : null;
}

// --- selection ring (canvas-safe) --------------------------------------------------------------
// A canvas circleMarker has no per-marker DOM <path>, so a CSS class can't ride it. Instead setStyle
// the tracked marker to the accent ring and restore its captured base style on deselect. Colors must
// be concrete (the canvas ctx can't resolve var()), so --accent is read at apply time (theme-aware).
function accentHex() {
  return getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#2b6cb0";
}
function applyBase(m) { if (m && m.setStyle && m.__odBase) m.setStyle(m.__odBase); }
function select(id, marker) {
  const prev = idMap.get(selectedId);
  if (prev && prev !== marker) applyBase(prev);
  selectedId = id;
  const m = marker || idMap.get(id);
  if (m && m.setStyle) m.setStyle({ color: accentHex(), weight: 3.5 });
}
function clearSelection() { applyBase(idMap.get(selectedId)); selectedId = null; }

// --- point markers -----------------------------------------------------------------------------
function makePointMarker(f) {
  const [lon, lat] = f.geometry.coordinates;
  const p = f.properties;
  const pending = !!p.has_pending;
  // Pending pins carry their violet stroke as their BASE style (so setStyle-selection can override it
  // and restore it); the keyframe still animates their filter on the SVG node.
  const base = pending ? { color: "#a855f7", weight: 2 } : { color: "#ffffff", weight: 2 };
  const marker = L.circleMarker([lat, lon], {
    renderer: pending ? svgRenderer : canvasRenderer,
    radius: 7, fillColor: bucketColor(p.bucket), fillOpacity: 1,
    ...base,
    className: pending ? "odc-pending" : "", // reaches the SVG <path> only; canvas ignores className
  });
  marker.__odId = p.id;
  marker.__odBase = base;
  marker.__odPending = pending; // so a has_pending flip on a survivor can be detected and migrated
  marker.on("click", () => {
    select(p.id, marker);
    openPlacePanel(marker.getLatLng(), p.id);
    // Clear the ring when the panel closes — tracked by id so a rebuild between select and close
    // (a pan under the open panel) can't leave it stuck on a detached marker.
    panelOnceClose(() => { if (selectedId === p.id) clearSelection(); });
  });
  if (p.id === selectedId) marker.setStyle({ color: accentHex(), weight: 3.5 }); // re-ring after a rebuild
  return marker;
}

// --- cluster bubbles (DOM divIcons) ------------------------------------------------------------
// Build (but do NOT add) a bubble marker. Diameter scales with log(count) (bubbleSize, capped below
// BIN_PX so neighbours never touch); the label is pre-formatted (formatCount). The caller batches the
// returned markers into clusterLayer in one pass.
function makeBubble(lat, lon, count, label, onClick, mobile) {
  const size = bubbleSize(count, mobile);
  const icon = L.divIcon({
    html: `<div class="odc-cluster" style="width:${size}px;height:${size}px">${label}</div>`,
    className: "",
    iconSize: [size, size],
  });
  const m = L.marker([lat, lon], { icon });
  m.on("click", onClick);
  return m;
}

// Paint clusters + individual pins for the current view straight from the Supercluster index. No
// fetch — the whole active set is client-side. Cluster bubbles are rebuilt each call (their ids are
// per-zoom); individual pins are diffed by id so a pan reuses survivors (the canvas isn't torn down).
export function renderView(bbox, zoom) {
  if (!index || !bbox) {
    // Nothing to show (pre-load, or a view flung into the noWrap void past ±180): clear everything.
    clusterLayer.clearLayers();
    pointLayer.clearLayers();
    idMap.clear();
    return;
  }
  const z = Math.min(Math.floor(zoom), SC_MAX_ZOOM); // Supercluster wants an int zoom; map uses 0.25 steps
  const cells = index.getClusters(expandBbox(bbox, RENDER_MARGIN), z);
  const mob = isMobile(); // smaller bubbles on a narrow phone

  // Cluster bubbles: full rebuild (cheap — a few hundred DOM divIcons at most).
  clusterLayer.clearLayers();
  const points = [];
  const bubbles = [];
  for (const c of cells) {
    if (c.properties.cluster) {
      const [lon, lat] = c.geometry.coordinates;
      const count = c.properties.point_count;
      const cid = c.properties.cluster_id;
      bubbles.push(makeBubble(lat, lon, count, formatCount(count), () => {
        // Zoom to exactly where this cluster breaks apart (Supercluster knows), capped at maxZoom;
        // never zoom OUT (a dense cluster's expansion zoom can equal the current zoom).
        const ez = Math.min(index.getClusterExpansionZoom(cid), SC_MAX_ZOOM);
        map.flyTo([lat, lon], Math.max(ez, map.getZoom() + 0.5), { animate: !prefersReducedMotion() });
      }, mob));
    } else {
      points.push(c); // an unclustered leaf: getClusters returns the ORIGINAL point feature
    }
  }
  bubbles.forEach((b) => clusterLayer.addLayer(b));

  // Individual pins: diff by id, reuse survivors, build/remove only the delta — and migrate any pin
  // whose has_pending flipped between the canvas and svg renderers (computeDiff keys on id alone and
  // would otherwise treat that as an untouched survivor).
  const { gone, fresh } = computeDiff(idMap, points);
  const goneNow = gone.slice();
  const freshNow = fresh.slice();
  points.forEach((f) => {
    const m = idMap.get(f.properties.id);
    if (m && m.__odPending !== !!f.properties.has_pending) { goneNow.push(m); freshNow.push(f); }
  });
  if (goneNow.length) {
    goneNow.forEach((m) => pointLayer.removeLayer(m));
    goneNow.forEach((m) => idMap.delete(m.__odId));
  }
  if (freshNow.length) {
    const made = freshNow.map(makePointMarker);
    made.forEach((m) => pointLayer.addLayer(m));
    made.forEach((m) => idMap.set(m.__odId, m));
  }
  const sel = idMap.get(selectedId);
  if (sel) sel.setStyle({ color: accentHex(), weight: 3.5 }); // keep the open pin ringed after a diff
}
