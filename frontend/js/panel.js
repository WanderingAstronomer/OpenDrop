// Place-details panel — the app's single location-details surface.
//
// Replaces the old Leaflet popup card (which had grown taller than a phone screen). Research-
// grounded shape: a NON-MODAL overlay panel — desktop: 408px left dock with an all-or-nothing
// collapse tab; mobile: a two-snap bottom sheet (peek/full, no third snap, no gesture physics) —
// that scrolls internally while the map stays pannable behind it. The map div is never resized
// (no invalidateSize choreography); instead the camera pans by a pixel offset so the selected
// marker stays centered in the UNCOVERED map area. Content blocks are the same host-mounted
// modules the popup used (mountVote/mountSignals/mountPhotos/mountFieldEdits) — a host swap,
// not a rewrite.
import { fetchDetail, reportLocation } from "./api.js";
import { mountSignals } from "./attributes.js";
import { bucketCssColor, bucketLabel, esc, orgTypeLabel, supportLine } from "./confidence.js";
import * as corrections from "./corrections.js";
import * as fieldedit from "./fieldedit.js";
import { icon } from "./icons.js";
import { mountPhotos } from "./photos.js";
import { app } from "./state.js";
import { toast } from "./toast.js";
import { guard, verifyFailMessage } from "./turnstile.js";
import { mountVote } from "./vote.js";
import { prefersReducedMotion } from "./viewport.js";

const DESKTOP_MQ = "(min-width: 768px)";
const PANEL_W = 408;   // Google's own full-detail cap (~408px on maps.google.com)
const PEEK_FRACTION = 0.30;

let map = null;
let panelEl = null, bodyEl = null, titleEl = null, subEl = null, addrEl = null, dirEl = null, grabEl = null, tabEl = null;
let currentId = null;
let opener = null;
let onCloseCbs = [];
let histOwned = false;   // we pushed a history entry for the open panel
let closingViaPop = false;

const isDesktop = () => window.matchMedia(DESKTOP_MQ).matches;

// One-shot hooks flushed when the panel closes (ghost-marker cleanup, marker ring, focus…).
export function panelOnceClose(fn) {
  onCloseCbs.push(fn);
}

function flushCloseHooks() {
  const cbs = onCloseCbs;
  onCloseCbs = [];
  cbs.forEach((fn) => { try { fn(); } catch (e) { /* hook died — never block close */ } });
}

// ---- small local geometry (kept local so the panel has no scraper/sheet deps) -----------------
function distM(lat1, lon1, lat2, lon2) {
  const r = 6371008.8, toR = Math.PI / 180;
  const dLat = (lat2 - lat1) * toR, dLon = (lon2 - lon1) * toR;
  const a = Math.sin(dLat / 2) ** 2 +
    Math.cos(lat1 * toR) * Math.cos(lat2 * toR) * Math.sin(dLon / 2) ** 2;
  return 2 * r * Math.asin(Math.sqrt(a));
}

// ---- content builders (ported from the retired popup) -----------------------------------------
function addrText(a) {
  if (!a) return "";
  const parts = [a.line, [a.city, a.state].filter(Boolean).join(", "), a.postal_code].filter(Boolean);
  return parts.join(" · "); // set via textContent (auto-escaped), so no esc() here
}

// A community-supplied website (from world-editable OSM tags) is only safe to emit as a link when
// it resolves to http(s): esc() blocks HTML-injection, but a `javascript:`/`data:` scheme contains
// no escapable characters and would execute on click (there is no CSP backstop). Parse, allow only
// http/https, and prepend https:// for the common bare-domain tag ("example.org"). Returns null for
// anything else, so a hostile scheme is simply dropped rather than rendered.
function safeHttpUrl(raw) {
  const s = String(raw).trim();
  const withScheme = /^[a-z][a-z0-9+.-]*:/i.test(s) ? s : `https://${s}`;
  try {
    const u = new URL(withScheme);
    return (u.protocol === "http:" || u.protocol === "https:") ? u.href : null;
  } catch (e) {
    return null;
  }
}

function linksHtml(d) {
  const out = [];
  const site = d.website ? safeHttpUrl(d.website) : null;
  if (site) out.push(`<a href="${esc(site)}" target="_blank" rel="noopener noreferrer nofollow">Website ${icon.external(12)}</a>`);
  if (d.phone) out.push(`<a href="tel:${esc(String(d.phone).replace(/[^\d+]/g, ""))}">Call</a>`);
  return out.length ? `<div class="po-links">${out.join("")}</div>` : "";
}

// Plain-English status; the raw confidence score is an internal signal and never shown.
// Centered block: a glowing status dot + label on one row, then the confirmed/gone tally below.
// The dot color is driven off the confidence bucket via a --dot custom property so its soft ring
// (color-mix) tracks the same hue in either theme.
function confHtml(d) {
  return `<div class="conf-head">
      <span class="conf-dot" style="--dot:${bucketCssColor(d.bucket)}"></span>
      <span class="conf-label">${bucketLabel(d.bucket)}</span>
    </div>
    <div class="conf-tally">${d.upvotes} confirmed · ${d.denies} said gone</div>`;
}

function mountCorrections(host, d) {
  const open = d.open_corrections || [];
  if (!open.length) { host.innerHTML = ""; return; }
  host.innerHTML = `<div class="po-fixes">
    <h3 class="po-fixes-t">Pin move suggested</h3>
    ${open.map((c) => {
      const m = distM(d.lat, d.lon, c.suggested_lat, c.suggested_lon);
      const ft = Math.round(m * 3.28084);
      const midLatRad = ((d.lat + c.suggested_lat) / 2) * Math.PI / 180;
      const brg = (Math.atan2((c.suggested_lon - d.lon) * Math.cos(midLatRad), c.suggested_lat - d.lat) * 180 / Math.PI + 360) % 360;
      const dir = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][Math.round(brg / 45) % 8];
      const pct = Math.min(100, Math.round((c.support / c.required_support) * 100));
      return `<div class="po-fix" data-cid="${c.id}" data-slat="${c.suggested_lat}" data-slon="${c.suggested_lon}">
        ${c.note ? `<div class="fix-note">"${esc(c.note)}"</div>` : ""}
        <button class="btn tiny ghost fix-view" type="button">Show proposed spot · ${ft} ft ${dir}</button>
        <div class="fix-meter">
          <div class="fix-bar" role="img" aria-label="${c.support} of ${c.required_support} confirmations"><span style="width:${pct}%"></span></div>
          ${supportLine(c.support, c.required_support)}
        </div>
        <div class="ts fix-ts"></div>
        <div class="fix-btns">
          <button class="btn tiny confirm" type="button">Looks right</button>
          <button class="btn tiny deny" type="button">Not right</button>
        </div>
        <p class="fix-priv">If location access is on, your phone checks whether you're nearby — only a yes or no ever leaves your device.</p>
      </div>`;
    }).join("")}
  </div>`;

  host.querySelectorAll(".po-fix").forEach((row) => {
    const cid = Number(row.dataset.cid);
    const sLat = Number(row.dataset.slat);
    const sLon = Number(row.dataset.slon);
    const tsHost = row.querySelector(".fix-ts");

    // Voters shouldn't endorse coordinates they never saw: draw the proposed spot + a dashed
    // line from the current pin, cleaned up when the panel closes.
    row.querySelector(".fix-view").onclick = () => {
      const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#2b6cb0";
      const ghost = L.circleMarker([sLat, sLon], { radius: 8, color: accent, fillOpacity: 0.4 }).addTo(map);
      const line = L.polyline([[d.lat, d.lon], [sLat, sLon]], { color: accent, dashArray: "4 6", weight: 2 }).addTo(map);
      panelOnceClose(() => { ghost.remove(); line.remove(); });
    };

    const submitVote = corrections.submitCorrectionVote;
    const confirmBtn = row.querySelector(".confirm");
    const denyBtn = row.querySelector(".deny");
    const onVote = async (confirm, btn, otherBtn) => {
      // The confirm path awaits GPS corroboration (permissions + a geolocation fix) BEFORE guard()
      // runs, so guard's own disable is too late to stop a double-tap — latch btn ourselves up front
      // and re-entrancy-guard it (mirrors the correction-submit handler).
      if (btn.disabled) return;
      btn.disabled = true;
      if (otherBtn) otherBtn.disabled = true;
      try {
        const res = await submitVote({ corrId: cid, confirm, suggestedLat: sLat, suggestedLon: sLon, host: tsHost, btn });
        if (res.applied) {
          toast("Pin updated — thank you!", "success");
          app.refresh();
          closePlacePanel();
        } else if (res.status === "rejected") {
          toast("Vote recorded", "info");
          row.remove();
        } else {
          const pct = Math.min(100, Math.round((res.support / res.required_support) * 100));
          row.querySelector(".fix-bar span").style.width = `${pct}%`;
          row.querySelector(".fix-meter").lastChild.textContent = supportLine(res.support, res.required_support);
          btn.disabled = false; if (otherBtn) otherBtn.disabled = false; // meter-update keeps the row — re-arm both
          toast("Vote recorded — thank you", "success");
        }
      } catch (e) {
        btn.disabled = false; if (otherBtn) otherBtn.disabled = false; // re-arm both for retry
        if (e.status === 409 && e.error?.code === "self_vote") toast("You can't vote on your own suggestion", "info");
        else if (e.status === 409) toast("That suggestion is already closed", "info");
        else if (e.status === 403) toast(verifyFailMessage(), "error");
        else toast("Couldn't save your vote — try again", "error");
      }
    };
    confirmBtn.onclick = () => onVote(true, confirmBtn, denyBtn);
    denyBtn.onclick = () => onVote(false, denyBtn, confirmBtn);
  });
}

// Inline "report this location" form under the footer. A report only files a complaint for
// moderator review — it never hides a listing on its own.
function openLocationReport(container, locId, reportBtn) {
  const setExpanded = (v) => reportBtn && reportBtn.setAttribute("aria-expanded", String(v));
  if (container.dataset.open) { container.requestClose(); return; }
  container.dataset.open = "1";
  setExpanded(true);
  container.innerHTML = `
    <div class="po-report-hint">Flag this listing for a moderator — wrong info, offensive content, or it doesn't exist. Reports never remove a spot on their own.</div>
    <label class="rep-label" for="rep-reason">Why are you reporting this? <span class="opt">(optional)</span></label>
    <div class="rep-chips" role="group" aria-label="Reason">
      <button type="button" class="chip" data-cat="gone" aria-pressed="false">It's gone</button>
      <button type="button" class="chip" data-cat="wrong" aria-pressed="false">Wrong info</button>
      <button type="button" class="chip" data-cat="abuse" aria-pressed="false">Spam or offensive</button>
    </div>
    <textarea id="rep-reason" class="po-report-reason" rows="3" maxlength="500" placeholder="Anything that helps a moderator check it" aria-label="Reason for report"></textarea>
    <div class="ts po-report-ts"></div>
    <div class="po-report-actions">
      <button class="btn tiny primary po-report-send" type="button">Send to moderators</button>
      <button class="btn tiny ghost po-report-cancel" type="button">Cancel</button>
    </div>`;
  const tsHost = container.querySelector(".po-report-ts");
  const reasonEl = container.querySelector(".po-report-reason");
  const cancelBtn = container.querySelector(".po-report-cancel");
  let cat = null;
  container.querySelectorAll(".chip").forEach((ch) => {
    ch.onclick = () => {
      const on = ch.getAttribute("aria-pressed") !== "true";
      container.querySelectorAll(".chip").forEach((c) => c.setAttribute("aria-pressed", "false"));
      ch.setAttribute("aria-pressed", String(on));
      cat = on ? ch.dataset.cat : null;
    };
  });

  const close = () => { container.innerHTML = ""; delete container.dataset.open; setExpanded(false); };
  // Two-step discard: a non-empty reason converts Cancel to a visible confirm for 4s.
  let armed = null;
  container.requestClose = () => {
    if (!container.dataset.open) return;
    if (!reasonEl || !reasonEl.value.trim() || armed) { clearTimeout(armed); close(); return; }
    cancelBtn.textContent = "Discard report?";
    cancelBtn.classList.add("danger");
    armed = setTimeout(() => {
      armed = null;
      cancelBtn.textContent = "Cancel";
      cancelBtn.classList.remove("danger");
    }, 4000);
  };
  cancelBtn.onclick = container.requestClose;

  container.querySelector(".po-report-send").onclick = async (e) => {
    const text = reasonEl.value.trim();
    const reason = cat ? `[${cat}] ${text}`.trim() : text; // category rides as a prefix until the API grows a field
    try {
      await guard(tsHost, e.currentTarget, { action: "report" },
        (token) => reportLocation(locId, { reason: reason || null, turnstile_token: token }));
      toast("Report sent — a moderator will take a look", "success");
      clearTimeout(armed);
      close();
    } catch (err) {
      if (err.status === 403) toast(verifyFailMessage(), "error");
      else if (err.status === 429) toast("That's today's limit for reports — try again tomorrow", "info");
      else if (err.status === 422) toast(err.error?.message || "Couldn't send that report — reword it and try again", "error");
      else if (err.status === 404) toast("This spot was removed while you had it open.", "error");
      else toast("Couldn't send the report — try again", "error");
    }
  };
}

// ---- panel mechanics ---------------------------------------------------------------------------

function setHidden(hidden) {
  panelEl.classList.toggle("open", !hidden);
  panelEl.setAttribute("aria-hidden", String(hidden));
}

function setSheetState(state) {
  // mobile only: "peek" | "full"
  panelEl.classList.toggle("full", state === "full");
  panelEl.classList.toggle("peek", state === "peek");
  grabEl.setAttribute("aria-expanded", String(state === "full"));
}

function setCollapsed(collapsed) {
  // desktop only: all-or-nothing collapse behind the edge tab (selection retained). Right-docked,
  // so the chevron points RIGHT (›) to push the panel off the right edge; the .is-collapsed class
  // rotates that single glyph 180° (→ ‹) in sync with the slide, instead of an instant text swap.
  panelEl.classList.toggle("collapsed", collapsed);
  if (!tabEl.querySelector(".pp-chev")) tabEl.innerHTML = '<span class="pp-chev">›</span>';
  tabEl.classList.toggle("is-collapsed", collapsed);
  tabEl.setAttribute("aria-expanded", String(!collapsed));
  const label = collapsed ? "Reopen place details" : "Collapse place details";
  tabEl.setAttribute("aria-label", label);
  tabEl.title = label; // native hover tooltip (both states)
}

// Keep the selected marker centered in the UNCOVERED map area (the map div never resizes).
function offsetPan(latlng) {
  if (!latlng) return;
  const p = map.latLngToContainerPoint(latlng);
  const size = map.getSize();
  let tx, ty;
  if (isDesktop()) {
    // Right dock: center the marker in the map area to the LEFT of the panel. If the list drawer
    // is open on the left, bias the center rightward so the pin clears it too.
    const pw = Math.min(PANEL_W, size.x - 56);
    const listEl = document.getElementById("list-panel");
    const lw = listEl && listEl.classList.contains("open") ? listEl.getBoundingClientRect().width : 0;
    tx = lw + (size.x - pw - lw) / 2;
    ty = size.y / 2;
  } else {
    tx = size.x / 2;
    ty = (size.y * (1 - PEEK_FRACTION)) / 2;
  }
  map.panBy([p.x - tx, p.y - ty], { animate: !prefersReducedMotion() });
}

export function closePlacePanel() {
  if (!currentId) return;
  currentId = null;
  flushCloseHooks();
  setHidden(true);
  setCollapsed(false);
  tabEl.hidden = true;
  // Pop our history entry unless the browser Back button is what closed us.
  if (histOwned && !closingViaPop) {
    histOwned = false;
    try { history.back(); } catch (e) { /* history exhausted */ }
  }
  histOwned = false;
  try { if (opener && opener.focus) opener.focus({ preventScroll: true }); } catch (e) { /* gone */ }
  opener = null;
}

export async function openPlacePanel(latlng, id) {
  if (!panelEl) return; // initPlacePanel not run (defensive)
  opener = opener || document.activeElement;

  // History: first open pushes ONE entry (Back closes in one press); switching pins replaces it.
  if (!histOwned) {
    try { history.pushState({ odBin: id }, "", `#bin/${id}`); histOwned = true; } catch (e) { /* sandboxed */ }
  } else {
    try { history.replaceState({ odBin: id }, "", `#bin/${id}`); } catch (e) { /* ignore */ }
  }

  const switching = currentId !== null;
  currentId = id;
  if (switching) flushCloseHooks(); // ghost markers etc. from the previous place

  titleEl.textContent = "Loading location…";
  subEl.textContent = "";
  bodyEl.innerHTML = `<div class="po-skel" role="status">Loading location…</div>`;
  setHidden(false);
  setCollapsed(false);
  tabEl.hidden = !isDesktop();
  if (!isDesktop()) setSheetState(switching ? (panelEl.classList.contains("full") ? "full" : "peek") : "peek");
  offsetPan(latlng);

  let d;
  try {
    d = await fetchDetail(id);
  } catch (e) {
    if (currentId !== id) return; // user moved on
    if (e.status === 404) {
      // A merged bin 404s with the survivor's id — follow it instead of stranding the user.
      const canonicalId = e.error?.details?.canonical_id;
      if (e.error?.code === "merged" && canonicalId != null && canonicalId !== id) {
        openPlacePanel(latlng, canonicalId);
        return;
      }
      bodyEl.innerHTML = `<div class="po-skel">This location was removed or merged with another.</div>`;
      return; // a 404 can never succeed on retry — no "Try again"
    }
    bodyEl.innerHTML = `<div class="po-skel">Couldn't load this location.</div>
      <button class="btn ghost po-retry" type="button">Try again</button>`;
    bodyEl.querySelector(".po-retry").onclick = () => openPlacePanel(latlng, id);
    return;
  }
  if (currentId !== id) return; // a different place was opened while we fetched

  panelEl.setAttribute("aria-label", d.name);
  titleEl.textContent = d.name;
  // Identity block: the blue chip is the Directions link (href set here); the address + category
  // lines sit beside it. Chip glyph + structure are static in index.html (pp-identity).
  const at = addrText(d.address);
  addrEl.textContent = at || "Get directions";
  if (dirEl) dirEl.href = `https://www.google.com/maps/dir/?api=1&destination=${d.lat},${d.lon}`;
  subEl.textContent = `${orgTypeLabel(d.org_type)}${d.org_name ? ` · ${d.org_name}` : ""}`;
  if (!latlng && d.lat != null) offsetPan(L.latLng(d.lat, d.lon));

  // Owner's T-sketch: photos full-width on top; essentials band; then details|community columns
  // when the panel is wide enough (container query — zero JS); footer carries the crowd tools.
  // Section order mirrors the redesign: photo → (optional hours/links) → status+presence →
  // community-proposed changes → community signals → footer. Full-width hairline dividers sit
  // ABOVE each section via CSS (empty sections collapse, taking their divider with them).
  bodyEl.innerHTML = `
    <div class="photos-area pp-photos"></div>
    ${d.hours_raw || linksHtml(d) ? `<div class="po-meta">
      ${d.hours_raw ? `<div class="po-row"><span class="po-ic">${icon.clock()}</span><span>${esc(d.hours_raw)}</span></div>` : ""}
      ${linksHtml(d)}
    </div>` : ""}
    <div class="pp-status">
      <div class="conf-slot">${confHtml(d)}</div>
      <div class="vote-area"></div>
    </div>
    <div class="pp-changes">
      <div class="fieldedits-area"></div>
      <div class="corrections-area"></div>
    </div>
    <div class="signals-area"></div>
    <div class="po-foot-wrap">
      <h3 class="po-foot-h">Something wrong?</h3>
      <div class="po-foot">
        <button class="btn quiet tiny po-fixbtn" type="button">Move pin</button>
        <button class="btn quiet tiny po-editbtn" type="button">Edit info</button>
        <button class="btn quiet danger tiny po-reportbtn" type="button" aria-expanded="false">Report</button>
      </div>
    </div>
    <div class="po-report"></div>`;

  const q = (sel) => bodyEl.querySelector(sel);
  const mountedId = d.id;
  mountVote(q(".vote-area"), d.id, (u) => {
    if (currentId !== mountedId) return; // vote resolved after a switch — don't paint A's tallies into B
    const slot = q(".conf-slot");
    if (slot) slot.innerHTML = confHtml(u);
  });
  mountCorrections(q(".corrections-area"), d);
  fieldedit.mountFieldEdits(q(".fieldedits-area"), d);
  mountSignals(q(".signals-area"), d);
  mountPhotos(q(".photos-area"), d.id, latlng || (d.lat != null ? L.latLng(d.lat, d.lon) : null));

  // Mutual exclusion: opening one sheet closes the other (optional-call — a missing export can
  // never break the panel).
  q(".po-fixbtn").onclick = () => {
    fieldedit.closeFieldEditSheet?.();
    corrections.startCorrection(d);
  };
  q(".po-editbtn").onclick = () => {
    corrections.closeCorrectionSheet?.();
    fieldedit.startFieldEdit(d);
  };
  const reportBtn = q(".po-reportbtn");
  reportBtn.onclick = () => openLocationReport(q(".po-report"), d.id, reportBtn);

  // Keyboard/SR users land inside the panel on load.
  try { titleEl.focus({ preventScroll: true }); } catch (e) { /* ignore */ }
}

export function initPlacePanel(m) {
  map = m;
  panelEl = document.getElementById("place-panel");
  tabEl = document.getElementById("pp-tab");
  if (!panelEl || !tabEl) return;
  bodyEl = panelEl.querySelector(".pp-body");
  titleEl = panelEl.querySelector(".pp-title");
  subEl = panelEl.querySelector(".pp-sub");
  addrEl = panelEl.querySelector(".pp-addr");
  dirEl = panelEl.querySelector(".pp-dir-btn");
  grabEl = panelEl.querySelector(".pp-grab");

  app.closePanel = closePlacePanel;
  app.panelOnceClose = panelOnceClose;

  panelEl.querySelector(".pp-close").onclick = () => closePlacePanel();
  panelEl.addEventListener("keydown", (e) => { if (e.key === "Escape") closePlacePanel(); });

  // Desktop collapse: the edge tab toggles the panel away/back, selection retained.
  tabEl.onclick = () => setCollapsed(!panelEl.classList.contains("collapsed"));

  // Mobile: grabber toggles peek/full; drag with pointer events, two snaps, nearest on release.
  grabEl.hidden = isDesktop();
  window.matchMedia(DESKTOP_MQ).addEventListener?.("change", (e) => {
    grabEl.hidden = e.matches;
    // A panel open across a breakpoint crossing would otherwise be left stateless: on desktop the
    // mobile .peek/.full classes mean nothing, and on mobile the base rule translates the sheet
    // fully off-screen unless one of them is set (rotate a tablet with a place open → it vanishes).
    // Re-seat it into the layout the new mode expects.
    if (!currentId) return;
    if (e.matches) setCollapsed(false);                                       // entered desktop dock
    else setSheetState(panelEl.classList.contains("full") ? "full" : "peek"); // entered bottom sheet
  });
  grabEl.onclick = () => setSheetState(panelEl.classList.contains("full") ? "peek" : "full");

  let drag = null;
  const head = panelEl.querySelector(".pp-head");
  head.addEventListener("pointerdown", (e) => {
    if (isDesktop() || !currentId) return;
    drag = { y0: e.clientY, t0: panelEl.getBoundingClientRect().top, moved: false };
    head.setPointerCapture(e.pointerId);
    panelEl.classList.add("dragging");
  });
  head.addEventListener("pointermove", (e) => {
    if (!drag) return;
    const dy = e.clientY - drag.y0;
    if (Math.abs(dy) > 4) drag.moved = true;
    // translate within [full-top, peek-top]; CSS anchors the sheet at full height
    const vh = window.innerHeight;
    const fullTop = 48, peekTop = vh * (1 - PEEK_FRACTION);
    const target = Math.min(peekTop, Math.max(fullTop, drag.t0 + dy));
    panelEl.style.transform = `translateY(${target - fullTop}px)`;
  });
  // One release path for pointerup AND pointercancel (gesture takeover, rotation, an incoming call).
  // Without a pointercancel handler an interrupted drag stranded the sheet mid-screen with its
  // transition still suppressed by .dragging. Read the dragged position BEFORE clearing the inline
  // transform — clearing first snaps the box to its class position, so the old code always measured
  // "peek" and the drag-to-expand gesture could never reach full.
  const endDrag = () => {
    if (!drag) return;
    const vh = window.innerHeight;
    const draggedTop = panelEl.getBoundingClientRect().top;
    const moved = drag.moved;
    drag = null;
    panelEl.classList.remove("dragging");
    panelEl.style.transform = "";
    if (moved) {
      const mid = (48 + vh * (1 - PEEK_FRACTION)) / 2;
      setSheetState(draggedTop < mid ? "full" : "peek");
    }
  };
  head.addEventListener("pointerup", endDrag);
  head.addEventListener("pointercancel", endDrag);

  // Back button closes the panel (one press — switching pins uses replaceState).
  window.addEventListener("popstate", () => {
    if (currentId) {
      closingViaPop = true;
      histOwned = false;
      closePlacePanel();
      closingViaPop = false;
    }
  });
}
