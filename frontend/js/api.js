import { API } from "./config.js";

export async function fetchLocations(bounds, cluster = "auto") {
  const bbox = [bounds.getWest(), bounds.getSouth(), bounds.getEast(), bounds.getNorth()];
  const params = new URLSearchParams({ bbox: bbox.join(","), cluster });
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
