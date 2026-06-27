import { API } from "./config.js";

export async function fetchLocations(bounds, cluster = "auto", types = null) {
  const bbox = [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()];
  const params = new URLSearchParams({ bbox: bbox.join(","), cluster });
  if (types) params.set("types", types);
  const r = await fetch(`${API}/locations?${params.toString()}`);
  if (!r.ok) throw await r.json().catch(() => ({}));
  return r.json();
}

export async function fetchDetail(id) {
  const r = await fetch(`${API}/locations/${id}`);
  if (!r.ok) throw { status: r.status, ...(await r.json().catch(() => ({}))) };
  return r.json();
}

export async function postVote(id, vote, token) {
  const r = await fetch(`${API}/locations/${id}/vote`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ vote, turnstile_token: token }),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw { status: r.status, ...d };
  return d;
}

export async function postSubmit(payload) {
  const r = await fetch(`${API}/locations`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw { status: r.status, ...d };
  return d;
}

export async function fetchImages(id, includeLow = false) {
  const r = await fetch(`${API}/locations/${id}/images${includeLow ? "?include_low=true" : ""}`);
  if (!r.ok) return { images: [] };
  return r.json();
}

export async function voteImage(imgId, vote) {
  const r = await fetch(`${API}/images/${imgId}/vote`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ vote }),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw { status: r.status, ...d };
  return d;
}

export async function uploadImage(locId, file, token, suggested) {
  const fd = new FormData();
  fd.append("file", file);
  if (token) fd.append("turnstile_token", token);
  if (suggested) {
    fd.append("suggested_lat", suggested.lat);
    fd.append("suggested_lon", suggested.lon);
  }
  const r = await fetch(`${API}/locations/${locId}/images`, { method: "POST", body: fd });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw { status: r.status, ...d };
  return d;
}
