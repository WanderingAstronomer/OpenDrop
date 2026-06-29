import { fetchDetail, reportLocation } from "./api.js";
import { mountSignals } from "./attributes.js";
import { bucketColor, bucketLabel, esc, orgTypeLabel } from "./confidence.js";
import { startCorrection, submitCorrectionVote } from "./corrections.js";
import { mountFieldEdits, startFieldEdit } from "./fieldedit.js";
import { mountPhotos } from "./photos.js";
import { app } from "./state.js";
import { toast } from "./toast.js";
import { guard } from "./turnstile.js";
import { mountVote } from "./vote.js";

// Inline "report this location" form revealed under the popover footer. A report only files a
// complaint for moderator review — it never hides a listing on its own (anti-grief: one actor
// can't pull a seed pin).
function openLocationReport(container, locId) {
  if (container.dataset.open) { container.innerHTML = ""; delete container.dataset.open; return; }
  container.dataset.open = "1";
  container.innerHTML = `
    <div class="po-report-hint">Wrong, offensive, or doesn't exist? A moderator will review it. Reporting never removes a listing on its own.</div>
    <input class="po-report-reason" maxlength="500" type="text" placeholder="What's the problem? (optional)" aria-label="Reason for report" />
    <div class="ts po-report-ts"></div>
    <div class="po-report-actions">
      <button class="btn tiny danger po-report-send" type="button">Send report</button>
      <button class="btn tiny po-report-cancel" type="button">Cancel</button>
    </div>`;
  const tsHost = container.querySelector(".po-report-ts");
  const close = () => { container.innerHTML = ""; delete container.dataset.open; };
  container.querySelector(".po-report-cancel").onclick = close;
  container.querySelector(".po-report-send").onclick = async (e) => {
    const reason = container.querySelector(".po-report-reason").value.trim();
    try {
      await guard(tsHost, e.currentTarget, { action: "report" },
        (token) => reportLocation(locId, { reason: reason || null, turnstile_token: token }));
      toast("Thanks — reported for review", "success");
      close();
    } catch (err) {
      if (err.status === 403) toast("Please complete the verification first", "error");
      else if (err.status === 429) toast("Daily report limit reached", "error");
      else if (err.status === 422) toast(err.error?.message || "Report rejected", "error");
      else if (err.status === 404) toast("That location is no longer available", "error");
      else toast("Couldn't file the report", "error");
    }
  };
}

function addrHtml(a) {
  if (!a) return "";
  const parts = [a.line, [a.city, a.state].filter(Boolean).join(", "), a.postal_code].filter(Boolean);
  return parts.length ? `<div class="po-row"><span class="po-ic">📍</span><span>${esc(parts.join(" · "))}</span></div>` : "";
}

function linksHtml(d) {
  const out = [];
  if (d.website) out.push(`<a href="${esc(d.website)}" target="_blank" rel="noopener nofollow">Website ↗</a>`);
  if (d.phone) out.push(`<a href="tel:${esc(String(d.phone).replace(/[^\d+]/g, ""))}">Call</a>`);
  return out.length ? `<div class="po-links">${out.join("")}</div>` : "";
}

function confHtml(d) {
  return `<span class="dot" style="background:${bucketColor(d.bucket)}"></span>
    <span class="conf-label">${bucketLabel(d.bucket)}</span>
    <span class="conf-score" title="confidence score">${Math.round(d.confidence)}</span>
    <span class="conf-tally">👍 ${d.upvotes} · 👎 ${d.denies}</span>`;
}

function mountCorrections(host, d, latlng) {
  const open = d.open_corrections || [];
  if (!open.length) { host.innerHTML = ""; return; }
  host.innerHTML = `<div class="po-fixes">
    <div class="po-fixes-t">📍 Pin move suggested</div>
    ${open.map((c) => {
      const left = Math.max(0, c.required_support - c.support);
      return `<div class="po-fix" data-cid="${c.id}">
        ${c.note ? `<div class="fix-note">“${esc(c.note)}”</div>` : ""}
        <div class="fix-meter">${left > 0 ? `needs ${left} more` : "ready"} · ${c.support}/${c.required_support}</div>
        <div class="ts fix-ts"></div>
        <div class="fix-btns">
          <button class="btn tiny confirm" type="button" data-cid="${c.id}">Looks right</button>
          <button class="btn tiny deny" type="button" data-cid="${c.id}">No</button>
        </div>
      </div>`;
    }).join("")}
  </div>`;

  host.querySelectorAll(".po-fix").forEach((row) => {
    const cid = Number(row.dataset.cid);
    const tsHost = row.querySelector(".fix-ts");
    const onVote = async (confirm, btn) => {
      try {
        const res = await submitCorrectionVote({ corrId: cid, confirm, host: tsHost, btn });
        if (res.applied) {
          toast("Pin updated — thank you!", "success");
          app.refresh();
          app.map.closePopup();
        } else if (res.status === "rejected") {
          toast("Thanks — suggestion dismissed", "success");
          row.remove();
        } else {
          const left = Math.max(0, res.required_support - res.support);
          row.querySelector(".fix-meter").textContent =
            `${left > 0 ? `needs ${left} more` : "ready"} · ${res.support}/${res.required_support}`;
          toast("Thanks — recorded", "success");
        }
      } catch (e) {
        if (e.status === 409 && e.error?.code === "self_vote") toast("You can't vote on your own suggestion", "info");
        else if (e.status === 409) toast("That suggestion is already closed", "info");
        else if (e.status === 403) toast("Please complete the verification", "error");
        else toast("Couldn't record your vote", "error");
      }
    };
    row.querySelector(".confirm").onclick = (e) => onVote(true, e.currentTarget);
    row.querySelector(".deny").onclick = (e) => onVote(false, e.currentTarget);
  });
  void latlng;
}

export async function openPopover(map, latlng, id) {
  let d;
  try {
    d = await fetchDetail(id);
  } catch (e) {
    toast("Couldn't load that location", "error");
    return;
  }
  const opener = document.activeElement;  // restore focus here when the popover closes
  const div = document.createElement("div");
  div.className = "popover";
  div.setAttribute("role", "dialog");
  div.setAttribute("aria-label", d.name);
  div.tabIndex = -1;
  div.innerHTML = `
    <header class="po-head">
      <h3 class="po-title">${esc(d.name)}</h3>
      <div class="po-sub">${esc(orgTypeLabel(d.org_type))}${d.org_name ? ` · ${esc(d.org_name)}` : ""}</div>
    </header>
    <div class="po-body">
      ${addrHtml(d.address)}
      ${d.hours_raw ? `<div class="po-row"><span class="po-ic">🕑</span><span>${esc(d.hours_raw)}</span></div>` : ""}
      ${linksHtml(d)}
      <div class="po-conf conf-slot">${confHtml(d)}</div>
      <div class="vote-area"></div>
      <div class="corrections-area"></div>
      <div class="fieldedits-area"></div>
      <div class="signals-area"></div>
      <div class="photos-area"></div>
      <div class="po-foot">
        <button class="btn ghost po-fixbtn" type="button">Fix location</button>
        <button class="btn ghost po-editbtn" type="button">Edit details</button>
        <button class="btn ghost po-reportbtn" type="button">Report</button>
      </div>
      <div class="po-report"></div>
    </div>`;

  L.popup({ minWidth: 290, maxWidth: 340, autoPan: true, className: "po-popup" })
    .setLatLng(latlng)
    .setContent(div)
    .openOn(map);

  // Move focus into the card (Leaflet closes on Esc by default), and hand it back to whatever
  // opened the popover (a marker or a list row) when it closes — so keyboard users aren't dropped.
  try { div.focus({ preventScroll: true }); } catch (e) { /* older browsers ignore the option */ }
  map.once("popupclose", () => {
    try { if (opener && opener.focus) opener.focus({ preventScroll: true }); } catch (e) { /* gone */ }
  });

  mountVote(div.querySelector(".vote-area"), d.id, (u) => {
    const slot = div.querySelector(".conf-slot");
    if (slot) slot.innerHTML = confHtml(u);
  });
  mountCorrections(div.querySelector(".corrections-area"), d, latlng);
  mountFieldEdits(div.querySelector(".fieldedits-area"), d);
  mountSignals(div.querySelector(".signals-area"), d);
  mountPhotos(div.querySelector(".photos-area"), d.id, latlng);

  div.querySelector(".po-fixbtn").onclick = () => {
    map.closePopup();
    startCorrection(d);
  };
  div.querySelector(".po-editbtn").onclick = () => {
    map.closePopup();
    startFieldEdit(d);
  };
  div.querySelector(".po-reportbtn").onclick = () =>
    openLocationReport(div.querySelector(".po-report"), d.id);
}
