// Operator API client. Every /admin route is gated by the OPERATOR_TOKEN, sent as the
// `X-Operator-Token` header (deps.require_operator). A wrong/absent token 404s — indistinguishable
// from a missing route — so callers treat 404 on these routes as "not authenticated".
//
// The token lives in sessionStorage (cleared when the tab closes: a shared-machine safe default) and
// is mirrored in memory. It is NEVER placed in the URL, the DOM, or a log line — only this header.

const API = "/api";
const TOKEN_KEY = "opendrop_operator_token";

let _token = null;
try { _token = sessionStorage.getItem(TOKEN_KEY) || null; } catch (e) { _token = null; }

export function getToken() { return _token; }

export function setToken(t) {
  _token = t || null;
  try {
    if (_token) sessionStorage.setItem(TOKEN_KEY, _token);
    else sessionStorage.removeItem(TOKEN_KEY);
  } catch (e) { /* private mode — in-memory only, re-auth next reload */ }
}

export function clearToken() { setToken(null); }

// Core operator fetch. Injects the token header; parses the uniform {error:{code,message}} envelope
// and throws {status, ...body} so callers can branch on status/error.code. JSON body only when given.
async function op(path, { method = "GET", body = null } = {}) {
  const headers = { "X-Operator-Token": _token || "" };
  const init = { method, headers };
  if (body !== null) {
    headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(body);
  }
  const r = await fetch(`${API}${path}`, init);
  const d = await r.json().catch(() => ({}));
  if (!r.ok) throw { status: r.status, ...d };
  return d;
}

// --- pending photo-move review queue (migration 0013) ---
export const listPendingMoves = () => op("/admin/images/pending-moves");
export const applyMove = (imgId) => op(`/admin/images/${imgId}/apply-move`, { method: "POST" });
export const rejectMove = (imgId) => op(`/admin/images/${imgId}/reject-move`, { method: "POST" });

// --- broader moderation queue: reports + takedown/restore ---
export const listReports = () => op("/admin/reports");
export const resolveReport = (id, note) => op(`/admin/reports/${id}/resolve`, { method: "POST", body: { note: note || null } });
export const takedownLocation = (id, reason) => op(`/admin/locations/${id}/takedown`, { method: "POST", body: { reason: reason || null } });
export const restoreLocation = (id) => op(`/admin/locations/${id}/restore`, { method: "POST" });
export const takedownImage = (id, reason) => op(`/admin/images/${id}/takedown`, { method: "POST", body: { reason: reason || null } });
export const restoreImage = (id) => op(`/admin/images/${id}/restore`, { method: "POST" });

// --- revert tooling (moderation_audit) ---
export const locationAudit = (id) => op(`/admin/locations/${id}/audit`);
export const revertAudit = (id, note) => op(`/admin/audit/${id}/revert`, { method: "POST", body: { note: note || null } });
export const revertAll = (locId, note) => op(`/admin/locations/${locId}/revert-all`, { method: "POST", body: { note: note || null } });
export const revertActor = (ipHash, note) => op("/admin/revert-actor", { method: "POST", body: { actor_ip_hash: ipHash, note: note || null } });
