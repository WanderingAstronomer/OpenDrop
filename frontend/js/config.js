export const API = "/api";
export const DEFAULT_VIEW = { center: [39.96, -82.99], zoom: 11 }; // Columbus, OH
export const MIN_ZOOM = 4; // cap zoom-out so you can't shrink past ~continental US

export const BUCKET_COLORS = { high: "#1a9850", medium: "#f1a340", low: "#d73027" };
export const ORG_TYPE_LABELS = {
  charity_store: "Charity store",
  thrift_store: "Thrift store",
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
