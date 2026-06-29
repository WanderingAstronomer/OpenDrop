import { postSubmit, reverseGeocode } from "./api.js";
import { ORG_TYPE_LABELS, ORG_TYPES } from "./config.js";
import { currentPosition } from "./geo.js";
import { pinDragLatLng, snapPinTo, startPinDrag, stopPinDrag } from "./pindrag.js";
import { app } from "./state.js";
import { toast } from "./toast.js";
import { guard } from "./turnstile.js";

let mode = "address"; // "address" | "pin"
let dropped = null; // {lat, lon} when a pin has been placed
let revTimer = null;

export function initSubmitPanel() {
  const panel = document.getElementById("submit-panel");
  const btn = document.getElementById("add-btn");
  btn.onclick = () => (panel.classList.contains("hidden") ? openPanel(panel) : closePanel(panel));
}

function optionsHtml() {
  return ORG_TYPES.map((t) => `<option value="${t}">${ORG_TYPE_LABELS[t]}</option>`).join("");
}

function focusables(panel) {
  return Array.from(panel.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), '
    + 'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )).filter((el) => el.offsetParent !== null);  // skip hidden controls
}

// The panel declares role=dialog + aria-modal, so keep Tab inside it (and Escape closes it).
function onKeydown(e) {
  const panel = document.getElementById("submit-panel");
  if (!panel || panel.classList.contains("hidden")) return;
  if (e.key === "Escape") { closePanel(panel); return; }
  if (e.key !== "Tab") return;
  const f = focusables(panel);
  if (!f.length) return;
  const first = f[0];
  const last = f[f.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
}

function openPanel(panel) {
  mode = "address";
  dropped = null;
  panel.innerHTML = `
    <h2 tabindex="-1">Add a donation location</h2>
    <div class="seg mode-seg" role="tablist" aria-label="How to locate it">
      <button type="button" class="on" data-mode="address" role="tab" aria-selected="true">✏️ By address</button>
      <button type="button" data-mode="pin" role="tab" aria-selected="false">📍 Drop a pin</button>
    </div>
    <label for="f-name">Name</label>
    <input id="f-name" autocomplete="organization" placeholder="e.g. St. Mark's clothing closet" />
    <label for="f-type">Type</label>
    <select id="f-type">${optionsHtml()}</select>
    <p class="pin-hint" hidden>Drag the pin to the exact spot — we'll fill in the address. Snap it to where you are, or use the search box to jump to an address.</p>
    <button type="button" class="btn ghost pin-snap" id="f-snap" hidden>Snap to my location</button>
    <div class="addr-block">
      <label for="f-line" class="addr-lead">Street address</label>
      <input id="f-line" autocomplete="address-line1" placeholder="123 Main St" />
      <div class="row">
        <div><label for="f-city">City</label><input id="f-city" autocomplete="address-level2" /></div>
        <div><label for="f-state">State</label><input id="f-state" maxlength="2" autocomplete="address-level1" placeholder="OH" /></div>
        <div><label for="f-zip">ZIP</label><input id="f-zip" inputmode="numeric" autocomplete="postal-code" pattern="\\d{5}(-\\d{4})?" /></div>
      </div>
    </div>
    <div class="ts submit-ts"></div>
    <div class="actions">
      <button class="btn primary" id="f-submit" type="button">Submit</button>
      <button class="btn ghost" id="f-cancel" type="button">Cancel</button>
    </div>
    <p class="consent-line">By submitting, you agree to the
      <a href="/terms.html" target="_blank" rel="noopener">Terms</a> and
      <a href="/privacy.html" target="_blank" rel="noopener">Privacy Notice</a>.
      Contributions become open data.</p>`;
  panel.classList.remove("hidden");
  panel.setAttribute("aria-hidden", "false");

  panel.querySelectorAll(".mode-seg button").forEach((b) => {
    b.onclick = () => setMode(panel, b.dataset.mode);
  });
  panel.querySelector("#f-cancel").onclick = () => closePanel(panel);
  panel.querySelector("#f-submit").onclick = (e) => doSubmit(panel, e.currentTarget);
  panel.querySelector("#f-snap").onclick = () => snapToGps(panel);
  document.addEventListener("keydown", onKeydown);
  panel.querySelector("#f-name").focus();
}

function setMode(panel, next) {
  if (next === mode) return;
  mode = next;
  panel.querySelectorAll(".mode-seg button").forEach((b) => {
    const on = b.dataset.mode === mode;
    b.classList.toggle("on", on);
    b.setAttribute("aria-selected", String(on));
  });
  const hint = panel.querySelector(".pin-hint");
  const lead = panel.querySelector(".addr-lead");
  const snap = panel.querySelector("#f-snap");
  if (mode === "pin") {
    hint.hidden = false;
    if (snap) snap.hidden = false;
    if (lead) lead.textContent = "Address (auto-filled — edit if needed)";
    const c = app.map.getCenter();
    dropped = { lat: c.lat, lon: c.lng };
    startPinDrag(app.map, c, { label: "Drag me to the spot", onMove: (ll) => onPinMove(panel, ll) });
    onPinMove(panel, c); // seed the address from the starting point
    toast("Drag the pin to the exact spot", "info");
  } else {
    hint.hidden = true;
    if (snap) snap.hidden = true;
    if (lead) lead.textContent = "Street address";
    dropped = null;
    stopPinDrag(app.map);
  }
}

// "Snap to my location": drop the pin on the user's GPS fix (they're standing at the bin). The pin
// stays draggable afterward, and snapping re-runs the reverse geocode to fill the address.
async function snapToGps(panel) {
  if (mode !== "pin") return;
  const snap = panel.querySelector("#f-snap");
  if (!navigator.geolocation) { toast("Location isn't available on this device", "error"); return; }
  const orig = snap.textContent;
  snap.disabled = true;
  snap.textContent = "Locating…";
  const ll = await currentPosition();
  snap.disabled = false;
  snap.textContent = orig;
  if (!ll) { toast("Couldn't get your location — drag the pin instead", "error"); return; }
  snapPinTo(app.map, ll);
  app.map.setView(ll, Math.max(app.map.getZoom(), 16));
  toast("Pin snapped to your location", "success");
}

function onPinMove(panel, ll) {
  dropped = { lat: ll.lat, lon: ll.lng };
  clearTimeout(revTimer);
  revTimer = setTimeout(async () => {
    const a = await reverseGeocode(ll.lat, ll.lng);
    if (!a || mode !== "pin") return;
    const set = (sel, v) => { const el = panel.querySelector(sel); if (el && v) el.value = v; };
    set("#f-line", a.line);
    set("#f-city", a.city);
    set("#f-state", a.state);
    set("#f-zip", a.postal_code);
  }, 550);
}

function closePanel(panel) {
  clearTimeout(revTimer);
  stopPinDrag(app.map);
  dropped = null;
  mode = "address";
  panel.classList.add("hidden");
  panel.setAttribute("aria-hidden", "true");
  panel.innerHTML = "";
  document.removeEventListener("keydown", onKeydown);
  const addBtn = document.getElementById("add-btn");
  if (addBtn) addBtn.focus();
}

function val(panel, sel) {
  const el = panel.querySelector(sel);
  const v = el ? el.value.trim() : "";
  return v || null;
}

async function doSubmit(panel, btn) {
  const name = (panel.querySelector("#f-name").value || "").trim();
  if (!name) { toast("Please enter a name", "error"); return; }
  if (mode === "pin" && !dropped) { toast("Drop a pin on the map first", "error"); return; }
  const state = val(panel, "#f-state");
  const payload = {
    name,
    org_type: panel.querySelector("#f-type").value,
    address: {
      line: val(panel, "#f-line"),
      city: val(panel, "#f-city"),
      state: state ? state.toUpperCase() : null,
      postal_code: val(panel, "#f-zip"),
    },
  };
  if (mode === "pin" && dropped) {
    const at = pinDragLatLng();
    payload.lat = at ? at.lat : dropped.lat;
    payload.lon = at ? at.lng : dropped.lon;
  }
  try {
    const d = await guard(panel.querySelector(".submit-ts"), btn, { action: "submit" },
      (token) => postSubmit({ ...payload, turnstile_token: token }));
    if (d.status === "duplicate") toast("That location looks like it already exists — thanks!", "info");
    else if (d.status === "promoted") toast("Added! It appears once the community confirms it", "success");
    else toast("Submitted for review — thank you!", "success");
    closePanel(panel);
    app.refresh();
  } catch (e) {
    if (e.status === 403) toast("Please complete the verification", "error");
    else if (e.status === 429) toast("Daily submission limit reached", "error");
    else if (e.status === 422) toast("Couldn't locate that address — saved for review", "info");
    else toast("Submission failed — please try again", "error");
  }
}
