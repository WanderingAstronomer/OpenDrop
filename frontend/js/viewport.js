// Pure viewport/bbox helpers — no Leaflet, no DOM, fully unit-testable (test/viewport.test.js).
//
// Why this module exists: Leaflet's map.getBounds() is allowed to return longitudes beyond ±180
// (a world-wide view on a wide monitor yields west < -180; the default zoom-4 view on a 2000px
// screen yields west ≈ -188), and a not-yet-laid-out container yields a zero-area "point" bounds.
// Our API validates bbox strictly (west<east, south<north, |lon|<=180, |lat|<=90) and 400s
// otherwise — which the UI used to misreport as "couldn't reach the server". Every request path
// therefore sanitizes through here first.

// The tile layers run noWrap, so the world renders exactly ONCE: longitudes past ±180 are grey
// void on screen, never a wrapped copy. Out-of-range bounds are therefore CLAMPED (the void side
// dropped) — the old wrap-around normalization fetched real data for territory that wasn't
// visible (e.g. an east pan past +180 "wrapping" into Alaska).
const WORLD = [-180, -90, 180, 90];

// Everywhere US data can live (CONUS + AK + HI + PR, padded).
export const US_DATA_ENVELOPE = [-180, 14, -60, 72];
// The contiguous-US core: a view that contains this rect is genuinely "the whole US on screen".
export const CONUS_CORE = [-125, 24, -66, 50];
// Where data ACTUALLY lives, tighter than the padded envelope (whose ~6° of empty Atlantic east
// of -66 would otherwise count as "coverage" and suppress the guidance banner over blank ocean).
export const US_DATA_BOXES = [
  CONUS_CORE,                 // contiguous US
  [-180, 50, -125, 72],       // Alaska (incl. the Aleutians to -180)
  [-165, 15, -150, 25],       // Hawaii
  [-68, 17, -64, 19.5],       // Puerto Rico / USVI
];

// Raw Leaflet bounds -> a valid [west, south, east, north] bbox for the API, or null when the
// viewport is degenerate (container not sized yet) OR lies entirely in the noWrap void past ±180
// — either way the fetch should be silently skipped.
export function sanitizeBbox({ west, south, east, north }) {
  if (![west, south, east, north].every((v) => typeof v === "number" && isFinite(v))) return null;

  const s = Math.max(south, -90);
  const n = Math.min(north, 90);
  if (!(s < n)) return null; // degenerate or inverted latitudes

  if (west === east) return null; // point bbox from an unsized container
  if (east - west >= 360) return [-180, s, 180, n]; // a full wrap or more: the whole world is on screen

  const w = Math.max(west, -180);
  const e = Math.min(east, 180);
  if (!(w < e)) return null; // the view sits entirely in the void beyond ±180
  return [w, s, e, n];
}

// True when two [w,s,e,n] boxes overlap at all (strict — edge-touching does not count).
export function bboxIntersects(a, b) {
  return a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3];
}

// The "is any US data territory on screen?" gate now that the world is freely pannable — checked
// against the real data boxes, not the padded envelope, so empty ocean padding never counts.
export function inUSCoverage(bbox) {
  return !!bbox && US_DATA_BOXES.some((b) => bboxIntersects(bbox, b));
}

// Grow a bbox around its center by `factor` (e.g. 1.4 = ~40% margin), clamped to the world. Used to
// cluster/paint a little beyond the viewport so pins just off the edge exist for short pans.
export function expandBbox([w, s, e, n], factor) {
  const cx = (w + e) / 2;
  const cy = (s + n) / 2;
  const hw = ((e - w) / 2) * factor;
  const hh = ((n - s) / 2) * factor;
  return [
    Math.max(WORLD[0], cx - hw),
    Math.max(WORLD[1], cy - hh),
    Math.min(WORLD[2], cx + hw),
    Math.min(WORLD[3], cy + hh),
  ];
}

// Partition an id-keyed marker map against an incoming feature list into the markers to REMOVE and
// the features to ADD — the diff that lets renderView reuse survivors instead of clearLayers()+rebuild
// on every pan (the render margin means consecutive views overlap heavily, so most markers survive).
// Pure: reads only `.__odId` on markers and `.properties.id` on features, treats markers as opaque
// handles, touches no Leaflet/DOM — which also keeps it importable in the headless suite.
//   idMap: Map<id, marker>   features: [{ properties:{ id } }]
//   -> { gone: marker[], fresh: feature[] }
export function computeDiff(idMap, features) {
  const incoming = new Set((features || []).map((f) => f.properties.id));
  const gone = [];
  idMap.forEach((m, id) => { if (!incoming.has(id)) gone.push(m); });
  const seen = new Set();
  const fresh = (features || []).filter((f) => {
    const id = f.properties.id;
    if (idMap.has(id) || seen.has(id)) return false; // already shown, or a dup within this payload
    seen.add(id);
    return true;
  });
  return { gone, fresh };
}

// Keep the list panel's "N in view" honest when the render margin over-covered the viewport.
export function filterFeaturesToBbox(features, [w, s, e, n]) {
  return (features || []).filter((f) => {
    const [lon, lat] = f.geometry.coordinates;
    return lon >= w && lon <= e && lat >= s && lat <= n;
  });
}

// Cluster-bubble diameter (px) from its count. Grows with log(count) and is CAPPED at BUBBLE_MAX_PX,
// kept at/below Supercluster's cluster radius (markers.js SC_OPTS) so adjacent cluster bubbles stay
// clear of one another. Trimmed ~12% from the earlier cap-64 that touched its neighbours.
export const BUBBLE_MAX_PX = 56;
export function bubbleSize(count) {
  return Math.round(Math.min(BUBBLE_MAX_PX, 30 + Math.log2((count || 0) + 1) * 3.6));
}

// Compact bubble labels: 999 -> "999", 1500 -> "1.5k", 16204 -> "16k".
export function formatCount(n) {
  if (n < 1000) return String(n);
  if (n < 10000) {
    const k = Math.round(n / 100) / 10;
    return `${k % 1 === 0 ? Math.round(k) : k}k`;
  }
  return `${Math.round(n / 1000)}k`;
}

// True when the OS/browser requests reduced motion. Read LIVE (never cached) so a mid-session OS
// toggle is honored: Leaflet's flyTo/panBy/fitBounds run their own JS animation and ignore the CSS
// prefers-reduced-motion block, so every camera call branches on this and passes {animate:!reduced}.
// matchMedia may be absent in old/embedded webviews → default to motion ON (false).
export function prefersReducedMotion() {
  try { return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches); }
  catch (e) { return false; }
}
