import { API } from "./config.js";
import { pinDragLatLng, snapPinTo } from "./pindrag.js";

let map = null;
let timer = null;
let active = -1; // index of the highlighted option, -1 = none

export function initSearch(m) {
  map = m;
  const wrap = document.getElementById("search");
  const input = document.getElementById("search-input");
  const results = document.getElementById("search-results");
  if (!input || !results) return;

  // Combobox wiring (the <ul> is already role=listbox in the markup).
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-controls", "search-results");
  input.setAttribute("aria-expanded", "false");

  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => run(input.value, input, results), 350);
  });
  input.addEventListener("keydown", (e) => {
    const opts = Array.from(results.querySelectorAll("li[data-pick]"));
    if (e.key === "ArrowDown" && opts.length) {
      e.preventDefault();
      setActive(input, opts, active + 1 >= opts.length ? 0 : active + 1);
    } else if (e.key === "ArrowUp" && opts.length) {
      e.preventDefault();
      setActive(input, opts, active - 1 < 0 ? opts.length - 1 : active - 1);
    } else if (e.key === "Enter") {
      e.preventDefault();
      const pick = (active >= 0 && opts[active]) || opts[0];
      if (pick) pick.click();
    } else if (e.key === "Escape") {
      clear(input, results);
    }
  });
  // Close the dropdown when clicking away
  document.addEventListener("click", (e) => {
    if (wrap && !wrap.contains(e.target)) clear(input, results);
  });
}

function clear(input, results) {
  results.innerHTML = "";
  active = -1;
  input.setAttribute("aria-expanded", "false");
  input.removeAttribute("aria-activedescendant");
}

// Move the highlight to option `i` (roving via aria-activedescendant — focus stays in the input).
function setActive(input, opts, i) {
  active = i;
  opts.forEach((li, idx) => {
    const on = idx === i;
    li.classList.toggle("active", on);
    li.setAttribute("aria-selected", String(on));
  });
  const cur = opts[i];
  if (cur) {
    input.setAttribute("aria-activedescendant", cur.id);
    cur.scrollIntoView({ block: "nearest" });
  } else {
    input.removeAttribute("aria-activedescendant");
  }
}

async function run(q, input, results) {
  q = (q || "").trim();
  if (q.length < 3) {
    clear(input, results);
    return;
  }
  let items = [];
  try {
    const r = await fetch(`${API}/geosearch?q=${encodeURIComponent(q)}`);
    items = (await r.json()).results || [];
  } catch (e) {
    return; // silent — leave prior results
  }
  results.innerHTML = "";
  active = -1;
  input.removeAttribute("aria-activedescendant");
  if (!items.length) {
    results.innerHTML = `<li class="empty" role="option" aria-disabled="true">No matches</li>`;
    input.setAttribute("aria-expanded", "true");
    return;
  }
  items.forEach((res, idx) => {
    const li = document.createElement("li");
    li.textContent = res.name;
    li.id = `search-opt-${idx}`;
    li.setAttribute("data-pick", "1");
    li.setAttribute("role", "option");
    li.setAttribute("aria-selected", "false");
    li.onclick = () => {
      goTo(res);
      clear(input, results);
    };
    li.onmousemove = () => {
      const opts = Array.from(results.querySelectorAll("li[data-pick]"));
      if (active !== idx) setActive(input, opts, idx);
    };
    results.appendChild(li);
  });
  input.setAttribute("aria-expanded", "true");
}

function goTo(res) {
  // If a pin is being placed (drop-a-pin or fix-location), snap it to the chosen result rather than
  // only recentring — this is the "snap to a searched address" path. Otherwise just move the view.
  if (pinDragLatLng() && res.lat != null && res.lon != null) {
    snapPinTo(map, L.latLng(res.lat, res.lon));
    map.setView([res.lat, res.lon], Math.max(map.getZoom(), 16));
    return;
  }
  if (res.bbox) {
    map.fitBounds([[res.bbox.south, res.bbox.west], [res.bbox.north, res.bbox.east]], { maxZoom: 15 });
  } else {
    map.flyTo([res.lat, res.lon], 14);
  }
}
