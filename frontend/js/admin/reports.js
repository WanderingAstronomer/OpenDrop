// Reports view: the open community-report queue (GET /admin/reports) with the hard-moderation
// actions — resolve, take down / restore a location, remove / restore a photo. Kept compact: one
// optional note field per card feeds both Resolve and Takedown.

import {
  listReports, resolveReport, restoreImage, restoreLocation, takedownImage, takedownLocation,
} from "./api.js";
import { relativeTime, shortHash } from "./fmt.js";
import { el, flagAuthLost, isAuthError, reportError } from "./ui.js";
import { toast } from "../toast.js";

let listEl = null;
let countEl = null;

function statusPill(text) {
  if (!text) return null;
  return el("span", { class: `pill pill-${text}`, text });
}

function dropCard(card) {
  card.remove();
  const remaining = listEl.querySelectorAll(".report-card").length;
  if (countEl) countEl.textContent = remaining ? `${remaining} open` : "";
  if (!remaining) listEl.appendChild(el("div", { class: "empty-state" }, [el("p", { text: "No open reports. ✓" })]));
}

function noteValue(card) {
  const inp = card.querySelector(".report-note");
  return inp ? inp.value.trim() : "";
}

function reportCard(r) {
  const isImage = r.target_type === "image";
  const note = el("input", {
    class: "report-note", type: "text", maxlength: "500",
    placeholder: "note (optional) — recorded on resolve / takedown",
    "aria-label": "Moderation note",
  });

  // Target line + a link to the affected location on the public map.
  const locId = isImage ? r.img_location_id : r.target_id;
  const targetLine = isImage
    ? el("div", { class: "report-target" }, [
        el("span", { class: "report-kind" }, ["🖼 Photo"]),
        el("span", { class: "report-ref", text: `#${r.target_id}` }),
        r.img_location_id ? el("span", { class: "muted" }, ["on "]) : null,
        r.img_location_id ? el("a", { href: `/#bin/${r.img_location_id}`, target: "_blank", rel: "noopener", text: `location #${r.img_location_id}` }) : null,
        statusPill(r.img_status),
        r.img_removed ? statusPill("removed") : null,
      ])
    : el("div", { class: "report-target" }, [
        el("span", { class: "report-kind" }, ["📍 Location"]),
        el("a", { href: `/#bin/${r.target_id}`, target: "_blank", rel: "noopener", text: r.loc_name || `#${r.target_id}` }),
        el("span", { class: "report-ref", text: `#${r.target_id}` }),
        statusPill(r.loc_status),
      ]);

  const actions = el("div", { class: "report-actions" }, [
    el("button", { class: "btn ghost", type: "button", onClick: (e) => onResolve(e.target.closest(".report-card"), r.id) }, ["Resolve"]),
    isImage
      ? el("button", { class: "btn danger", type: "button", onClick: (e) => onTakedownImage(e.target.closest(".report-card"), r.target_id) }, ["Remove photo"])
      : el("button", { class: "btn danger", type: "button", onClick: (e) => onTakedownLocation(e.target.closest(".report-card"), r.target_id) }, ["Take down"]),
    isImage
      ? el("button", { class: "btn quiet", type: "button", onClick: () => onRestoreImage(r.target_id) }, ["Restore photo"])
      : el("button", { class: "btn quiet", type: "button", onClick: () => onRestoreLocation(r.target_id) }, ["Restore"]),
  ]);

  return el("div", { class: "report-card", dataset: { reportId: r.id, locId: locId || "" } }, [
    targetLine,
    r.reason ? el("p", { class: "report-reason", text: r.reason }) : el("p", { class: "report-reason muted", text: "(no reason given)" }),
    el("div", { class: "report-meta muted" }, [`reporter ${shortHash(r.reporter_ip_hash)} · ${relativeTime(r.created_at)}`]),
    note,
    actions,
  ]);
}

async function onResolve(card, id) {
  const btns = card.querySelectorAll("button"); btns.forEach((b) => (b.disabled = true));
  try {
    await resolveReport(id, noteValue(card));
    toast("Report resolved.", "success");
    dropCard(card);
  } catch (e) {
    btns.forEach((b) => (b.disabled = false));
    if (e.status === 404) { toast("Already resolved.", "info"); dropCard(card); }
    else reportError(e, "Couldn't resolve the report.");
  }
}

async function onTakedownLocation(card, id) {
  if (!confirm("Hide this location from the public map? It stays down until you restore it.")) return;
  const btns = card.querySelectorAll("button"); btns.forEach((b) => (b.disabled = true));
  try {
    await takedownLocation(id, noteValue(card));
    toast("Location taken down.", "success");
    dropCard(card);   // takedown auto-resolves this location's open reports
  } catch (e) {
    btns.forEach((b) => (b.disabled = false));
    reportError(e, "Couldn't take down the location.");
  }
}

async function onTakedownImage(card, id) {
  if (!confirm("Permanently remove this photo? The image file is deleted (this can't be undone if the file is purged).")) return;
  const btns = card.querySelectorAll("button"); btns.forEach((b) => (b.disabled = true));
  try {
    await takedownImage(id, noteValue(card));
    toast("Photo removed.", "success");
    dropCard(card);
  } catch (e) {
    btns.forEach((b) => (b.disabled = false));
    reportError(e, "Couldn't remove the photo.");
  }
}

async function onRestoreLocation(id) {
  try { await restoreLocation(id); toast("Location restored.", "success"); load(); }
  catch (e) { reportError(e, "Couldn't restore the location."); }
}

async function onRestoreImage(id) {
  try {
    const d = await restoreImage(id);
    toast(d && d.file_present === false ? "Un-hidden, but the file was already purged." : "Photo restored.", "success");
    load();
  } catch (e) { reportError(e, "Couldn't restore the photo."); }
}

function renderList(reports) {
  listEl.innerHTML = "";
  listEl.removeAttribute("aria-busy");
  if (countEl) countEl.textContent = reports.length ? `${reports.length} open` : "";
  if (!reports.length) { listEl.appendChild(el("div", { class: "empty-state" }, [el("p", { text: "No open reports. ✓" })])); return; }
  for (const r of reports) listEl.appendChild(reportCard(r));
}

async function load() {
  listEl.setAttribute("aria-busy", "true");
  listEl.innerHTML = "";
  listEl.appendChild(el("p", { class: "muted loading-line", text: "Loading reports…" }));
  try {
    const data = await listReports();
    renderList(data.reports || []);
  } catch (e) {
    if (isAuthError(e)) { flagAuthLost(); return; }
    listEl.innerHTML = "";
    listEl.removeAttribute("aria-busy");
    listEl.appendChild(el("div", { class: "empty-state" }, [
      el("p", { text: "Couldn't load reports — the server may be unreachable." }),
      el("button", { class: "btn ghost", type: "button", onClick: load }, ["Retry"]),
    ]));
  }
}

export function render(container) {
  container.innerHTML = "";
  countEl = el("span", { class: "view-count muted" });
  container.appendChild(el("div", { class: "view-head" }, [
    el("h2", { text: "Open reports" }),
    countEl,
    el("button", { class: "btn ghost view-refresh", type: "button", onClick: load }, ["↻ Refresh"]),
  ]));
  container.appendChild(el("p", { class: "view-lead muted", text:
    "Community flags awaiting triage. Resolve closes the report; Take down hides the content (reversible)." }));
  listEl = el("div", { class: "reports-list", "aria-busy": "true" });
  container.appendChild(listEl);
  load();
}

export function teardown() { listEl = countEl = null; }
