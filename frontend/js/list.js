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

  filterSel.innerHTML = Object.entries(FILTERS)
    .map(([k, v]) => `<option value="${k}">${v.label}</option>`)
    .join("");
  filterSel.value = current;
  filterSel.addEventListener("change", () => {
    current = filterSel.value;
    onFilterChange && onFilterChange();
  });

  // The list is ALWAYS present in the redesign — a left rail on desktop (collapsible behind the
  // edge tab), the resting bottom sheet on mobile. The old open/close toggle button is gone.
  panel.classList.add("open");
  panel.setAttribute("aria-hidden", "false");

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
  listTab.addEventListener("click", () => setCollapsed(!panel.classList.contains("collapsed")));
  // (The edge tab is the ONLY collapse affordance — the old in-header ‹ duplicated it and leaked
  // onto mobile, where its display rule out-cascaded the `hidden` attribute.)
  panel.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && window.matchMedia("(min-width: 1024px)").matches) setCollapsed(true);
  });

  // Mobile: the shared 3-snap sheet, resting at peek. Only the grab handle drags (the header
  // holds the filter <select> — dragging from it would fight the control).
  sheetApi = makeSheet(panel, [grab], { content: results });
  const seat = (desktop) => {
    listTab.hidden = !desktop;
    grab.hidden = desktop;
    if (desktop) { sheetApi.disable(); }
    else { setCollapsed(false); sheetApi.enable(); }
  };
  const mq = window.matchMedia("(min-width: 1024px)");
  seat(mq.matches);
  mq.addEventListener?.("change", (e) => seat(e.matches));
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
