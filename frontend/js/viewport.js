// Pure viewport/bbox helpers — no Leaflet, no DOM, fully unit-testable (test/viewport.test.js).
//
// Why this module exists: Leaflet's map.getBounds() is allowed to return longitudes beyond ±180
// (a world-wide view on a wide monitor yields west < -180; the default zoom-4 view on a 2000px
// screen yields west ≈ -188), and a not-yet-laid-out container yields a zero-area "point" bounds.
// Our API validates bbox strictly (west<east, south<north, |lon|<=180, |lat|<=90) and 400s
// otherwise — which the UI used to misreport as "couldn't reach the server". Every request path
// therefore sanitizes through here first.

// The API stores US data only (the pipeline's ingest gate caps longitude at [-180, -64] — even
// Alaska's antimeridian-crossing Aleutian islands are rejected at ingest), so the positive-lon
// side of an antimeridian-crossing view can never contain data and is safe to drop.
const WORLD = [-180, -90, 180, 90];

// Everywhere US data can live (CONUS + AK + HI + PR, padded) — also the map's pan limit rect.
export const US_DATA_ENVELOPE = [-180, 14, -60, 72];
// The contiguous-US core: a view that contains this rect is genuinely "the whole US on screen".
export const CONUS_CORE = [-125, 24, -66, 50];

function wrapLon(x) {
  // Normalize any longitude into [-180, 180). 180 maps to -180; callers preserve east=180 manually.
  return ((((x + 180) % 360) + 360) % 360) - 180;
}

// Raw Leaflet bounds -> a valid [west, south, east, north] bbox for the API, or null when the
// viewport is degenerate (container not sized yet) and the fetch should be silently skipped.
export function sanitizeBbox({ west, south, east, north }) {
  if (![west, south, east, north].every((v) => typeof v === "number" && isFinite(v))) return null;

  const s = Math.max(south, -90);
  const n = Math.min(north, 90);
  if (!(s < n)) return null; // degenerate or inverted latitudes

  if (west === east) return null; // point bbox from an unsized container
  let w, e;
  if (east - west >= 360) {
    [w, e] = [-180, 180]; // whole world (or more) in view
  } else {
    w = wrapLon(west);
    e = east === 180 ? 180 : wrapLon(east);
    if (!(w < e)) {
      // The view straddles ±180 (e.g. the default zoom-4 view on a wide monitor: west=-188 wraps
      // to +172). The visible longitudes are [w,180] ∪ [-180,e]; only [-180,-64] can hold data.
      // Keep [-180,e], and when the [w,180] side ALSO dips into data territory (wrapped west
      // below -64, i.e. a >300° view wrapping past +180), extend e so that side's data survives.
      if (w < -64) e = Math.max(e, -64);
      if (!(e > -180)) return null;
      w = -180;
    }
  }
  return [w, s, e, n];
}

// Grow a bbox around its center by `factor` (e.g. 1.4 over-fetches ~40% margin so small pans stay
// inside the fetched area and need no new request), clamped to the world.
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

export function bboxContains(outer, inner) {
  return outer[0] <= inner[0] && outer[1] <= inner[1] && outer[2] >= inner[2] && outer[3] >= inner[3];
}

// Decide whether the current view can be served from the last fetched `data` with NO refetch — the
// over-fetch cache predicate, lifted out of main.js so it is pure and unit-testable.
//   cache: { bbox:[w,s,e,n], zoom, types, national, data:{mode?} } | null   (the last fetch)
//   view:  { bbox:[w,s,e,n], zoom, types, national }                        (the live viewport)
// Rules, in order:
//  1. national and regional caches NEVER cross-serve — a national render totals region bubbles from
//     the WHOLE data envelope; a regional cache covers only its padded viewport, so serving one from
//     the other would silently drop Alaska/Hawaii or report partial totals.
//  2. POINTS-tolerant (A2): a points response is the <=point_cap nearest points for cache.bbox; any
//     CONTAINED viewport is a subset of those points, and the server only flips points->clusters when
//     the bbox GROWS past the density cap. So a contained points view is valid at ANY zoom — killing
//     the fetch+rebuild that used to fire on every street-level zoom step.
//  3. CLUSTERS/national fall back to strict zoom equality — cluster cells are pixel/grid-binned per
//     zoom, so a different zoom needs a fresh aggregation.
export function cacheHit(cache, view) {
  if (!cache) return false;
  if (cache.types !== view.types) return false;
  if (cache.national !== view.national) return false;
  const contained = bboxContains(cache.bbox, view.bbox);
  const cachedIsPoints = !!(cache.data && cache.data.mode !== "clusters");
  if (cachedIsPoints && !view.national && contained) return true;
  return cache.zoom === view.zoom && (view.national || contained);
}

// Partition an id-keyed marker map against an incoming feature list into the markers to REMOVE and
// the features to ADD — the diff that lets render() reuse survivors instead of clearLayers()+rebuild
// on every pan (the 1.5x over-fetch means consecutive fetches overlap heavily, so most markers
// survive). Pure: reads only `.__odId` on markers and `.properties.id` on features, treats markers
// as opaque handles, touches no Leaflet/DOM — which also keeps it importable in the headless suite.
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

// Keep the list panel's "N in view" honest when the fetch over-covered the viewport.
export function filterFeaturesToBbox(features, [w, s, e, n]) {
  return (features || []).filter((f) => {
    const [lon, lat] = f.geometry.coordinates;
    return lon >= w && lon <= e && lat >= s && lat <= n;
  });
}

// --- national view: collapse server cluster cells into 3 region bubbles ------------------------
// US dashboards conventionally special-case Alaska and Hawaii (the d3.geoAlbersUsa inset-map
// convention); at national spans hundreds of grid cells collapse into AK / HI / contiguous-US
// totals anchored at fixed, stable points so the bubbles don't wander as the user pans. The CONUS
// anchor is the surveyed geographic center of the contiguous US (Lebanon, Kansas, 39°50'N 98°35'W).
export const REGIONS = {
  ak: { lat: 64.2, lon: -152.5, zoom: 4, label: "Alaska" },
  hi: { lat: 20.7, lon: -157.0, zoom: 6, label: "Hawaii" },
  conus: { lat: 39.83, lon: -98.58, zoom: 5, label: "Contiguous US" },
};

// Collapse to region bubbles on a genuinely NATIONAL view, decided by WIDTH alone (≈ zoom) so the
// choice is PAN-INVARIANT. CONUS spans ~58°; 65° means "wider than the whole contiguous US". The
// earlier gate also required the view to *contain* the CONUS core, which made it pan-dependent:
// dragging one corner (e.g. Maine) off-screen flipped the whole map between 3 region bubbles and
// hundreds of raw cells — the jarring toggle the owner reported. The bubbles sit at fixed anchors
// and pan smoothly with the map, so a view panned toward Alaska just shows the AK bubble on-screen
// and the CONUS bubble drifting toward the edge. (At these widths the map's maxBounds keep the view
// inside [-180,-60], so no antimeridian clamping shrinks the span — width tracks zoom exactly. The
// one accepted edge: panning so far into the Pacific that an anchor leaves the screen briefly shows
// no bubble for that region; maxBounds makes this rare.)
export const REGION_COLLAPSE_SPAN_DEG = 65;

export function isNationalView(bbox) {
  return !!bbox && (bbox[2] - bbox[0]) >= REGION_COLLAPSE_SPAN_DEG;
}

export function regionOf(lat, lon) {
  if (lat >= 50 && lon <= -125) return "ak";
  if (lat >= 15 && lat <= 25 && lon >= -165 && lon <= -150) return "hi";
  return "conus"; // incl. PR/VI — they read as part of the main map at these zooms
}

export function partitionRegions(cells) {
  const sums = { ak: 0, hi: 0, conus: 0 };
  (cells || []).forEach((c) => { sums[regionOf(c.lat, c.lon)] += c.count || 0; });
  return Object.entries(sums)
    .filter(([, count]) => count > 0)
    .map(([key, count]) => ({ key, count, ...REGIONS[key] }));
}

// --- mid-zoom legibility: merge server cells on a screen-pixel grid ----------------------------
// The server grid-clusters in *degrees* (bbox-span/32), so on a busy national/state view it can
// return hundreds of cells. Binning the cells onto a screen grid caps visual density by PIXELS,
// which is what legibility actually depends on. Merged position is the count-weighted centroid.
// points: [{ x, y, lat, lon, count }] where x/y are container pixels.
export function binPoints(points, cellPx) {
  const bins = new Map();
  (points || []).forEach((p) => {
    const key = `${Math.floor(p.x / cellPx)}:${Math.floor(p.y / cellPx)}`;
    let b = bins.get(key);
    if (!b) bins.set(key, (b = { count: 0, latW: 0, lonW: 0 }));
    b.count += p.count;
    b.latW += p.lat * p.count;
    b.lonW += p.lon * p.count;
  });
  return [...bins.values()].map((b) => ({
    lat: b.latW / b.count,
    lon: b.lonW / b.count,
    count: b.count,
  }));
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
// toggle is honored. The one non-pure helper here, but it belongs with the camera math this module
// owns: Leaflet's flyTo/panBy/fitBounds run their own JS animation and ignore the CSS
// prefers-reduced-motion block, so every camera call must branch on this and pass {animate:!reduced}.
// matchMedia may be absent in old/embedded webviews → default to motion ON (false).
export function prefersReducedMotion() {
  try { return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches); }
  catch (e) { return false; }
}
