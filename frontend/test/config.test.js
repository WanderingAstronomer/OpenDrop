// Unit tests for js/config.js — the client constants and the /meta-backed fallbacks that govern
// the correction flow. The API is the source of truth, but the UI must degrade to documented
// defaults when /meta is unreachable. Tests are ordered so the META-null fallback case runs BEFORE
// any loadMeta() call mutates the module-level META.
import { test } from "node:test";
import assert from "node:assert/strict";

import {
  API, ORG_TYPES, ORG_TYPE_LABELS, BUCKET_COLORS,
  gpsRadiusM, maxMoveM, loadMeta,
} from "../js/config.js";

// Override a getter-only Node global (fetch) safely, restoring it afterwards.
function withFetch(stub, fn) {
  const real = globalThis.fetch;
  Object.defineProperty(globalThis, "fetch", { value: stub, configurable: true, writable: true });
  return (async () => {
    try { return await fn(); }
    finally {
      Object.defineProperty(globalThis, "fetch", { value: real, configurable: true, writable: true });
    }
  })();
}

test("API base path is the relative /api prefix", () => {
  assert.equal(API, "/api");
});

test("ORG_TYPES is exactly the keys of ORG_TYPE_LABELS (no drift)", () => {
  assert.deepEqual(ORG_TYPES, Object.keys(ORG_TYPE_LABELS));
});

test("BUCKET_COLORS defines a hex colour for all three confidence buckets", () => {
  for (const b of ["high", "medium", "low"]) {
    assert.match(BUCKET_COLORS[b], /^#[0-9a-f]{6}$/i, `${b} bucket colour`);
  }
});

test("gpsRadiusM / maxMoveM fall back to documented defaults when META is unset", () => {
  // Runs before any loadMeta() below, so META === null here.
  assert.equal(gpsRadiusM(), 75);
  assert.equal(maxMoveM(), 2000);
});

test("loadMeta applies server-provided correction constants", async () => {
  await withFetch(
    async () => ({ json: async () => ({ gps_radius_m: 50, correction_max_move_m: 1500, sources: [] }) }),
    async () => {
      const meta = await loadMeta();
      assert.equal(meta.gps_radius_m, 50);
      assert.equal(gpsRadiusM(), 50);
      assert.equal(maxMoveM(), 1500);
    },
  );
});

test("gpsRadiusM / maxMoveM re-fall-back when META omits the keys", async () => {
  await withFetch(
    async () => ({ json: async () => ({ sources: [] }) }),  // no gps_radius_m / correction_max_move_m
    async () => {
      await loadMeta();
      assert.equal(gpsRadiusM(), 75);
      assert.equal(maxMoveM(), 2000);
    },
  );
});

test("loadMeta swallows a fetch failure and yields a safe empty-meta shape", async () => {
  await withFetch(
    async () => { throw new Error("network down"); },
    async () => {
      const meta = await loadMeta();
      assert.deepEqual(meta, { sources: [], turnstile_sitekey: null });
    },
  );
});
