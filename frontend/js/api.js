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

export async function voteImage(imgId, vote, token) {
  const r = await fetch(`${API}/images/${imgId}/vote`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ vote, turnstile_token: token }),
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

// --- Community pin corrections + signals ---

export async function postCorrection(locId, payload) {
  const r = await fetch(`${API}/locations/${locId}/corrections`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw { status: r.status, ...d };
  return d;
}

export async function voteCorrection(corrId, payload) {
  const r = await fetch(`${API}/corrections/${corrId}/vote`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw { status: r.status, ...d };
  return d;
}

export async function postAttribute(locId, payload) {
  const r = await fetch(`${API}/locations/${locId}/attributes`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw { status: r.status, ...d };
  return d;
}

// Retract the caller's own rating for one attribute (rating deselect). DELETE carries a JSON body
// so the Turnstile token rides along like every other write.
export async function deleteAttribute(locId, attribute, token) {
  const r = await fetch(`${API}/locations/${locId}/attributes/${attribute}`, {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ turnstile_token: token }),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw { status: r.status, ...d };
  return d;
}

// --- Community field corrections (name / type / org / address) ---

export async function postFieldCorrection(locId, payload) {
  const r = await fetch(`${API}/locations/${locId}/field-corrections`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw { status: r.status, ...d };
  return d;
}

export async function voteFieldCorrection(corrId, payload) {
  const r = await fetch(`${API}/field-corrections/${corrId}/vote`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw { status: r.status, ...d };
  return d;
}

export async function fetchOrgs() {
  try {
    const r = await fetch(`${API}/orgs`);
    if (!r.ok) return [];
    return (await r.json()).orgs || [];
  } catch (e) {
    return [];
  }
}

// --- Public content reporting (location / photo) ---

export async function reportLocation(locId, payload) {
  const r = await fetch(`${API}/locations/${locId}/report`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw { status: r.status, ...d };
  return d;
}

export async function reportImage(imgId, payload) {
  const r = await fetch(`${API}/images/${imgId}/report`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw { status: r.status, ...d };
  return d;
}

export async function reverseGeocode(lat, lon) {
  try {
    const r = await fetch(`${API}/reverse?lat=${encodeURIComponent(lat)}&lon=${encodeURIComponent(lon)}`);
    if (!r.ok) return null;
    return (await r.json()).address || null;
  } catch (e) {
    return null;
  }
}
