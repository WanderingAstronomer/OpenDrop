import { bucketColor } from "./confidence.js";
import { openPlacePanel, panelOnceClose } from "./panel.js";
import { onThemeChange } from "./theme.js";
import {
  REGION_COLLAPSE_SPAN_DEG, binPoints, computeDiff, formatCount, isNationalView, partitionRegions,
  prefersReducedMotion,
} from "./viewport.js";

let map = null;
let clusterGroup = null;
let serverLayer = null;

// Two SHARED renderers, created in initMarkers before any marker references them. The bulk of the
// point markers draw onto ONE canvas (one <canvas>, one paint pass, flat hit-testing) — the whole
// point of B-1. The few pins with an open community proposal draw onto a shared SVG renderer so
// their .odc-pending keyframe (which animates the SVG <path>'s filter) still works — canvas has no
// per-marker DOM node to animate. getRenderer() honors each marker's options.renderer first, so the
// two coexist inside the same markercluster group (verified against the vendored Leaflet 1.9.4).
let canvasRenderer = null;
let svgRenderer = null;

// Idempotency memo: render() early-returns when the same data object comes back (an over-fetch cache
// hit re-presenting) so a pan/zoom settle no longer tears down and rebuilds every marker. Points are
// zoom/size-independent, so points-mode returns on data identity alone; cluster BUBBLES are pixel-
// binned, so clusters-mode must also re-bin when zoom / container size / national-ness changes.
let lastData = null, lastMode = null, lastZoom = null, lastSizeKey = null, lastNational = null;

// id -> the live circleMarker currently in clusterGroup, so consecutive fetches diff instead of
// rebuild (survivors are reused untouched) and the selection ring can be re-found by id.
const idMap = new Map();
let selectedId = null; // id of the pin whose place panel is open — its ring is re-applied by id

// Bin server cluster cells onto this screen-pixel grid before drawing. The server clusters in
// DEGREES (bbox-span/32), so a busy national/state view can return hundreds of cells; legibility
// depends on PIXELS. 64px sits between Supercluster's 40px and Leaflet.markercluster's 80px
// defaults, and caps visual density by screen size instead of data density.
const BIN_PX = 64;

function clusterIcon(cluster) {
  const n = cluster.getChildCount();
  const size = n < 10 ? 34 : n < 50 ? 40 : 48;
  return L.divIcon({
    html: `<div class="odc-cluster" style="width:${size}px;height:${size}px">${n}</div>`,
    className: "",
    iconSize: [size, size],
  });
}

export function initMarkers(m) {
  map = m;
  // markercluster's split/merge slide animates every marker's position on each cluster change —
  // hundreds of individually compositing transitions, the worst of the zoom-frame cost on a phone.
  // Off on mobile (clusters snap to their new positions instead of sliding); kept on desktop.
  // Init-time only, paired with the map's markerZoomAnimation flag in map.js.
  const isMobile = !!(window.matchMedia && window.matchMedia("(max-width: 1023px)").matches);
  // padding 0.2 (renderer surface = viewport * 1.4/axis, ~2x area): a small margin so pins just off
  // the edge are pre-painted for short pans, without the 4x-area canvas that padding 0.5 balloons to
  // on large/retina screens. The 1.5x over-fetch DATA cache already covers longer pans.
  canvasRenderer = L.canvas({ tolerance: 6, padding: 0.2 }).addTo(map);
  svgRenderer = L.svg({ padding: 0.2 }).addTo(map);
  clusterGroup = L.markerClusterGroup({
    chunkedLoading: true,
    maxClusterRadius: 40,        // looser grouping than the default 80 -> neighborhoods separate sooner
    disableClusteringAtZoom: 16, // street level -> always individual pins (good for bins)
    showCoverageOnHover: false,  // drop the distracting blue coverage polygon
    spiderfyOnMaxZoom: true,
    iconCreateFunction: clusterIcon,
    animate: !isMobile,          // A4 — no per-marker cluster animation on mobile
  });
  map.addLayer(clusterGroup);
  serverLayer = L.layerGroup().addTo(map);
  lastData = null; lastMode = null; idMap.clear();
  // A theme swap changes --accent; the canvas won't repaint the selected ring on its own, so re-style
  // the live selection when the theme flips.
  onThemeChange(() => { const m2 = idMap.get(selectedId); if (m2) m2.setStyle({ color: accentHex(), weight: 3.5 }); });
}

// --- selection ring (canvas-safe) --------------------------------------------------------------
// A canvas circleMarker has no per-marker DOM <path>, so the old CSS .marker-selected class can't
// ride it. Instead setStyle the tracked marker to the accent ring, and restore its captured base
// style on deselect. Colors must be concrete (SVG presentation attrs / the canvas ctx can't resolve
// var()), so --accent is read from the computed root style at apply time (stays theme-aware).
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
  // Pending pins carry their violet stroke as their BASE style (the CSS .odc-pending stroke rule was
  // dropped so setStyle-selection can override it); the keyframe still animates their filter.
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

// --- server cluster bubbles (DOM divIcons) -----------------------------------------------------
// Build (but do NOT add) a bubble marker. Diameter scales with the log of the count; the label is
// pre-formatted. The caller batches the returned markers into serverLayer in one pass.
function makeBubble(lat, lon, count, label, onClick, extraClass = "") {
  const size = Math.round(Math.min(64, 34 + Math.log2(count + 1) * 4));
  const icon = L.divIcon({
    html: `<div class="odc-cluster ${extraClass}" style="width:${size}px;height:${size}px">${label}</div>`,
    className: "",
    iconSize: [size, size],
  });
  const m = L.marker([lat, lon], { icon });
  m.on("click", onClick);
  return m;
}

const sizeKey = () => { const s = map.getSize(); return `${s.x}x${s.y}`; };

// `bbox` is the sanitized [w,s,e,n] viewport — its longitude span decides the national collapse.
export function render(data, bbox) {
  if (!data) {
    clusterGroup.clearLayers();
    serverLayer.clearLayers();
    idMap.clear();
    lastData = null; lastMode = null;
    return;
  }

  const mode = data.mode;
  const national = mode === "clusters" ? isNationalView(bbox) : null;
  const z = map.getZoom();
  const sk = sizeKey();

  // Idempotent guard (A1): the same fetched object re-presenting (a cache-hit pan) rebuilds nothing.
  if (data === lastData && mode === lastMode) {
    if (mode !== "clusters") return; // points: positions are zoom/size-independent
    if (z === lastZoom && sk === lastSizeKey && national === lastNational) return; // clusters: re-bin only on change
  }
  // Mode flip -> the two structures hold the wrong kind of layer; hard-reset before building.
  if (mode !== lastMode) {
    clusterGroup.clearLayers();
    serverLayer.clearLayers();
    idMap.clear();
  }

  if (mode === "clusters") {
    serverLayer.clearLayers();
    const cells = data.clusters || [];
    const bubbles = [];

    if (national) {
      // National view, decided by width/zoom ALONE (pan-invariant — see viewport.isNationalView).
      // Collapse to at most three fixed-anchor region bubbles (AK / HI / CONUS), the albersUsa inset
      // convention; they pan with the map, so dragging the edge no longer toggles bubbles<->cells.
      // Click flies into the region at a zoom deep enough to EXIT the national view on any screen
      // width (a fixed target zoom dead-ends on wide monitors, where even zoom 5 still spans >= the
      // collapse threshold).
      partitionRegions(cells).forEach((r) => {
        bubbles.push(makeBubble(r.lat, r.lon, r.count, formatCount(r.count), () => {
          const px = map.getSize().x || 1024;
          const exitZoom = Math.max(r.zoom,
            Math.ceil(Math.log2((px * 360) / (256 * REGION_COLLAPSE_SPAN_DEG))));
          map.flyTo([r.lat, r.lon], exitZoom, { animate: !prefersReducedMotion() });
        }, "odc-region"));
      });
    } else {
      // State/metro views: merge server cells on a screen-pixel grid so bubble density is capped by
      // pixels, not by how many degree-cells the server happened to cut.
      const pts = cells.map((c) => {
        const p = map.latLngToContainerPoint([c.lat, c.lon]);
        return { x: p.x, y: p.y, lat: c.lat, lon: c.lon, count: c.count };
      });
      binPoints(pts, BIN_PX).forEach((c) => {
        bubbles.push(makeBubble(c.lat, c.lon, c.count, formatCount(c.count),
          () => map.flyTo([c.lat, c.lon], Math.min(map.getZoom() + 3, 16), { animate: !prefersReducedMotion() })));
      });
    }
    // serverLayer is a plain L.layerGroup (no bulk addLayers); a few hundred DOM bubbles add cheaply.
    bubbles.forEach((b) => serverLayer.addLayer(b));
  } else {
    // Points mode: diff by id and reuse survivors — only truly new pins are built, only truly gone
    // pins are removed. Bulk add/remove so markercluster's chunkedLoading spreads the work.
    const { gone, fresh } = computeDiff(idMap, data.features || []);
    // A has_pending flip on a SURVIVING pin (e.g. a move proposal opens/resolves via forceRefresh)
    // must migrate it between the svg (pending) and canvas (normal) renderers and swap its base
    // stroke + pulse. computeDiff keys on id alone and treats it as an untouched survivor, so detect
    // the flip here and route those pins through remove+re-add onto the correct renderer.
    const goneNow = gone.slice();
    const freshNow = fresh.slice();
    (data.features || []).forEach((f) => {
      const m = idMap.get(f.properties.id);
      if (m && m.__odPending !== !!f.properties.has_pending) { goneNow.push(m); freshNow.push(f); }
    });
    if (goneNow.length) {
      clusterGroup.removeLayers(goneNow);
      goneNow.forEach((m) => idMap.delete(m.__odId));
    }
    if (freshNow.length) {
      const made = freshNow.map(makePointMarker);
      clusterGroup.addLayers(made);
      made.forEach((m) => idMap.set(m.__odId, m));
    }
    const sel = idMap.get(selectedId);
    if (sel) sel.setStyle({ color: accentHex(), weight: 3.5 }); // keep the open pin ringed after a diff
  }

  lastData = data; lastMode = mode; lastZoom = z; lastSizeKey = sk; lastNational = national;
}
