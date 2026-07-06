// Unit tests for the pure presentation helpers in js/admin/fmt.js (operator dashboard).
import { test } from "node:test";
import assert from "node:assert/strict";

import { formatDistance, formatCoord, relativeTime, shortHash } from "../js/admin/fmt.js";

test("formatDistance keeps sub-km in metres and km to two decimals", () => {
  assert.equal(formatDistance(0), "0 m");
  assert.equal(formatDistance(320.4), "320 m");
  assert.equal(formatDistance(999), "999 m");
  assert.equal(formatDistance(1000), "1.00 km");
  assert.equal(formatDistance(1401), "1.40 km");
  assert.equal(formatDistance(1999), "2.00 km");
});

test("formatDistance guards non-finite input with a dash", () => {
  assert.equal(formatDistance(NaN), "—");
  assert.equal(formatDistance(Infinity), "—");
  assert.equal(formatDistance(null), "—");
  assert.equal(formatDistance(undefined), "—");
});

test("formatCoord prints five decimals and dashes on non-finite", () => {
  assert.equal(formatCoord(40.01, -83.01), "40.01000, -83.01000");
  assert.equal(formatCoord(0, 0), "0.00000, 0.00000");
  assert.equal(formatCoord(NaN, -83), "—");
  assert.equal(formatCoord(40, undefined), "—");
});

test("relativeTime bucketises age from a fixed 'now'", () => {
  const now = Date.parse("2026-07-05T12:00:00Z");
  assert.equal(relativeTime("2026-07-05T11:59:40Z", now), "just now");
  assert.equal(relativeTime("2026-07-05T11:50:00Z", now), "10 min ago");
  assert.equal(relativeTime("2026-07-05T09:00:00Z", now), "3 hr ago");
  assert.equal(relativeTime("2026-07-04T12:00:00Z", now), "1 day ago");
  assert.equal(relativeTime("2026-07-01T12:00:00Z", now), "4 days ago");
  // Older than a week collapses to an absolute date.
  assert.equal(relativeTime("2026-05-01T12:00:00Z", now), "2026-05-01");
});

test("relativeTime returns empty string on missing/unparseable input", () => {
  assert.equal(relativeTime(null), "");
  assert.equal(relativeTime(""), "");
  assert.equal(relativeTime("not a date"), "");
});

test("shortHash truncates long hashes and passes short ones through", () => {
  assert.equal(shortHash("0123456789abcdef0123", 10), "0123456789…");
  assert.equal(shortHash("short"), "short");
  assert.equal(shortHash(""), "");
  assert.equal(shortHash(null), "");
});
