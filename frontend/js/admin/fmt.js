// Pure presentation helpers for the operator dashboard. No DOM, no side effects, so the unit suite
// (frontend/test/admin-fmt.test.js) can pin them. `nowMs` is injectable for deterministic tests.

// Metres -> human distance. Sub-km stays in whole metres (a 320 m vs 1.40 km move reads very
// differently to an operator judging the 2 km cap); km gets two decimals for that near-cap precision.
export function formatDistance(m) {
  if (!Number.isFinite(m)) return "—";
  if (m < 1000) return `${Math.round(m)} m`;
  return `${(m / 1000).toFixed(2)} km`;
}

// lat, lon -> "40.01000, -83.01000" at ~1 m precision. Both must be finite or we show a dash rather
// than "NaN, NaN".
export function formatCoord(lat, lon) {
  if (!Number.isFinite(lat) || !Number.isFinite(lon)) return "—";
  return `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
}

// ISO timestamp -> compact relative age ("just now", "5 min ago", "3 hr ago", "2 days ago") and an
// absolute date past a week. Unparseable/absent input yields "" so a card never prints "Invalid Date".
export function relativeTime(iso, nowMs = Date.now()) {
  if (!iso) return "";
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const s = Math.round((nowMs - t) / 1000);
  if (s < 45) return "just now";
  const m = Math.round(s / 60);
  if (m < 60) return `${m} min ago`;
  const h = Math.round(m / 60);
  if (h < 24) return `${h} hr ago`;
  const d = Math.round(h / 24);
  if (d <= 7) return `${d} day${d === 1 ? "" : "s"} ago`;
  // Older than a week: a plain date is clearer than "37 days ago".
  return new Date(t).toISOString().slice(0, 10);
}

// Truncate an anonymized ip_hash for display — the full 64-hex is noise; the head is enough to eyeball
// that two rows share an actor. Never used as an identifier, only a visual tag.
export function shortHash(h, n = 10) {
  if (!h) return "";
  return h.length > n ? `${h.slice(0, n)}…` : h;
}
