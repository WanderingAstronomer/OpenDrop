import { geosearch, postSubmit, reverseGeocode } from "./api.js";
import { ORG_TYPE_LABELS, ORG_TYPES } from "./config.js";
import { currentPosition } from "./geo.js";
import { icon } from "./icons.js";
import { pinDragLatLng, snapPinTo, startPinDrag, stopPinDrag } from "./pindrag.js";
import { makeSheet } from "./sheet.js";
import { app } from "./state.js";
import { toast } from "./toast.js";
import { guard, verifyFailMessage } from "./turnstile.js";
import { prefersReducedMotion } from "./viewport.js";

let mode = "address"; // "address" | "pin"
let dropped = null; // {lat, lon} when a pin has been placed
let revTimer = null;
let discardTimer = null; // pending revert of the armed "Discard entries?" state

// Mobile bottom-sheet plumbing (shared helper, mirrors panel.js/list.js). Desktop keeps the
// floating .panel card; only <1024px becomes a sheet so the map stays visible for Drop-a-pin.
let grab = null;      // static drag handle (.submit-grab) — survives the innerHTML wipe on close
let body = null;      // .submit-body scroll container the form is injected into
let sheetApi = null;  // 3-snap controller
let mq = null;        // desktop breakpoint (matches => desktop)

const ADDR_FIELDS = ["#f-line", "#f-city", "#f-state", "#f-zip"];
// Mobile: pin mode lifts the sheet to HALF (55dvh), leaving the top ~45% of the map visible/
// draggable — the freshly-dropped pin is panned into that band (mirrors panel.js SHEET_HALF).
const SHEET_HALF = 0.55;

export function initSubmitPanel() {
  const panel = document.getElementById("submit-panel");
  const btn = document.getElementById("add-btn");
  grab = panel.querySelector(".submit-grab");
  body = panel.querySelector(".submit-body");
  btn.onclick = () => (panel.classList.contains("hidden") ? openPanel(panel) : closePanel(panel));

  // Build the sheet ONCE. On desktop the helper is disabled and the .panel card CSS owns the box;
  // on mobile it drives the snap heights. A swipe below peek routes to the same two-step discard
  // the Cancel button uses, so a downward flick can never silently drop typed entries.
  sheetApi = makeSheet(panel, [grab], {
    content: body,
    onDismiss: () => {
      if (requestClose(panel)) return true; // closed (no entries / already armed) — closePanel ran
      sheetApi.setSnap("half");             // entries present: discard armed, keep the form reachable
      return true;                          // handled — don't let the helper settle to peek
    },
  });

  // Re-seat on breakpoint crossings so a form left open across a rotation isn't stranded.
  mq = window.matchMedia("(min-width: 1024px)");
  const onMqChange = (e) => seat(e.matches);
  if (mq.addEventListener) mq.addEventListener("change", onMqChange);
  else if (mq.addListener) mq.addListener(onMqChange); // Safari <14

  // Tap the grab to step snaps (guarded against the click a drag release fires), same as list/panel.
  grab.addEventListener("click", () => {
    if (panel.dataset.justDragged) return;
    sheetApi.setSnap(sheetApi.snap() === "half" ? "full" : "half");
  });

  seat(mq.matches);
}

// Desktop => the floating card (helper disabled, CSS owns the box). Mobile => the grab shows and,
// if the form is open, the sheet is enabled at a reachable snap.
function seat(desktop) {
  const panel = document.getElementById("submit-panel");
  if (!panel || !sheetApi) return;
  grab.hidden = desktop;
  if (desktop) {
    sheetApi.disable(); // clears every inline height the helper owned so the card renders as today
  } else if (!panel.classList.contains("hidden")) {
    sheetApi.enable();
    if (sheetApi.snap() === "peek") sheetApi.setSnap("half"); // keep the form usable after a rotation
  }
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
  body.innerHTML = `
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
  // Mobile: raise as a bottom sheet at half — the form is usable and the map peeks above it (pin
  // mode drops it lower). Desktop leaves the card alone (helper stays disabled).
  if (!mq.matches) { sheetApi.enable(); sheetApi.setSnap("half"); }

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

  // Type-aware default: a donation bin has no civic street address — its anchor is a coordinate.
  // Picking "Donation bin" before typing any address steers to Drop-a-pin; a user who's already
  // entered an address is left alone (some bins do sit at a known address).
  panel.querySelector("#f-type").addEventListener("change", (e) => suggestPinForType(panel, e.target.value));

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

// Bins are coordinate-anchored, not address-anchored: selecting "Donation bin" with an empty
// address flips to Drop-a-pin so the user places the exact spot instead of hunting for a street
// number that may not exist. Guarded so it never overwrites an address the user has begun typing.
function suggestPinForType(panel, type) {
  if (type !== "drop_bin" || mode !== "address") return;
  if (["#f-line", "#f-city", "#f-state"].some((s) => val(panel, s))) return;
  setMode(panel, "pin");
  toast("Donation bins usually have no street address — drop a pin on the exact spot.", "info");
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
    // Mobile: drop the sheet to half so the map is visible/draggable behind it.
    if (!mq.matches) sheetApi.setSnap("half");
    const c = app.map.getCenter();
    dropped = { lat: c.lat, lon: c.lng };
    startPinDrag(app.map, c, { label: "Drag to the exact spot", onMove: (ll) => onPinMove(panel, ll) });
    // Mobile: the map center sits UNDER the half sheet — lift the freshly-dropped pin into the
    // uncovered top band so it's visible and reachable (mirrors panel.js offsetPan).
    if (!mq.matches) {
      const size = app.map.getSize();
      const p = app.map.latLngToContainerPoint(c);
      const ty = (size.y * (1 - SHEET_HALF)) / 2;
      app.map.panBy([0, p.y - ty], { animate: !prefersReducedMotion() });
    }
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

// Two-step discard: with entries in the form, the first Escape/Cancel/swipe-down arms the Cancel
// button as a visible "Discard entries?" confirm (no window.confirm); the second activation within
// 4 s closes. Returns true when it actually closed, false when it only armed the confirm — the
// sheet's onDismiss uses this to keep the form up (settling back) instead of dropping typed data.
function requestClose(panel) {
  if (!anyEntry(panel)) { closePanel(panel); return true; }
  const cancel = panel.querySelector("#f-cancel");
  if (!cancel) { closePanel(panel); return true; }
  if (cancel.dataset.armed) { closePanel(panel); return true; }
  cancel.dataset.armed = "1";
  cancel.classList.add("danger");
  cancel.textContent = "Discard entries?";
  clearTimeout(discardTimer);
  discardTimer = setTimeout(() => {
    delete cancel.dataset.armed;
    cancel.classList.remove("danger");
    cancel.textContent = "Cancel";
  }, 4000);
  return false;
}

function closePanel(panel) {
  clearTimeout(revTimer);
  clearTimeout(discardTimer);
  stopPinDrag(app.map);
  dropped = null;
  mode = "address";
  if (sheetApi) sheetApi.disable(); // drop the inline sheet heights (no-op on desktop); the card/
  // shell CSS owns the box again. Idempotent — safe on both breakpoints.
  panel.classList.add("hidden");
  panel.setAttribute("aria-hidden", "true");
  body.innerHTML = ""; // clear only the form — the static grab + .submit-body survive for reopen
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
    // Honest status. A fresh crowd pin lands at confidence 20 (< the 25 'active' gate) so it stays
    // PENDING — but it now SHOWS on the map badged "unconfirmed", so say exactly that.
    if (d.geocoded === false) {
      // Coordinates-first: a geocode miss is NOT a dead-end. Keep everything the user typed, move
      // the map near the city they entered, and flip to Drop-a-pin so they place the exact spot —
      // the pin is authoritative on resubmit, no address lookup needed. Panel stays open.
      await fallbackToPin(panel);
      return;
    }
    if (d.status === "duplicate") {
      toast("Looks like that spot is already on the map — thanks for checking.", "info");
    } else if (d.status === "resurfaced" && d.now_active) {
      toast("Confirmed — that spot is on the map now. Thanks!", "success");
    } else {
      toast("Added — it's on the map now, marked unconfirmed until a neighbor confirms it's there.", "success");
    }
    closePanel(panel);
    app.refresh();
  } catch (e) {
    if (e.status === 403) toast(verifyFailMessage(), "error");
    else if (e.status === 429) toast("That's today's limit for new locations — try again tomorrow", "info");
    else if (e.status === 422) toast(e.error?.message || "We can't accept this as written — please revise the name or address and try again.", "error");
    else toast("Couldn't add this spot — try again", "error");
  }
}

// The address couldn't be pinpointed (rural spot, a bin with no civic address, a Nominatim gap).
// Rather than dead-end and throw away what the user typed, switch to Drop-a-pin WITHOUT closing:
// recenter the map near the city they entered so the seeded pin lands close, flip to pin mode
// (which drops a draggable pin at the map center and reverse-geocodes without clobbering their
// typed address), and tell them what to do. On resubmit the pin is authoritative — no lookup.
async function fallbackToPin(panel) {
  const city = val(panel, "#f-city");
  const state = val(panel, "#f-state");
  const q = [city, state].filter(Boolean).join(", ");
  if (q) {
    const hits = await geosearch(q);
    if (hits[0]) app.map.setView([hits[0].lat, hits[0].lon], 15, { animate: !prefersReducedMotion() });
  }
  if (mode !== "pin") setMode(panel, "pin"); // seeds a draggable pin at the (now city-centered) map
  if (!mq.matches && sheetApi) sheetApi.setSnap("half"); // keep the sheet reachable on mobile
  toast("We couldn't pinpoint that address — drag the pin to the exact spot, then add it again.", "info");
}
