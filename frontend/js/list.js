import { ORG_TYPE_LABELS } from "./config.js";
import { bucketColor, esc } from "./confidence.js";
import { openPopover } from "./popover.js";

// Category filters map to org_type sets (also makes "places to resell" discoverable).
const FILTERS = {
  all: { label: "Everything", types: null },
  donate: { label: "Places to donate", types: "charity_store,thrift_store,donation_center,mutual_aid,church_drive" },
  resell: { label: "Places to resell 💵", types: "consignment" },
  bins: { label: "Drop bins", types: "drop_bin" },
};

let map = null;
let onFilterChange = null;
let current = "all";

export function getTypes() {
  return FILTERS[current].types;
}

export function initList(m, onChange) {
  map = m;
  onFilterChange = onChange;
  const toggle = document.getElementById("list-toggle");
  const panel = document.getElementById("list-panel");
  const filterSel = document.getElementById("list-filter");
  const closeBtn = document.getElementById("list-close");

  filterSel.innerHTML = Object.entries(FILTERS)
    .map(([k, v]) => `<option value="${k}">${v.label}</option>`)
    .join("");
  filterSel.value = current;
  filterSel.addEventListener("change", () => {
    current = filterSel.value;
    onFilterChange && onFilterChange();
  });

  const setOpen = (open) => {
    panel.classList.toggle("open", open);
    panel.setAttribute("aria-hidden", open ? "false" : "true");
    toggle.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) filterSel.focus();
    else toggle.focus();
  };
  toggle.addEventListener("click", () => setOpen(!panel.classList.contains("open")));
  closeBtn.addEventListener("click", () => setOpen(false));
  panel.addEventListener("keydown", (e) => { if (e.key === "Escape") setOpen(false); });
}

export function updateList(data) {
  const ul = document.getElementById("list-results");
  const count = document.getElementById("list-count");
  if (!ul) return;
  ul.innerHTML = "";
  if (!data || data.mode === "clusters") {
    count.textContent = "";
    ul.innerHTML = `<li class="list-empty">Zoom in to list individual locations.</li>`;
    return;
  }
  const feats = data.features || [];
  count.textContent = `${feats.length} in view`;
  if (!feats.length) {
    ul.innerHTML = `<li class="list-empty">No locations in this area.</li>`;
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
      `<span class="dot" style="background:${bucketColor(p.bucket)}"></span>` +
      `<span class="li-name">${esc(p.name)}</span>` +
      `<span class="li-type">${esc(ORG_TYPE_LABELS[p.org_type] || p.org_type)}</span>`;
    btn.addEventListener("click", () => {
      map.flyTo([lat, lon], Math.max(map.getZoom(), 15));
      openPopover(map, [lat, lon], p.id);
    });
    li.appendChild(btn);
    ul.appendChild(li);
  });
}
