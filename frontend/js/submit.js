import { postSubmit, reverseGeocode } from "./api.js";
import { ORG_TYPE_LABELS, ORG_TYPES } from "./config.js";
import { currentPosition } from "./geo.js";
import { icon } from "./icons.js";
import { pinDragLatLng, snapPinTo, startPinDrag, stopPinDrag } from "./pindrag.js";
import { app } from "./state.js";
import { toast } from "./toast.js";
import { guard, verifyFailMessage } from "./turnstile.js";
import { prefersReducedMotion } from "./viewport.js";

let mode = "address"; // "address" | "pin"
let dropped = null; // {lat, lon} when a pin has been placed
let revTimer = null;
let discardTimer = null; // pending revert of the armed "Discard entries?" state

const ADDR_FIELDS = ["#f-line", "#f-city", "#f-state", "#f-zip"];

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

// The panel is a non-modal role=dialog. Tab is only wrapped in address mode: in pin mode the user
// MUST be able to Tab out to the map marker, or pin placement is keyboard-inaccessible.
function onKeydown(e) {
  const panel = document.getElementById("submit-panel");
  if (!panel || panel.classList.contains("hidden")) return;
  if (e.key === "Escape") { requestClose(panel); return; }
  if (e.key !== "Tab" || mode === "pin") return;
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
    <form id="f-form" novalidate>
      <div class="seg mode-seg" role="radiogroup" aria-label="How to locate it">
        <button type="button" class="on" data-mode="address" role="radio" aria-checked="true">${icon.lines()} Type the address</button>
        <button type="button" data-mode="pin" role="radio" aria-checked="false" tabindex="-1">${icon.pin()} Drop a pin</button>
      </div>
      <label for="f-name">Name</label>
      <input id="f-name" autocomplete="organization" placeholder="e.g. Planet Aid bin — Kroger parking lot" aria-describedby="f-name-err" />
      <p class="field-err" id="f-name-err" role="alert" hidden>Give this spot a name — e.g. “Planet Aid bin, Kroger lot”.</p>
      <label for="f-type">Type</label>
      <select id="f-type">${optionsHtml()}</select>
      <p class="pin-hint" hidden>Drag the pin to the exact spot — we'll fill in the address. Snap it to where you are, or use the search box to jump to an address.</p>
      <button type="button" class="btn ghost pin-snap" id="f-snap" hidden>Snap to my location</button>
      <p class="pin-readout" aria-live="polite"></p>
      <div class="addr-block">
        <label for="f-line" class="addr-lead">Street address</label>
        <input id="f-line" autocomplete="address-line1" placeholder="123 Main St" aria-describedby="f-addr-err" />
        <div class="row">
          <div><label for="f-city">City</label><input id="f-city" autocomplete="address-level2" placeholder="Springfield" aria-describedby="f-addr-err" /></div>
          <div><label for="f-state">State</label><input id="f-state" maxlength="2" autocomplete="address-level1" placeholder="CA" aria-describedby="f-addr-err" /></div>
          <div><label for="f-zip">ZIP</label><input id="f-zip" inputmode="numeric" autocomplete="postal-code" placeholder="90210" pattern="\\d{5}(-\\d{4})?" /></div>
        </div>
        <p class="field-err" id="f-addr-err" role="alert" hidden>Enter the street, city, and state — or switch to “Drop a pin” if you don't know the address.</p>
      </div>
      <div class="ts submit-ts"></div>
      <div class="actions">
        <button class="btn primary" id="f-submit" type="submit">Add location</button>
        <button class="btn ghost" id="f-cancel" type="button">Cancel</button>
      </div>
    </form>
    <p class="consent-line">By submitting, you agree to the
      <a href="/terms.html" target="_blank" rel="noopener">Terms</a> and
      <a href="/privacy.html" target="_blank" rel="noopener">Privacy Notice</a>.
      Contributions become open data.</p>`;
  panel.classList.remove("hidden");
  panel.setAttribute("aria-hidden", "false");

  const seg = panel.querySelector(".mode-seg");
  seg.querySelectorAll("button").forEach((b) => {
    b.onclick = () => setMode(panel, b.dataset.mode);
  });
  // Radiogroup contract: arrows move the selection (two options, so both arrows toggle).
  seg.addEventListener("keydown", (e) => {
    if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
    e.preventDefault();
    const next = mode === "address" ? "pin" : "address";
    setMode(panel, next);
    seg.querySelector(`button[data-mode="${next}"]`).focus();
  });

  panel.querySelector("#f-form").addEventListener("submit", (e) => {
    e.preventDefault();
    doSubmit(panel, panel.querySelector("#f-submit"));
  });
  panel.querySelector("#f-cancel").onclick = () => requestClose(panel);
  panel.querySelector("#f-snap").onclick = () => snapToGps(panel);

  // Hand-edited address fields are sacred: mark them dirty so the pin-drag geocoder never
  // overwrites a correction the user typed in themselves.
  ADDR_FIELDS.forEach((s) => {
    panel.querySelector(s).addEventListener("input", (e) => { e.target.dataset.dirty = "1"; });
  });

  // Typing in a flagged field withdraws its inline error immediately.
  panel.querySelector("#f-name").addEventListener("input", (e) => {
    e.target.removeAttribute("aria-invalid");
    panel.querySelector("#f-name-err").hidden = true;
  });
  ["#f-line", "#f-city", "#f-state"].forEach((s) => {
    panel.querySelector(s).addEventListener("input", (e) => {
      e.target.removeAttribute("aria-invalid");
      panel.querySelector("#f-addr-err").hidden = true;
    });
  });

  const stateEl = panel.querySelector("#f-state");
  stateEl.addEventListener("blur", () => { stateEl.value = stateEl.value.toUpperCase(); });

  document.addEventListener("keydown", onKeydown);
  panel.querySelector("#f-name").focus();
}

function setMode(panel, next) {
  if (next === mode) return;
  mode = next;
  panel.querySelectorAll(".mode-seg button").forEach((b) => {
    const on = b.dataset.mode === mode;
    b.classList.toggle("on", on);
    b.setAttribute("aria-checked", String(on));
    b.tabIndex = on ? 0 : -1; // roving tabindex: only the checked radio is a tab stop
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
    startPinDrag(app.map, c, { label: "Drag to the exact spot", onMove: (ll) => onPinMove(panel, ll) });
    onPinMove(panel, c); // seed the address from the starting point
  } else {
    hint.hidden = true;
    if (snap) snap.hidden = true;
    if (lead) lead.textContent = "Street address";
    dropped = null;
    stopPinDrag(app.map);
    const ro = panel.querySelector(".pin-readout");
    if (ro) ro.textContent = "";
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
  // An explicit "put me where I'm standing" outranks stale hand edits, so the fresh geocode may
  // refill everything. Plain drags never get this pass — they must respect dirty fields.
  ADDR_FIELDS.forEach((s) => { const el = panel.querySelector(s); if (el) delete el.dataset.dirty; });
  snapPinTo(app.map, ll);
  app.map.setView(ll, Math.max(app.map.getZoom(), 16), { animate: !prefersReducedMotion() });
  toast("Pin snapped to your location", "success");
}

function onPinMove(panel, ll) {
  dropped = { lat: ll.lat, lon: ll.lng };
  clearTimeout(revTimer);
  revTimer = setTimeout(async () => {
    const a = await reverseGeocode(ll.lat, ll.lng);
    if (mode !== "pin") return;
    const ro = panel.querySelector(".pin-readout");
    if (ro) ro.textContent = a ? `Pin near ${a.line}, ${a.city}` : "";
    if (!a) return;
    const set = (sel, v) => {
      const el = panel.querySelector(sel);
      if (el && v && !el.dataset.dirty) el.value = v; // never clobber a hand-edited field
    };
    set("#f-line", a.line);
    set("#f-city", a.city);
    set("#f-state", a.state);
    set("#f-zip", a.postal_code);
  }, 550);
}

// True when the user has typed anything worth protecting from an accidental Escape/Cancel.
function anyEntry(panel) {
  return ["#f-name", ...ADDR_FIELDS].some((s) => {
    const el = panel.querySelector(s);
    return el && el.value.trim();
  });
}

// Two-step discard: with entries in the form, the first Escape/Cancel arms the Cancel button as a
// visible "Discard entries?" confirm (no window.confirm); the second activation within 4 s closes.
function requestClose(panel) {
  if (!anyEntry(panel)) { closePanel(panel); return; }
  const cancel = panel.querySelector("#f-cancel");
  if (!cancel) { closePanel(panel); return; }
  if (cancel.dataset.armed) { closePanel(panel); return; }
  cancel.dataset.armed = "1";
  cancel.classList.add("danger");
  cancel.textContent = "Discard entries?";
  clearTimeout(discardTimer);
  discardTimer = setTimeout(() => {
    delete cancel.dataset.armed;
    cancel.classList.remove("danger");
    cancel.textContent = "Cancel";
  }, 4000);
}

function closePanel(panel) {
  clearTimeout(revTimer);
  clearTimeout(discardTimer);
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

// Flag a field inline: aria-invalid + its role=alert error line. Returns the field so the first
// failure can take focus.
function flag(panel, inputSel, errSel) {
  const el = panel.querySelector(inputSel);
  el.setAttribute("aria-invalid", "true");
  panel.querySelector(errSel).hidden = false;
  return el;
}

function validate(panel) {
  // Reset last round so errors never linger on fields the user already fixed.
  panel.querySelectorAll('[aria-invalid="true"]').forEach((el) => el.removeAttribute("aria-invalid"));
  panel.querySelectorAll(".field-err").forEach((el) => { el.hidden = true; });

  let firstBad = null;
  if (!val(panel, "#f-name")) firstBad = flag(panel, "#f-name", "#f-name-err");
  if (mode === "address") {
    // Pin mode only needs the pin; address mode needs enough of one to geocode.
    const missing = ["#f-line", "#f-city", "#f-state"].filter((s) => !val(panel, s));
    missing.forEach((s) => flag(panel, s, "#f-addr-err"));
    if (missing.length && !firstBad) firstBad = panel.querySelector(missing[0]);
  }
  if (firstBad) { firstBad.focus(); return false; }
  return true;
}

async function doSubmit(panel, btn) {
  if (!validate(panel)) return;
  if (mode === "pin" && !dropped) return; // defensive: setMode always seeds a pin
  const state = val(panel, "#f-state");
  const payload = {
    name: val(panel, "#f-name"),
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
    if (d.status === "duplicate") toast("Looks like that spot is already on the map — thanks for checking.", "info");
    else if (d.status === "promoted") toast("Added — your spot is live on the map.", "success");
    else toast("Got it. Your spot goes live after a quick community check.", "success");
    closePanel(panel);
    app.refresh();
  } catch (e) {
    if (e.status === 403) toast(verifyFailMessage(), "error");
    else if (e.status === 429) toast("That's today's limit for new locations — try again tomorrow", "info");
    else if (e.status === 422) toast(e.error?.message || "We can't accept this as written — please revise the name or address and try again.", "error");
    else toast("Couldn't add this spot — try again", "error");
  }
}
