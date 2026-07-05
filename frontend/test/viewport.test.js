// Unit tests for js/viewport.js — the pure bbox/cluster-view helpers that keep every /api/locations
// request valid (Leaflet getBounds() legally exceeds ±180 at low zooms; unsized containers yield
// point bounds) and keep wide views legible (region collapse, pixel binning).
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  sanitizeBbox, expandBbox, bboxContains, filterFeaturesToBbox, isNationalView,
  partitionRegions, regionOf, binPoints, formatCount, REGIONS, prefersReducedMotion,
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

test("sanitizeBbox keeps the western side of an antimeridian-crossing view (Hawaii reachable)", () => {
  // Panning west past the antimeridian toward Hawaii: east=210 wraps to -150; the data-bearing
  // side [-180, -150] survives (Hawaii at -157 stays fetchable), the no-data positive side drops.
  assert.deepEqual(sanitizeBbox({ west: 150, south: 20, east: 210, north: 60 }),
    [-180, 20, -150, 60]);
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

test("sanitizeBbox straddle keeps data visible on BOTH sides of the wrap (review fix #11)", () => {
  // west=-100 east=210: a >300° view showing [-100..180]∪[-180..-150]. US data at lon -100..-64 is
  // on screen via the western segment — the straddle shortcut must extend east to keep it.
  const out = sanitizeBbox({ west: -100, south: 20, east: 210, north: 50 });
  assert.deepEqual(out, [-180, 20, -64, 50]);
  assert.ok(out[0] <= -83 && -83 <= out[2], "Columbus (-83) must stay fetchable");
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
