// Unit tests for js/geo.js — one-shot device geolocation → Leaflet LatLng (or null on
// denial/timeout/unsupported). Unlike the correction GPS check, this returns the actual coordinate
// (a pin the user is deliberately dropping), so the success path must yield an L.latLng, never a
// bare boolean.
import { test } from "node:test";
import assert from "node:assert/strict";

import { currentPosition } from "../js/geo.js";

// navigator and L are getter-style globals in Node; override via defineProperty.
function setGlobal(name, value) {
  Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
}

test("currentPosition resolves null when geolocation is unsupported", async () => {
  setGlobal("navigator", {});
  assert.equal(await currentPosition(), null);
});

test("currentPosition resolves null when the user denies or it times out", async () => {
  setGlobal("navigator", { geolocation: { getCurrentPosition: (ok, err) => err(new Error("denied")) } });
  assert.equal(await currentPosition(), null);
});

test("currentPosition resolves a Leaflet LatLng on success", async () => {
  setGlobal("L", { latLng: (lat, lng) => ({ lat, lng, __latlng: true }) });
  setGlobal("navigator", {
    geolocation: { getCurrentPosition: (ok) => ok({ coords: { latitude: 40.1, longitude: -83.2 } }) },
  });
  assert.deepEqual(await currentPosition(), { lat: 40.1, lng: -83.2, __latlng: true });
});

test("currentPosition forwards a custom timeout into the geolocation options", async () => {
  let opts = null;
  setGlobal("navigator", {
    geolocation: { getCurrentPosition: (ok, err, o) => { opts = o; err(); } },
  });
  await currentPosition({ timeout: 1234 });
  assert.equal(opts.timeout, 1234);
  assert.equal(opts.enableHighAccuracy, true);
  assert.equal(opts.maximumAge, 0);
});
