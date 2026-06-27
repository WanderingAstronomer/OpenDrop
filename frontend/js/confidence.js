import { BUCKET_COLORS, ORG_TYPE_LABELS } from "./config.js";

export function bucketColor(bucket) {
  return BUCKET_COLORS[bucket] || BUCKET_COLORS.low;
}

export function bucketLabel(bucket) {
  return { high: "High confidence", medium: "Medium confidence", low: "Low / unverified" }[bucket] || "Unknown";
}

export function orgTypeLabel(t) {
  return ORG_TYPE_LABELS[t] || "Donation location";
}

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])
  );
}
