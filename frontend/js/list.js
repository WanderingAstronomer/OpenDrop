import { ORG_TYPE_LABELS } from "./config.js";
import { bucketCssColor, esc } from "./confidence.js";
import { openPlacePanel } from "./panel.js";
import { makeSheet } from "./sheet.js";
import { prefersReducedMotion } from "./viewport.js";

// Category filters map to org_type sets (also makes "places to resell" discoverable).
const FILTERS = {
  all: { label: "Everything", types: null },
  donate: { label: "Places to donate", types: "charity_store,thrift_store,donation_center,mutual_aid,church_drive" },
  resell: { label: "Consignment & resale", types: "consignment" },
  bins: { label: "Drop bins", types: "drop_bin" },
};

let map = null;
let onFilterChange = null;
let current = "all";
let sheetApi = null; // mobile bottom-sheet controller — module-level so updateList can nudge it
let lastList = null;   // last data updateList received, for replay on summon (A8)
let placeOpen = false; // HOISTED from initList closure — listVisible() reads it
let mq = null;         // HOISTED — assigned in initList

// A8: don't rebuild the list DOM while its surface is hidden. Stash the data and replay when the
// surface becomes visible again. Hidden means: mobile sheet dismissed / a place panel covering it,
// OR a desktop rail collapsed behind its edge tab (.collapsed => visibility:hidden). Desktop with the
// rail expanded is always visible => zero behavior change there.
function listVisible() {
  const panel = document.getElementById("list-panel");
  if (!panel) return false;
  if (mq && mq.matches) return !panel.classList.contains("collapsed"); // desktop: hidden while collapsed
  return panel.classList.contains("open") && !placeOpen;              // mobile: summoned & not covered
}
function replayList() { if (lastList !== null) updateList(lastList); }

export function getTypes() {
  return FILTERS[current].types;
}

export function initList(m, onChange) {
  map = m;
  onFilterChange = onChange;
  const panel = document.getElementById("list-panel");
  const filterSel = document.getElementById("list-filter");
  const listTab = document.getElementById("list-tab");
  const grab = panel.querySelector(".list-grab");
  const results = document.getElementById("list-results");
  const pill = document.getElementById("list-pill");

  filterSel.innerHTML = Object.entries(FILTERS)
    .map(([k, v]) => `<option value="${k}">${v.label}</option>`)
    .join("");
  filterSel.value = current;
  filterSel.addEventListener("change", () => {
    current = filterSel.value;
    onFilterChange && onFilterChange();
  });

  // Two lives (the summonable-sheet model): desktop = an ALWAYS-present left rail (collapsible
  // behind the edge tab); mobile = NO sheet at rest — a clean map with a bottom-center "List"
  // pill that summons the sheet at half, and a below-peek drag dismisses it (the pill returns).
  mq = window.matchMedia("(min-width: 1024px)");
  // placeOpen (the place panel supersedes the sheet AND the pill while open) is now module-scope.

  const setOpen = (open) => {
    panel.classList.toggle("open", open);
    panel.setAttribute("aria-hidden", String(!open));
  };
  const updatePill = () => {
    if (!pill) return;
    pill.hidden = mq.matches || panel.classList.contains("open") || placeOpen;
  };

  // Desktop collapse behind the edge tab (chevron ‹ pushes the rail off the left edge; the
  // .is-collapsed class rotates the glyph in sync with the slide).
  const setCollapsed = (collapsed) => {
    panel.classList.toggle("collapsed", collapsed);
    if (!listTab.querySelector(".list-chev")) listTab.innerHTML = '<span class="list-chev">‹</span>';
    listTab.classList.toggle("is-collapsed", collapsed);
    listTab.setAttribute("aria-expanded", String(!collapsed));
    const label = collapsed ? "Expand list" : "Collapse list";
    listTab.setAttribute("aria-label", label);
    listTab.title = label;
  };
  listTab.addEventListener("click", () => {
    const collapse = !panel.classList.contains("collapsed");
    setCollapsed(collapse);
    if (!collapse) replayList(); // A8: expanding a collapsed rail paints the stash it skipped while hidden
  });
  // (The edge tab is the ONLY collapse affordance — the old in-header ‹ duplicated it and leaked
  // onto mobile, where its display rule out-cascaded the `hidden` attribute.)

  // Mobile: the shared 3-snap sheet, SUMMONED rather than resting. Only the grab handle drags
  // (the header holds the filter <select> — dragging from it would fight the control). Dismissal
  // has three paths, each single-pointer or keyboard (WCAG 2.5.7 needs a non-drag alternative):
  // a drag ending below peek (ONE swipe — unlike the place sheet's two-stage dismiss), a TAP on
  // the map outside the sheet, or Escape. Every dismissal hands focus to the returned pill.
  const dismiss = () => {
    setOpen(false);
    panel.style.height = "0px"; // the next summon slides up from the bottom edge
    updatePill();
    if (pill && !pill.hidden) { try { pill.focus({ preventScroll: true }); } catch (err) { /* ignore */ } }
  };
  sheetApi = makeSheet(panel, [grab], { content: results, onDismiss: () => { dismiss(); } });

  // Escape: desktop collapses the rail; mobile dismisses the summoned sheet.
  panel.addEventListener("keydown", (e) => {
    if (e.key !== "Escape") return;
    if (mq.matches) { setCollapsed(true); return; }
    dismiss();
  });

  // Tapping the map dismisses the summoned sheet (the map-app convention). Leaflet fires "click"
  // only for a tap, never a pan. Pin taps bubble here too — a MAP-initiated pin selection
  // dismissing the list is intended; row-initiated selections are DOM clicks and don't bubble,
  // so picking from the list keeps it for when the place closes.
  map.on("click", () => {
    if (!mq.matches && panel.classList.contains("open")) dismiss();
  });

  // Summoning moves focus INTO the sheet: the pill (the focused element) is about to hide, and
  // dropping focus to <body> would strand the panel-scoped Escape path above.
  const summon = () => {
    setOpen(true);
    sheetApi.setSnap("half");
    updatePill();
    replayList(); // A8: paint the freshest stash now that the sheet is visible
    try { filterSel.focus({ preventScroll: true }); } catch (err) { /* ignore */ }
  };
  if (pill) pill.addEventListener("click", summon);

  // The place panel announces open/close (see panel.js setHidden) — the pill yields while a
  // place is up and returns when it closes (unless the list sheet itself is still open under it).
  document.addEventListener("od:place-toggle", (e) => {
    placeOpen = !!(e.detail && e.detail.open);
    updatePill();
    if (!placeOpen) replayList(); // A8: place closed over an open list => repaint the freshest stash
  });

  const seat = (desktop) => {
    listTab.hidden = !desktop;
    grab.hidden = desktop;
    if (desktop) {
      sheetApi.disable(); // also clears the inline height the sheet owned
      setOpen(true);      // the rail is always present on desktop
      replayList();       // A8: newly-visible rail paints the accumulated stash immediately
    } else {
      setCollapsed(false);
      setOpen(false);     // mobile default: clean map, the pill summons
      sheetApi.enable();
      panel.style.height = "0px"; // start at the bottom edge (enable() applied a snap height)
    }
    updatePill();
  };
  seat(mq.matches);
  const onMqChange = (e) => seat(e.matches);
  if (mq.addEventListener) mq.addEventListener("change", onMqChange);
  else if (mq.addListener) mq.addListener(onMqChange);  // Safari <14
  // Tap the grabber to toggle half/peek (guarded against the click a drag release fires).
  grab.addEventListener("click", () => {
    if (panel.dataset.justDragged) return;
    sheetApi.setSnap(sheetApi.snap() === "peek" ? "half" : "peek");
  });
}

export function updateList(data) {
  const ul = document.getElementById("list-results");
  const count = document.getElementById("list-count");
  if (!ul) return;
  lastList = data;                 // A8: always stash the latest data
  if (!listVisible()) return;      // A8: hidden -> skip the DOM rebuild (replayed on re-show)
  ul.innerHTML = "";
  if (!data || data.mode === "clusters") {
    count.textContent = "";
    // Make the gated accessible path actionable for keyboard/SR users rather than a dead statement.
    ul.innerHTML = `<li class="list-empty">Too many locations to list here.<br>
      <button class="btn quiet list-zoomin" type="button">Zoom in to list them</button></li>`;
    const zi = ul.querySelector(".list-zoomin");
    if (zi) zi.onclick = () => map.setZoom(Math.min(map.getZoom() + 3, 16), { animate: !prefersReducedMotion() });
    return;
  }
  // Order nearest-first from the current map center so the list is stable between fetches (the
  // backend has no ORDER BY under its LIMIT) and the slice(0,300) below keeps the CLOSEST 300.
  const c = map && map.getCenter ? map.getCenter() : null;
  const feats = (data.features || []).slice(); // copy — never sort the caller's fetched array in place
  if (c) {
    const kx = Math.cos((c.lat * Math.PI) / 180);
    const d2 = (f) => {
      const [lon, lat] = f.geometry.coordinates;
      const dx = (lon - c.lng) * kx, dy = lat - c.lat;
      return dx * dx + dy * dy; // squared cos-scaled degrees — monotonic with true distance in-viewport
    };
    feats.sort((a, b) => d2(a) - d2(b));
  }
  count.textContent = `${feats.length} in view`;
  if (!feats.length) {
    // Out of US coverage entirely (main.js skipped the fetch): recruiting a submission here would
    // walk the user into out-of-coverage coordinates — just point home instead.
    if (data.outOfCoverage) {
      ul.innerHTML = `<li class="list-empty">Nothing here — OpenDrop lists US locations.</li>`;
      return;
    }
    // The empty state recruits: hand the user the Add flow instead of a dead end.
    ul.innerHTML = `<li class="list-empty">No locations in this area yet.<br>
      <button class="btn quiet list-add" type="button">Add one you know about</button></li>`;
    ul.querySelector(".list-add").onclick = () => {
      // Drop the mobile sheet to peek so the Add flow isn't buried under it.
      if (sheetApi && !window.matchMedia("(min-width: 1024px)").matches) sheetApi.setSnap("peek");
      document.getElementById("add-btn").click();
    };
    return;
  }
  feats.slice(0, 300).forEach((f) => {
    const [lon, lat] = f.geometry.coordinates;
    const p = f.properties;
    const li = document.createElement("li");
    const btn = document.createElement("button");
    btn.className = "list-item";
    btn.type = "button";
    btn.innerHTML =
      `<span class="dot" style="background:${bucketCssColor(p.bucket)}"></span>` +
      `<span class="li-name">${esc(p.name)}</span>` +
      `<span class="li-type">${esc(ORG_TYPE_LABELS[p.org_type] || p.org_type)}</span>`;
    btn.addEventListener("click", () => {
      // Mobile "mode swap": opening a place hides this list sheet via CSS (one sheet at a time);
      // closing the place brings it back at the same snap — nothing to do here.
      const targetZoom = Math.max(map.getZoom(), 15);
      const ll = L.latLng(lat, lon);
      if (map.getZoom() < targetZoom) {
        // Zoom in first, then let the panel's offsetPan own the final centering once the fly settles
        // — the two camera animations used to run concurrently and drop the pin under the right dock.
        map.flyTo(ll, targetZoom, { animate: !prefersReducedMotion() });
        map.once("moveend", () => openPlacePanel(ll, p.id));
      } else {
        openPlacePanel(ll, p.id); // already zoomed in — offsetPan alone re-centers for the dock
      }
    });
    li.appendChild(btn);
    ul.appendChild(li);
  });
}
