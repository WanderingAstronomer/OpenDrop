// Unit tests for js/viewport.js — the pure bbox/cluster-view helpers that keep every /api/locations
// request valid (Leaflet getBounds() legally exceeds ±180 at low zooms; unsized containers yield
// point bounds) and keep wide views legible (region collapse, pixel binning).
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  sanitizeBbox, expandBbox, bboxContains, bboxIntersects, filterFeaturesToBbox, inUSCoverage,
  isNationalView, partitionRegions, regionOf, binPoints, formatCount, REGIONS, US_DATA_ENVELOPE,
  prefersReducedMotion, cacheHit, computeDiff,
} from "../js/viewport.js";

// --- sanitizeBbox ---------------------------------------------------------------------------

test("sanitizeBbox passes a normal in-range viewport through unchanged", () => {
  assert.deepEqual(
    sanitizeBbox({ west: -83.25, south: 39.8, east: -82.75, north: 40.18 }),
    [-83.25, 39.8, -82.75, 40.18],
  );
});

test("sanitizeBbox fixes the default-view-on-a-wide-monitor case (west < -180)", () => {
  // zoom 4 on a ~2000px screen centered on the US: west ≈ -188 — the exact bbox that 400'd on
  // load. The crossing view keeps its data-bearing western-hemisphere side.
  assert.deepEqual(sanitizeBbox({ west: -188, south: 10, east: -8, north: 65 }),
    [-180, 10, -8, 65]);
});

test("sanitizeBbox returns the full world when the view spans >= 360 degrees", () => {
  assert.deepEqual(sanitizeBbox({ west: -257, south: -60, east: 161, north: 75 }),
    [-180, -60, 180, 75]);
});

test("sanitizeBbox keeps Hawaii fetchable on the real westward route (west < -180 clamps)", () => {
  // Panning west toward Hawaii, the view's west edge crosses -180 into the noWrap void; the void
  // side clamps off and the visible [-180, -150] survives (Hawaii at -157 stays fetchable).
  assert.deepEqual(sanitizeBbox({ west: -190, south: 20, east: -150, north: 60 }),
    [-180, 20, -150, 60]);
});

test("sanitizeBbox drops the void past +180 instead of wrapping it into Alaska/Hawaii", () => {
  // World panning is unlocked and tiles are noWrap: a view panned east past +180 shows VOID, not a
  // wrapped world copy. The old wrap turned east=210 into -150 and fetched Hawaii for a screen
  // showing Kamchatka — clamp keeps only what is physically visible.
  assert.deepEqual(sanitizeBbox({ west: 150, south: 20, east: 210, north: 60 }),
    [150, 20, 180, 60]);
  // Entirely beyond +180 = nothing but void on screen -> no fetch at all.
  assert.equal(sanitizeBbox({ west: 200, south: 20, east: 250, north: 60 }), null);
});

test("sanitizeBbox clamps latitudes to ±90", () => {
  assert.deepEqual(sanitizeBbox({ west: -100, south: -95, east: -60, north: 95 }),
    [-100, -90, -60, 90]);
});

test("sanitizeBbox rejects the degenerate point bbox from an unsized container", () => {
  assert.equal(sanitizeBbox({ west: -97.99, south: 39.5, east: -97.99, north: 39.5 }), null);
});

test("sanitizeBbox rejects non-finite input", () => {
  assert.equal(sanitizeBbox({ west: NaN, south: 0, east: 1, north: 1 }), null);
  assert.equal(sanitizeBbox({ west: -1, south: 0, east: Infinity, north: 1 }), null);
});

test("sanitizeBbox keeps east=180 as 180 (does not wrap it to -180)", () => {
  assert.deepEqual(sanitizeBbox({ west: 100, south: 0, east: 180, north: 50 }),
    [100, 0, 180, 50]);
});

test("sanitizeBbox keeps visible US data when a very wide view runs past +180", () => {
  // west=-100 east=210: a >300° view. Everything visible lives in [-100, 180] (beyond +180 is
  // noWrap void) — the clamp keeps the whole visible range, Columbus included, and no longer
  // fabricates an off-screen [-180, -150] segment the way the old wrap did.
  const out = sanitizeBbox({ west: -100, south: 20, east: 210, north: 50 });
  assert.deepEqual(out, [-100, 20, 180, 50]);
  assert.ok(out[0] <= -83 && -83 <= out[2], "Columbus (-83) must stay fetchable");
});

// --- bboxIntersects (the "is any US territory on screen?" gate) --------------------------------

test("bboxIntersects: overlapping, contained, and disjoint boxes", () => {
  assert.equal(bboxIntersects([-100, 30, -80, 45], US_DATA_ENVELOPE), true);   // inside the US
  assert.equal(bboxIntersects([-190, 10, -170, 20], US_DATA_ENVELOPE), true);  // clips the corner
  assert.equal(bboxIntersects([-56, 35, 60, 65], US_DATA_ENVELOPE), false);    // Europe at min zoom
  assert.equal(bboxIntersects([100, 20, 180, 60], US_DATA_ENVELOPE), false);   // east Asia/void side
});

test("bboxIntersects: edge-touching does not count as overlap", () => {
  // The envelope's east edge is -60; a view starting exactly there shows no US data.
  assert.equal(bboxIntersects([-60, 20, -10, 50], US_DATA_ENVELOPE), false);
  assert.equal(bboxIntersects([-61, 20, -10, 50], US_DATA_ENVELOPE), true);
});

test("inUSCoverage: real data territory counts, the envelope's empty ocean padding does not", () => {
  assert.equal(inUSCoverage([-100, 30, -80, 45]), true);      // CONUS
  assert.equal(inUSCoverage([-155, 55, -140, 65]), true);     // Alaska
  assert.equal(inUSCoverage([-160, 18, -154, 23]), true);     // Hawaii
  assert.equal(inUSCoverage([-67, 17.5, -65, 19]), true);     // Puerto Rico
  // A min-zoom view just east of Maine clips ONLY the envelope's dataless Atlantic padding
  // ([-66,-60]) — that view shows blank ocean and must get the coverage banner, not silence.
  assert.equal(inUSCoverage([-63, 30, 57, 55]), false);
  assert.equal(inUSCoverage([-56, 35, 60, 65]), false);       // Europe at min zoom
  assert.equal(inUSCoverage(null), false);
});

// --- isNationalView (region-collapse gate) ----------------------------------------------------

test("isNationalView: wide views collapse to region bubbles", () => {
  assert.equal(isNationalView([-180, 10.8, -8.2, 64.9]), true);   // wide-monitor default view
  assert.equal(isNationalView([-143, 15, -53, 65]), true);        // laptop min-zoom view
});

test("isNationalView is pan-invariant — a wide view collapses wherever it is panned", () => {
  // Regression for the owner's report: the old width+containment gate flipped bubbles<->cells the
  // instant a corner (e.g. Maine) left the screen. Collapse is now width-only, so the same-width
  // view collapses whether centered OR pinned west toward Alaska (67.5° wide — previously `false`).
  assert.equal(isNationalView([-143, 15, -53, 65]), true);        // centered, 90° wide
  assert.equal(isNationalView([-180, 32, -112.5, 57]), true);     // pinned toward Alaska, 67.5° wide
});

test("isNationalView flips at the width threshold, independent of pan position", () => {
  // just under 65° -> cells; just over -> bubbles; same widths panned to the far east decide the same
  assert.equal(isNationalView([-100, 30, -35.1, 45]), false);     // 64.9° wide
  assert.equal(isNationalView([-100, 30, -34.9, 45]), true);      // 65.1° wide
  assert.equal(isNationalView([-135.1, 30, -70, 45]), true);      // 65.1° wide, panned east
  assert.equal(isNationalView([-134.9, 30, -70, 45]), false);     // 64.9° wide, panned east
});

test("isNationalView: narrow views and null never collapse", () => {
  assert.equal(isNationalView([-85, 38, -80, 42]), false);
  assert.equal(isNationalView(null), false);
});

// --- expandBbox / bboxContains / filterFeaturesToBbox ----------------------------------------

test("expandBbox grows around the center and stays inside the world", () => {
  assert.deepEqual(expandBbox([-10, -10, 10, 10], 1.5), [-15, -15, 15, 15]);
  const clamped = expandBbox([-179, 80, 179, 89], 2);
  assert.ok(clamped[0] >= -180 && clamped[2] <= 180 && clamped[3] <= 90);
});

test("bboxContains: expanded fetch area contains any smaller pan inside it", () => {
  const fetched = expandBbox([-84, 39, -82, 41], 1.4);
  assert.ok(bboxContains(fetched, [-84, 39, -82, 41]));       // original viewport
  assert.ok(bboxContains(fetched, [-84.3, 38.9, -82.3, 40.9])); // small pan
  assert.ok(!bboxContains(fetched, [-90, 39, -82, 41]));        // big jump escapes
});

test("filterFeaturesToBbox keeps the list panel's 'in view' honest after over-fetch", () => {
  const feats = [
    { geometry: { coordinates: [-83.0, 40.0] } },  // in view
    { geometry: { coordinates: [-85.0, 40.0] } },  // fetched margin, out of view
  ];
  assert.equal(filterFeaturesToBbox(feats, [-84, 39, -82, 41]).length, 1);
});

// --- region collapse ---------------------------------------------------------------------------

test("regionOf classifies Anchorage, Honolulu, Columbus, and San Juan", () => {
  assert.equal(regionOf(61.2, -149.9), "ak");
  assert.equal(regionOf(21.3, -157.8), "hi");
  assert.equal(regionOf(39.96, -82.99), "conus");
  assert.equal(regionOf(18.4, -66.1), "conus"); // Puerto Rico rides with the main map
});

test("partitionRegions sums cluster cells into fixed-anchor region bubbles, dropping empties", () => {
  const cells = [
    { lat: 61.0, lon: -150.0, count: 40 },
    { lat: 64.8, lon: -147.7, count: 10 },
    { lat: 40.0, lon: -83.0, count: 900 },
  ];
  const out = partitionRegions(cells);
  assert.deepEqual(
    out.map(({ key, count, lat, lon }) => ({ key, count, lat, lon })).sort((a, b) => a.key.localeCompare(b.key)),
    [
      { key: "ak", count: 50, lat: REGIONS.ak.lat, lon: REGIONS.ak.lon },
      { key: "conus", count: 900, lat: REGIONS.conus.lat, lon: REGIONS.conus.lon },
    ],
  );
});

// --- pixel binning ------------------------------------------------------------------------------

test("binPoints merges cells that share a screen-grid cell at the weighted centroid", () => {
  const out = binPoints([
    { x: 10, y: 10, lat: 40.0, lon: -83.0, count: 30 },
    { x: 60, y: 40, lat: 41.0, lon: -84.0, count: 10 },   // same 72px cell as above
    { x: 500, y: 500, lat: 35.0, lon: -100.0, count: 5 }, // far away, own cell
  ], 72);
  assert.equal(out.length, 2);
  const merged = out.find((b) => b.count === 40);
  assert.ok(Math.abs(merged.lat - 40.25) < 1e-9);  // (40*30 + 41*10)/40
  assert.ok(Math.abs(merged.lon - (-83.25)) < 1e-9);
});

test("binPoints leaves already-sparse cells untouched", () => {
  const out = binPoints([
    { x: 0, y: 0, lat: 40, lon: -83, count: 3 },
    { x: 300, y: 300, lat: 41, lon: -82, count: 4 },
  ], 72);
  assert.equal(out.length, 2);
});

// --- label formatting ---------------------------------------------------------------------------

test("formatCount compacts large counts for bubble labels", () => {
  assert.equal(formatCount(7), "7");
  assert.equal(formatCount(999), "999");
  assert.equal(formatCount(1000), "1k");
  assert.equal(formatCount(1499), "1.5k");
  assert.equal(formatCount(9540), "9.5k");
  assert.equal(formatCount(16204), "16k");
});

// --- prefers-reduced-motion gate ----------------------------------------------------------------

test("prefersReducedMotion reflects matchMedia and defaults to false when unavailable", () => {
  const saved = global.window;
  try {
    delete global.window; // no window (old/embedded webview) → motion ON (false), never throws
    assert.equal(prefersReducedMotion(), false);
    global.window = { matchMedia: (q) => ({ matches: q === "(prefers-reduced-motion: reduce)" }) };
    assert.equal(prefersReducedMotion(), true);
    global.window = { matchMedia: () => ({ matches: false }) };
    assert.equal(prefersReducedMotion(), false);
  } finally {
    if (saved === undefined) delete global.window; else global.window = saved;
  }
});

// --- cacheHit (over-fetch cache predicate) ------------------------------------------------------
// Asserts the frozen truth table from the render-perf spec §1.3. cacheHit decides whether the live
// viewport can be served from the last fetched `data` with NO refetch. Fixtures: P() builds a POINTS
// cache, C() a CLUSTERS cache, V() a live view. BIG⊇SMALL (SMALL sits inside BIG); GROWN is NOT
// inside BIG.
const P = (bbox, zoom, types = null, national = false) =>
  ({ bbox, zoom, types, national, data: { mode: "points" } });
const C = (bbox, zoom, types = null, national = false) =>
  ({ bbox, zoom, types, national, data: { mode: "clusters" } });
const V = (bbox, zoom, types = null, national = false) => ({ bbox, zoom, types, national });
const BIG = [-100, 30, -80, 45], SMALL = [-95, 33, -85, 42], GROWN = [-110, 20, -70, 55];

test("cacheHit: null cache -> false", () => {
  assert.equal(cacheHit(null, V(SMALL, 12)), false);
});

test("cacheHit: types mismatch -> false", () => {
  assert.equal(cacheHit(P(BIG, 14, "drop_bin"), V(SMALL, 14, null)), false);
});

test("cacheHit: national mismatch -> false (AK/HI safety)", () => {
  assert.equal(cacheHit(P(BIG, 14, null, false), V(SMALL, 14, null, true)), false);
});

test("cacheHit: points regional, contained, DIFFERENT zoom -> true (A2 zoom-tolerant)", () => {
  assert.equal(cacheHit(P(BIG, 14), V(SMALL, 16)), true);
});

test("cacheHit: points regional, grown out of cache bbox, same zoom -> false", () => {
  assert.equal(cacheHit(P(BIG, 14), V(GROWN, 14)), false);
});

test("cacheHit: clusters, contained, different zoom -> false (binning is zoom-dependent)", () => {
  assert.equal(cacheHit(C(BIG, 10), V(SMALL, 11)), false);
});

test("cacheHit: clusters, contained, same zoom -> true (strict hit)", () => {
  assert.equal(cacheHit(C(BIG, 10), V(SMALL, 10)), true);
});

test("cacheHit: national both, same zoom -> true", () => {
  assert.equal(cacheHit(C(BIG, 5, null, true), V(SMALL, 5, null, true)), true);
});

test("cacheHit: national both, different zoom -> false", () => {
  assert.equal(cacheHit(C(BIG, 5, null, true), V(SMALL, 6, null, true)), false);
});

test("cacheHit: points cache but view is national -> false (!view.national guard)", () => {
  assert.equal(cacheHit(P(BIG, 14, null, false), V(SMALL, 14, null, true)), false);
});

test("cacheHit: undefined data falls back to strict rule", () => {
  const c = { bbox: BIG, zoom: 14, types: null, national: false, data: undefined };
  assert.equal(cacheHit(c, V(SMALL, 14)), true);   // strict: same zoom + contained
  assert.equal(cacheHit(c, V(SMALL, 16)), false);  // strict: zoom differs
});

// --- computeDiff (survivor/gone/fresh partition) ------------------------------------------------
// Spec §1.2: partition an id-keyed marker map against an incoming feature list into markers to REMOVE
// (gone) and features to ADD (fresh), so render() reuses survivors instead of clearing + rebuilding.
// Pure: plain Map + feature-like objects, no Leaflet/DOM.
const feat = (id) => ({ properties: { id }, geometry: { coordinates: [0, 0] } });
const mk = (id) => ({ __odId: id });

test("computeDiff: gone + fresh partition", () => {
  const idMap = new Map([[1, mk(1)], [2, mk(2)], [3, mk(3)]]);
  const { gone, fresh } = computeDiff(idMap, [feat(1), feat(2), feat(4)]);
  assert.deepEqual(gone.map((m) => m.__odId), [3]);
  assert.deepEqual(fresh.map((f) => f.properties.id), [4]);
});

test("computeDiff: all survive -> empty gone/fresh", () => {
  const idMap = new Map([[1, mk(1)], [2, mk(2)]]);
  const { gone, fresh } = computeDiff(idMap, [feat(1), feat(2)]);
  assert.equal(gone.length, 0);
  assert.equal(fresh.length, 0);
});

test("computeDiff: empty features -> all gone, none fresh", () => {
  const idMap = new Map([[1, mk(1)], [2, mk(2)]]);
  const { gone, fresh } = computeDiff(idMap, []);
  assert.deepEqual(gone.map((m) => m.__odId).sort((a, b) => a - b), [1, 2]);
  assert.equal(fresh.length, 0);
});

test("computeDiff: duplicate id in payload dedupes fresh", () => {
  const { fresh } = computeDiff(new Map(), [feat(5), feat(5)]);
  assert.deepEqual(fresh.map((f) => f.properties.id), [5]);
});
