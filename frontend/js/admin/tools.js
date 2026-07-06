// Revert power-tools: inspect a location's moderation_audit trail and undo auto-applied corrections
// one at a time or all at once, and bulk-revert every still-applied correction by one actor ip_hash.
// These wrap the revert endpoints (revert_audit / revert-all / revert-actor) — the same tooling that
// covers an approved photo move, since apply-move writes an ordinary moderation_audit row.

import { locationAudit, revertActor, revertAll, revertAudit } from "./api.js";
import { formatCoord, relativeTime, shortHash } from "./fmt.js";
import { el, flagAuthLost, isAuthError, reportError } from "./ui.js";
import { toast } from "../toast.js";

let auditBox = null;   // results container for the location-audit lookup
let currentLoc = null; // location id currently loaded in the audit box

// Compact "prior → new" for one audit row. Pin moves render as coordinates; field edits show the
// changed value. All values go through textContent (via el `text`) so nothing is parsed as HTML.
function summarizeChange(row) {
  if (row.kind === "pin_correction") {
    const p = row.prior_value || {}, n = row.new_value || {};
    return `${formatCoord(p.lat, p.lon)}  →  ${formatCoord(n.lat, n.lon)}`;
  }
  const one = (v) => (v && typeof v === "object" ? Object.values(v).filter(Boolean).join(", ") : String(v ?? "∅"));
  return `${one(row.prior_value)}  →  ${one(row.new_value)}`;
}

function auditRow(row) {
  const reverted = !!row.reverted_at;
  const head = el("div", { class: "audit-head" }, [
    el("span", { class: "audit-kind", text: row.kind === "pin_correction" ? "pin move" : `field: ${row.field || "?"}` }),
    reverted ? el("span", { class: "pill pill-reverted", text: "reverted" }) : null,
    el("span", { class: "muted audit-when", text: relativeTime(row.applied_at) }),
    el("span", { class: "muted", text: `by ${shortHash(row.actor_ip_hash)}` }),
  ]);
  const change = el("div", { class: "audit-change" }, [el("code", { text: summarizeChange(row) })]);
  const foot = reverted
    ? el("div", { class: "muted audit-note", text: row.reverted_note ? `note: ${row.reverted_note}` : "" })
    : el("button", { class: "btn danger btn-sm", type: "button", onClick: (e) => onRevertOne(e.target.closest(".audit-row"), row.id) }, ["Revert this"]);
  return el("div", { class: "audit-row", dataset: { auditId: row.id } }, [head, change, foot]);
}

async function onRevertOne(rowEl, auditId) {
  const btn = rowEl.querySelector("button"); if (btn) btn.disabled = true;
  try {
    const d = await revertAudit(auditId, "");
    const r = d && d.result;
    toast(r === "reverted" ? "Reverted — the value was restored."
      : r === "superseded" ? "Marked reverted (a newer edit had already replaced it)."
      : "Already reverted.", r === "reverted" ? "success" : "info");
    if (currentLoc != null) loadAudit(currentLoc);   // refresh the trail
  } catch (e) {
    if (btn) btn.disabled = false;
    if (e.status === 409) { toast("Already reverted.", "info"); if (currentLoc != null) loadAudit(currentLoc); }
    else reportError(e, "Couldn't revert that entry.");
  }
}

async function onRevertAll() {
  if (currentLoc == null) return;
  if (!confirm(`Revert EVERY un-reverted correction on location #${currentLoc}? This unwinds the whole edit chain to the original values.`)) return;
  try {
    const d = await revertAll(currentLoc, "revert-all from operator console");
    toast(`Reverted ${d.reverted}, superseded ${d.superseded}.`, "success");
    loadAudit(currentLoc);
  } catch (e) { reportError(e, "Couldn't revert the location."); }
}

async function loadAudit(locId) {
  currentLoc = locId;
  auditBox.innerHTML = "";
  auditBox.appendChild(el("p", { class: "muted loading-line", text: `Loading audit for #${locId}…` }));
  try {
    const data = await locationAudit(locId);
    const rows = data.audit || [];
    auditBox.innerHTML = "";
    if (!rows.length) { auditBox.appendChild(el("p", { class: "muted", text: `No moderation-audit entries for location #${locId}.` })); return; }
    const active = rows.filter((r) => !r.reverted_at).length;
    auditBox.appendChild(el("div", { class: "audit-summary" }, [
      el("span", { text: `${rows.length} entr${rows.length === 1 ? "y" : "ies"} · ${active} still applied` }),
      active ? el("button", { class: "btn danger btn-sm", type: "button", onClick: onRevertAll }, ["Revert all"]) : null,
    ]));
    for (const r of rows) auditBox.appendChild(auditRow(r));
  } catch (e) {
    if (isAuthError(e)) { flagAuthLost(); return; }
    auditBox.innerHTML = "";
    auditBox.appendChild(el("p", { class: "muted", text: "Couldn't load the audit trail." }));
  }
}

function auditTool() {
  const input = el("input", { type: "number", min: "1", class: "tool-input", placeholder: "location id", "aria-label": "Location id" });
  const go = () => {
    const id = parseInt(input.value, 10);
    if (!Number.isInteger(id) || id < 1) { toast("Enter a valid location id.", "error"); return; }
    loadAudit(id);
  };
  input.addEventListener("keydown", (e) => { if (e.key === "Enter") go(); });
  auditBox = el("div", { class: "audit-box" });
  return el("section", { class: "tool-card" }, [
    el("h3", { text: "Location audit & revert" }),
    el("p", { class: "muted", text: "Inspect the moderation-audit trail for a location and undo any auto-applied correction (including an approved photo move)." }),
    el("div", { class: "tool-row" }, [input, el("button", { class: "btn primary", type: "button", onClick: go }, ["Load audit"])]),
    auditBox,
  ]);
}

function actorTool() {
  const input = el("input", { type: "text", class: "tool-input wide", placeholder: "actor ip_hash (from a report or audit row)", "aria-label": "Actor ip hash" });
  const note = el("input", { type: "text", class: "tool-input wide", maxlength: "500", placeholder: "note (optional)", "aria-label": "Revert note" });
  const go = async () => {
    const h = input.value.trim();
    if (h.length < 8) { toast("Enter the full actor ip_hash (min 8 chars).", "error"); return; }
    if (!confirm("Revert EVERY still-applied correction authored by this actor, across all locations?")) return;
    try {
      const d = await revertActor(h, note.value.trim());
      toast(`Reverted ${d.reverted}, superseded ${d.superseded} across ${d.locations_affected} location(s).`, "success");
    } catch (e) {
      if (e.status === 422) toast("That ip_hash doesn't look valid.", "error");
      else reportError(e, "Couldn't revert by actor.");
    }
  };
  return el("section", { class: "tool-card" }, [
    el("h3", { text: "Revert by actor" }),
    el("p", { class: "muted", text: "Undo every un-reverted correction authored by one submitter (by anonymized ip_hash) — for cleaning up after an abuse run." }),
    el("div", { class: "tool-col" }, [input, note, el("div", {}, [el("button", { class: "btn danger", type: "button", onClick: go }, ["Revert all by this actor"])])]),
  ]);
}

export function render(container) {
  container.innerHTML = "";
  currentLoc = null;
  container.appendChild(el("div", { class: "view-head" }, [el("h2", { text: "Revert tools" })]));
  container.appendChild(el("p", { class: "view-lead muted", text: "Undo auto-applied pin/detail corrections. Reverts restore the exact prior value and are safe to re-run." }));
  container.appendChild(auditTool());
  container.appendChild(actorTool());
}

export function teardown() { auditBox = null; currentLoc = null; }
