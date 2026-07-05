import { BUCKET_COLORS, ORG_TYPE_LABELS } from "./config.js";

// Raw hex — ONLY for Leaflet SVG marker rendering (presentation attributes can't resolve var()).
export function bucketColor(bucket) {
  return BUCKET_COLORS[bucket] || BUCKET_COLORS.low;
}

// Theme-following color for HTML UI (inline style backgrounds resolve CSS custom properties, so
// dots/chips in the popover and list follow light/dark automatically).
export function bucketCssColor(bucket) {
  return { high: "var(--high)", medium: "var(--medium)", low: "var(--low)" }[bucket] || "var(--low)";
}

// Plain-English status — never "confidence" jargon in user-facing UI.
export function bucketLabel(bucket) {
  return { high: "Likely still there", medium: "Needs a check", low: "Unverified — help confirm" }[bucket] || "Unknown";
}

// One shared progress formatter for every community-consensus meter (pin moves, detail changes) —
// replaces the four drifted inline variants ("needs 2 more · 1/3", "ready").
export function supportLine(support, required) {
  // Guard non-finite / zero-required inputs: Math.max(0, NaN) is NaN and NaN > 0 is false, so the
  // unguarded code fell through to the "Confirmed" success copy on missing data — a false positive.
  if (!Number.isFinite(support) || !Number.isFinite(required) || required <= 0) return "Updating shortly";
  const left = Math.max(0, required - support);
  return left > 0
    ? `${support} of ${required} neighbor${required === 1 ? "" : "s"} confirmed — ${left} more to apply`
    : "Confirmed — updating shortly";
}

export function orgTypeLabel(t) {
  return ORG_TYPE_LABELS[t] || "Donation location";
}

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}
