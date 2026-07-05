import { API } from "./config.js";
import { pinDragLatLng, snapPinTo } from "./pindrag.js";
import { prefersReducedMotion } from "./viewport.js";

let map = null;
let timer = null;
let active = -1; // index of the highlighted option, -1 = none
let seq = 0;     // monotonic token: a response whose token != seq has been superseded and is dropped

export function initSearch(m) {
  map = m;
  const wrap = document.getElementById("search");
  const input = document.getElementById("search-input");
  const results = document.getElementById("search-results");
  const clearBtn = document.getElementById("search-clear");
  if (!input || !results) return;

  // Combobox wiring (the <ul> is already role=listbox in the markup).
  input.setAttribute("role", "combobox");
  input.setAttribute("aria-autocomplete", "list");
  input.setAttribute("aria-controls", "search-results");
  input.setAttribute("aria-expanded", "false");

  const syncClear = () => { if (clearBtn) clearBtn.hidden = !input.value; };
  if (clearBtn) {
    clearBtn.addEventListener("click", () => {
      input.value = "";
      clear(input, results);
      syncClear();
      input.focus();
    });
  }

  input.addEventListener("input", () => {
    syncClear();
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
  seq++; // invalidate any in-flight response so a late reply can't repaint / re-open after clear/Escape
  results.innerHTML = "";
  active = -1;
  input.setAttribute("aria-expanded", "false");
  input.removeAttribute("aria-activedescendant");
  announceSearch("");
}

// Screen-reader announcement of result state via the #search-live polite region (index.html).
function announceSearch(msg) {
  const live = document.getElementById("search-live");
  if (live) live.textContent = msg;
}

// Hard-failure state: a non-2xx or network error is surfaced, not left as stale results or a
// misleading "No matches".
function showSearchError(input, results) {
  results.innerHTML = `<li class="empty" role="option" aria-disabled="true">Search is unavailable — try again</li>`;
  active = -1;
  input.removeAttribute("aria-activedescendant");
  input.setAttribute("aria-expanded", "true");
  announceSearch("Search is unavailable");
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
  const token = ++seq; // this run owns the dropdown only while it stays the newest
  let items = [];
  try {
    const r = await fetch(`${API}/geosearch?q=${encodeURIComponent(q)}`);
    if (token !== seq) return;                             // superseded by a newer query — drop
    if (!r.ok) { showSearchError(input, results); return; } // 5xx / error envelope — surface it
    const body = await r.json().catch(() => ({}));
    if (token !== seq) return;                             // superseded during body parse — drop
    items = body.results || [];
  } catch (e) {
    if (token !== seq) return;
    showSearchError(input, results);                       // network failure — surface, don't leave stale
    return;
  }
  if (token !== seq) return;                               // final guard before mutating the DOM
  results.innerHTML = "";
  active = -1;
  input.removeAttribute("aria-activedescendant");
  if (!items.length) {
    results.innerHTML = `<li class="empty" role="option" aria-disabled="true">No matches</li>`;
    input.setAttribute("aria-expanded", "true");
    announceSearch("No matches");
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
  announceSearch(`${items.length} result${items.length === 1 ? "" : "s"}`);
}

function goTo(res) {
  const anim = !prefersReducedMotion();
  // If a pin is being placed (drop-a-pin or fix-location), snap it to the chosen result rather than
  // only recentring — this is the "snap to a searched address" path. Otherwise just move the view.
  if (pinDragLatLng() && res.lat != null && res.lon != null) {
    snapPinTo(map, L.latLng(res.lat, res.lon));
    map.setView([res.lat, res.lon], Math.max(map.getZoom(), 16), { animate: anim });
    return;
  }
  if (res.bbox) {
    map.fitBounds([[res.bbox.south, res.bbox.west], [res.bbox.north, res.bbox.east]], { maxZoom: 15, animate: anim });
  } else {
    map.flyTo([res.lat, res.lon], 14, { animate: anim });
  }
}
