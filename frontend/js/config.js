export const API = "/api";
// National fallback view (geographic center of the contiguous US). The map normally fits itself
// to the live data's coverage bbox from /api/meta; this is only used when coverage is unavailable
// or so wide (whole US) that a fixed national frame reads better than a zoomed-out fitBounds.
export const DEFAULT_VIEW = { center: [39.5, -98.0], zoom: 4 };
export const MIN_ZOOM = 3; // allow zooming out to the full US (incl. AK/HI), not past the continent

export const BUCKET_COLORS = { high: "#1a9850", medium: "#f1a340", low: "#d73027" };
export const ORG_TYPE_LABELS = {
  charity_store: "Charity store",
  thrift_store: "Thrift store",
  consignment: "Consignment / resale",
  drop_bin: "Donation bin",
  donation_center: "Donation center",
  mutual_aid: "Mutual aid",
  church_drive: "Church drive",
  other: "Donation location",
};

export const ORG_TYPES = Object.keys(ORG_TYPE_LABELS);

export let META = null;

export async function loadMeta() {
  try {
    const r = await fetch(`${API}/meta`);
    META = await r.json();
  } catch (e) {
    META = { sources: [], turnstile_sitekey: null };
  }
  return META;
}

// Client constants for the correction flow — the API is the single source of truth, but
// fall back to the documented defaults if /meta is unreachable.
export function gpsRadiusM() {
  return (META && META.gps_radius_m) || 75;
}
export function maxMoveM() {
  return (META && META.correction_max_move_m) || 2000;
}
