// Unit tests for the pure / GPS-gating logic in js/corrections.js.
//
// The headline target is confirmGpsCorroborated — the privacy-critical gate behind the production
// bug (ranked #4) where a "Looks right" confirm tap could (in an earlier revision) send
// gps_corroborated. The contract this suite LOCKS:
//   • a confirm tap must NEVER trigger a geolocation permission prompt — corroboration only ever
//     happens when permission was ALREADY granted; otherwise it silently returns false;
//   • only the boolean result leaves these functions — the device computes distance locally.
import { test } from "node:test";
import assert from "node:assert/strict";

import { haversine, gpsWithin, tierBlurb, confirmGpsCorroborated } from "../js/corrections.js";

// Node exposes `navigator` as a getter-only global, so it must be overridden via defineProperty.
function setNavigator(nav) {
  Object.defineProperty(globalThis, "navigator", { value: nav, configurable: true, writable: true });
}
// A geolocation stub: pass coords to succeed, or null to fire the error callback (denied/timeout).
function geolocation(coords, onAsked) {
  return {
    getCurrentPosition(ok, err) {
      if (onAsked) onAsked();
      if (coords) ok({ coords });
      else err(new Error("denied"));
    },
  };
}

// --- haversine: great-circle distance in metres -------------------------------------------------
test("haversine is exactly zero for identical points", () => {
  assert.equal(haversine(40, -83, 40, -83), 0);
});

test("haversine ≈ 111.2 km per degree of latitude", () => {
  const d = haversine(40, -83, 41, -83);
  assert.ok(Math.abs(d - 111195) < 500, `expected ~111195 m, got ${d}`);
});

test("haversine is symmetric in its endpoints", () => {
  const a = haversine(40.1, -83.2, 40.4, -82.9);
  const b = haversine(40.4, -82.9, 40.1, -83.2);
  assert.ok(Math.abs(a - b) < 1e-6, `${a} vs ${b}`);
});

// --- tierBlurb: human copy per engagement tier --------------------------------------------------
test("tierBlurb: cold applies immediately", () => {
  assert.match(tierBlurb("cold", 0), /applies right away/);
});
test("tierBlurb: hot states the confirmation count", () => {
  assert.match(tierBlurb("hot", 3), /3 confirmations/);
});
test("tierBlurb: warm pluralises the confirmation count correctly", () => {
  assert.ok(tierBlurb("warm", 1).includes("1 confirmation "), "singular for 1");
  assert.match(tierBlurb("warm", 2), /2 confirmations/);
});

// --- gpsWithin: on-device radius check, boolean out ---------------------------------------------
test("gpsWithin: TRUE when the device sits inside the radius", async () => {
  setNavigator({ geolocation: geolocation({ latitude: 40.0001, longitude: -83.0 }) }); // ~11 m
  assert.equal(await gpsWithin(40.0, -83.0, 75), true);
});

test("gpsWithin: FALSE when the device is outside the radius", async () => {
  setNavigator({ geolocation: geolocation({ latitude: 40.01, longitude: -83.0 }) }); // ~1.1 km
  assert.equal(await gpsWithin(40.0, -83.0, 75), false);
});

test("gpsWithin: FALSE when geolocation is denied", async () => {
  setNavigator({ geolocation: geolocation(null) });
  assert.equal(await gpsWithin(40.0, -83.0, 75), false);
});

test("gpsWithin: FALSE when geolocation is unsupported", async () => {
  setNavigator({});  // no geolocation member
  assert.equal(await gpsWithin(40.0, -83.0, 75), false);
});

// --- confirmGpsCorroborated: the #4 regression guard --------------------------------------------
test("confirmGpsCorroborated: false for null/NaN suggested coords", async () => {
  setNavigator({
    permissions: { query: async () => ({ state: "granted" }) },
    geolocation: geolocation({ latitude: 40, longitude: -83 }),
  });
  assert.equal(await confirmGpsCorroborated(null, -83), false);
  assert.equal(await confirmGpsCorroborated(40, null), false);
  assert.equal(await confirmGpsCorroborated(Number.NaN, -83), false);
  assert.equal(await confirmGpsCorroborated(40, Number.NaN), false);
});

test("confirmGpsCorroborated: false when the Permissions API is unavailable", async () => {
  setNavigator({ geolocation: geolocation({ latitude: 40, longitude: -83 }) }); // no .permissions
  assert.equal(await confirmGpsCorroborated(40.0, -83.0), false);
});

test("confirmGpsCorroborated: 'prompt' state returns false WITHOUT prompting for geolocation", async () => {
  // THE regression: a confirm tap must not raise a permission prompt. getCurrentPosition must
  // never be reached when permission is merely promptable.
  let asked = false;
  setNavigator({
    permissions: { query: async () => ({ state: "prompt" }) },
    geolocation: geolocation({ latitude: 40, longitude: -83 }, () => { asked = true; }),
  });
  assert.equal(await confirmGpsCorroborated(40.0, -83.0), false);
  assert.equal(asked, false, "must not call getCurrentPosition on a confirm tap");
});

test("confirmGpsCorroborated: 'denied' state returns false without prompting", async () => {
  let asked = false;
  setNavigator({
    permissions: { query: async () => ({ state: "denied" }) },
    geolocation: geolocation({ latitude: 40, longitude: -83 }, () => { asked = true; }),
  });
  assert.equal(await confirmGpsCorroborated(40.0, -83.0), false);
  assert.equal(asked, false);
});

test("confirmGpsCorroborated: granted + on-site => true (the boost path)", async () => {
  setNavigator({
    permissions: { query: async () => ({ state: "granted" }) },
    geolocation: geolocation({ latitude: 40.0001, longitude: -83.0 }), // ~11 m, inside 75 m default
  });
  assert.equal(await confirmGpsCorroborated(40.0, -83.0), true);
});

test("confirmGpsCorroborated: granted but far from the suggested point => false", async () => {
  setNavigator({
    permissions: { query: async () => ({ state: "granted" }) },
    geolocation: geolocation({ latitude: 41.0, longitude: -83.0 }), // ~111 km
  });
  assert.equal(await confirmGpsCorroborated(40.0, -83.0), false);
});

test("confirmGpsCorroborated: a throwing Permissions API is treated as no-boost", async () => {
  setNavigator({
    permissions: { query: async () => { throw new Error("not supported"); } },
    geolocation: geolocation({ latitude: 40, longitude: -83 }),
  });
  assert.equal(await confirmGpsCorroborated(40.0, -83.0), false);
});
