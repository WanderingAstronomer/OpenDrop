// Photo-optional pin corrections — the creative "fix the location" mechanism.
//
// Instead of forcing a photo upload, the user drags the pin to the right spot and submits.
// What happens next depends on how invested the community already is in this location
// (the engagement tier the API hands us):
//   • Cold  — a fresh, barely-touched spot: the fix applies immediately, on good faith.
//   • Warm  — a few people involved: needs one confirmation, OR the submitter standing there.
//   • Hot   — a busy, well-known spot: needs several confirmations to move.
//
// GPS privacy contract (honoured verbatim): if the user says "I'm standing here", their
// device computes the distance to the dragged pin locally and we send ONLY a boolean. The
// coordinates never leave the browser and are never stored, correlated, or sold. GPS only
// ADDS consensus weight; it never gates a cold good-faith fix.

import { postCorrection, voteCorrection } from "./api.js";
import { gpsRadiusM, maxMoveM } from "./config.js";
import { currentPosition } from "./geo.js";
import { pinDragLatLng, snapPinTo, startPinDrag, stopPinDrag } from "./pindrag.js";
import { app } from "./state.js";
import { toast } from "./toast.js";
import { guard } from "./turnstile.js";

const EARTH_M = 6371000;

// haversine / gpsWithin / tierBlurb / confirmGpsCorroborated are exported so the JS unit suite
// (frontend/test/) can exercise the pure distance math and the privacy-critical GPS-gating logic
// directly — the gps_corroborated confirm-tap bug that shipped undetected lived in the last of these.
export function haversine(aLat, aLon, bLat, bLon) {
  const toRad = (d) => (d * Math.PI) / 180;
  const dLat = toRad(bLat - aLat);
  const dLon = toRad(bLon - aLon);
  const s = Math.sin(dLat / 2) ** 2 +
    Math.cos(toRad(aLat)) * Math.cos(toRad(bLat)) * Math.sin(dLon / 2) ** 2;
  return 2 * EARTH_M * Math.asin(Math.sqrt(s));
}

// Resolve to TRUE only if the device is within `radius` m of the target (the dragged pin).
// The coordinates are read and compared entirely on-device; the caller only ever sees a boolean.
export function gpsWithin(targetLat, targetLon, radius) {
  return new Promise((resolve) => {
    if (!navigator.geolocation) { resolve(false); return; }
    navigator.geolocation.getCurrentPosition(
      (pos) => resolve(haversine(pos.coords.latitude, pos.coords.longitude, targetLat, targetLon) <= radius),
      () => resolve(false),
      { enableHighAccuracy: true, timeout: 8000, maximumAge: 0 },
    );
  });
}

export function tierBlurb(tier, req) {
  if (tier === "cold") return "This spot is new to the map — your fix applies right away. Thank you!";
  if (tier === "hot") return `This is a busy, well-known spot — it takes ${req} confirmations to move the pin.`;
  return `Needs ${req} confirmation${req === 1 ? "" : "s"} before the pin moves — or just yours, if you're standing here.`;
}

let sheet = null;
let onKey = null;
let sheetOpener = null;  // element to restore focus to when the sheet closes

function teardown() {
  if (onKey) { document.removeEventListener("keydown", onKey); onKey = null; }
  stopPinDrag(app.map);
  if (sheet) { sheet.remove(); sheet = null; }
  const o = sheetOpener;
  sheetOpener = null;
  try { if (o && o.focus) o.focus({ preventScroll: true }); } catch (e) { /* opener gone */ }
}

// Begin a drag-to-fix session for an already-loaded location detail `d`.
export function startCorrection(d) {
  const opener = document.activeElement;  // capture before teardown (a no-op here) so it survives
  teardown();                             // to be restored when THIS sheet closes
  sheetOpener = opener;
  const radius = gpsRadiusM();
  const start = L.latLng(d.lat, d.lon);
  startPinDrag(app.map, start, { label: "Drag me to the right spot" });
  // Nudge the view so the pin + sheet are both comfortably visible.
  app.map.panTo(start, { animate: true });

  sheet = document.createElement("div");
  sheet.className = "pin-sheet";
  sheet.setAttribute("role", "dialog");
  sheet.setAttribute("aria-modal", "true");
  sheet.setAttribute("aria-label", "Fix this location");
  sheet.innerHTML = `
    <div class="sheet-card">
      <div class="sheet-head">
        <strong>Fix this location</strong>
        <button class="sheet-x" type="button" aria-label="Cancel">✕</button>
      </div>
      <p class="sheet-sub">Drag the pin (or tap the map) to the correct spot — or snap it to where you are.</p>
      <button class="btn ghost corr-snap" type="button">Snap to my location</button>
      <p class="sheet-tier">${tierBlurb(d.tier, d.required_support)}</p>
      <label class="sheet-note-l" for="corr-note">Note <span class="opt">(optional)</span></label>
      <input id="corr-note" class="sheet-note" maxlength="500" placeholder="e.g. moved to the far corner of the lot" />
      <label class="sheet-gps">
        <input type="checkbox" class="corr-gps" />
        <span>📍 I'm standing here right now <span class="opt">(adds weight)</span></span>
      </label>
      <p class="sheet-priv">Checked on your device — your coordinates are never stored, correlated, or sold. Only a yes/no is sent.</p>
      <div class="ts sheet-ts"></div>
      <div class="sheet-actions">
        <button class="btn primary corr-submit" type="button">Submit fix</button>
        <button class="btn ghost corr-cancel" type="button">Cancel</button>
      </div>
    </div>`;
  document.body.appendChild(sheet);

  const submitBtn = sheet.querySelector(".corr-submit");
  const tsHost = sheet.querySelector(".sheet-ts");
  sheet.querySelector(".sheet-x").onclick = teardown;
  sheet.querySelector(".corr-cancel").onclick = teardown;
  sheet.querySelector(".corr-snap").onclick = async (e) => {
    const b = e.currentTarget;
    const orig = b.textContent;
    b.disabled = true;
    b.textContent = "Locating…";
    const ll = await currentPosition();
    b.disabled = false;
    b.textContent = orig;
    if (!ll) { toast("Couldn't get your location — drag the pin instead", "error"); return; }
    snapPinTo(app.map, ll);
    app.map.setView(ll, Math.max(app.map.getZoom(), 16));
  };
  onKey = (e) => { if (e.key === "Escape") teardown(); };
  document.addEventListener("keydown", onKey);
  // Move focus into the sheet (the close button is the least intrusive target — focusing the note
  // field would raise the on-screen keyboard on mobile).
  try { sheet.querySelector(".sheet-x").focus({ preventScroll: true }); } catch (e) { /* ignore */ }

  submitBtn.onclick = async () => {
    const at = pinDragLatLng() || start;
    if (haversine(d.lat, d.lon, at.lat, at.lng) > maxMoveM()) {
      toast(`That's more than ${Math.round(maxMoveM() / 1000)} km away — add a new location instead`, "error");
      return;
    }
    const note = sheet.querySelector("#corr-note").value.trim();
    let gps = false;
    if (sheet.querySelector(".corr-gps").checked) {
      submitBtn.textContent = "Checking your location…";
      gps = await gpsWithin(at.lat, at.lng, radius);
      submitBtn.textContent = "Submit fix";
      if (!gps) toast("Couldn't confirm you're here — submitting without the location boost", "info");
    }
    try {
      const res = await guard(tsHost, submitBtn, { action: "correct" }, (token) =>
        postCorrection(d.id, {
          suggested_lat: at.lat, suggested_lon: at.lng,
          note: note || null, gps_corroborated: gps, turnstile_token: token,
        }));
      if (res.applied) {
        toast("Pin updated — thank you!", "success");
        app.refresh();
      } else {
        const left = Math.max(0, res.required_support - res.support);
        toast(left > 0
          ? `Saved — needs ${left} more confirmation${left === 1 ? "" : "s"} to move the pin`
          : "Saved — awaiting review", "success");
      }
      teardown();
    } catch (e) {
      if (e.status === 422 && e.error && e.error.code === "move_too_far") {
        toast("That's too far for a correction — add a new location instead", "error");
      } else if (e.status === 429) {
        toast("You've hit today's correction limit — try again tomorrow", "error");
      } else if (e.status === 403) {
        toast("Please complete the verification", "error");
      } else if (e.status === 404) {
        toast("That location is no longer available", "error");
        teardown();
      } else {
        toast("Couldn't submit the fix — please try again", "error");
      }
    }
  };
}

// A CONFIRM vote can carry the same on-site GPS boost the server already weights for confirmers
// (1 + gps_corroborated). We corroborate ONLY when geolocation is already granted — a quick
// "Looks right" tap must never trigger a permission prompt — and against the SUGGESTED point the
// voter is endorsing. As everywhere, the device computes the distance and we send only the boolean.
export async function confirmGpsCorroborated(suggestedLat, suggestedLon) {
  if (suggestedLat == null || suggestedLon == null || Number.isNaN(suggestedLat) || Number.isNaN(suggestedLon)) {
    return false;
  }
  try {
    if (!navigator.permissions) return false;
    const st = await navigator.permissions.query({ name: "geolocation" });
    if (st.state !== "granted") return false;  // never prompt on a confirm tap; quietly no boost
  } catch (e) {
    return false;
  }
  return gpsWithin(suggestedLat, suggestedLon, gpsRadiusM());
}

// Confirm or reject an existing open correction (used by the popover's pending-fix list).
export async function submitCorrectionVote({ corrId, confirm, suggestedLat, suggestedLon, host, btn }) {
  const gps = confirm ? await confirmGpsCorroborated(suggestedLat, suggestedLon) : false;
  return guard(host, btn, { action: "confirm_correction" }, (token) =>
    voteCorrection(corrId, { confirm, gps_corroborated: gps, turnstile_token: token }));
}
