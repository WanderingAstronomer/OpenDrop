// Unit tests for js/viewport.js — the pure bbox/marker helpers that keep every /api/locations
// request valid (Leaflet getBounds() legally exceeds ±180 at low zooms; unsized containers yield
// point bounds) and keep the client render honest (coverage gate, bbox filtering, marker diffing).
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  sanitizeBbox, expandBbox, bboxIntersects, filterFeaturesToBbox, inUSCoverage,
  bubbleSize, BUBBLE_MAX_PX, formatCount, US_DATA_ENVELOPE, prefersReducedMotion, computeDiff,
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

// --- expandBbox / filterFeaturesToBbox --------------------------------------------------------

test("expandBbox grows around the center and stays inside the world", () => {
  assert.deepEqual(expandBbox([-10, -10, 10, 10], 1.5), [-15, -15, 15, 15]);
  const clamped = expandBbox([-179, 80, 179, 89], 2);
  assert.ok(clamped[0] >= -180 && clamped[2] <= 180 && clamped[3] <= 90);
});

test("filterFeaturesToBbox keeps the list panel's 'in view' honest after the render margin", () => {
  const feats = [
    { geometry: { coordinates: [-83.0, 40.0] } },  // in view
    { geometry: { coordinates: [-85.0, 40.0] } },  // render margin, out of view
  ];
  assert.equal(filterFeaturesToBbox(feats, [-84, 39, -82, 41]).length, 1);
});

// --- cluster bubble sizing (de-overlap invariant) -----------------------------------------------

test("bubbleSize grows with count but never exceeds the de-overlap cap", () => {
  // The cap is the load-bearing de-overlap guarantee: a bubble stays at/below Supercluster's cluster
  // radius so two adjacent cluster bubbles can't touch.
  for (const n of [0, 1, 5, 50, 500, 5000, 500000]) {
    const s = bubbleSize(n);
    assert.ok(s <= BUBBLE_MAX_PX, `size ${s} for count ${n} exceeds the cap`);
    assert.ok(s >= 30, `size ${s} for count ${n} is implausibly small`);
  }
});

test("bubbleSize is monotonic non-decreasing in count", () => {
  let prev = 0;
  for (const n of [1, 2, 10, 100, 1000, 10000, 100000]) {
    const s = bubbleSize(n);
    assert.ok(s >= prev, `size dropped at count ${n}`);
    prev = s;
  }
});

test("bubbleSize hits the cap by the thousands and stays there", () => {
  assert.equal(bubbleSize(2000), BUBBLE_MAX_PX);
  assert.equal(bubbleSize(50000), BUBBLE_MAX_PX);
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

// --- computeDiff (survivor/gone/fresh partition) ------------------------------------------------
// Partition an id-keyed marker map against an incoming feature list into markers to REMOVE (gone) and
// features to ADD (fresh), so renderView reuses survivors instead of clearing + rebuilding. Pure:
// plain Map + feature-like objects, no Leaflet/DOM.
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
