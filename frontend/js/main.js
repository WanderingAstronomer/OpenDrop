import { fetchAllLocations } from "./api.js";
import { initChrome } from "./chrome.js";
import { loadMeta } from "./config.js";
import { getTypes, initList, updateList } from "./list.js";
import { initLocateButton } from "./locate.js";
import { applyAttribution, fitToCoverage, initMap, initMapInsets } from "./map.js";
import { initMarkers, loadPoints, renderView } from "./markers.js";
import { initPlacePanel, openPlacePanel } from "./panel.js";
import { maybeShowWelcomeHero } from "./potd.js";
import { initSearch } from "./search.js";
import { app } from "./state.js";
import { initSubmitPanel } from "./submit.js";
import { initTheme } from "./theme.js";
import { filterFeaturesToBbox, inUSCoverage, sanitizeBbox } from "./viewport.js";

// Single-engine model: the whole active set (~17k points) is fetched ONCE and clustered client-side
// by Supercluster (js/markers.js). Panning and zooming re-render off the in-memory set with no server
// round-trip; the category filter is a client-side subset (instant, no refetch); only an external
// mutation (a new submission, a vote that flips a pin) reloads the feed.
let map = null;
let debounceTimer = null;
let allFeatures = [];   // the whole active set (all types), loaded once; re-filtered client-side
let loaded = false;     // has the one-shot load landed at least once? (drives the loading banner)
let reqSeq = 0;         // only the newest load may write allFeatures (a submit-vs-filter refetch race)

// More than this many pins in the EXACT viewport -> the list shows "zoom in to list" instead of a
// nearest-first 300-row list (the same gate the old cluster response drove).
const LIST_CAP = 500;
const COVERAGE_COPY = "OpenDrop covers the United States — head back west, or search a US city.";
const EMPTY_COPY = "No donation locations in this area — try zooming out, or add one.";

function setStatus(text) {
  const el = document.getElementById("map-status");
  if (!el) return;
  if (text) { el.textContent = text; el.hidden = false; } else { el.hidden = true; }
}

function viewportBbox() {
  const b = map.getBounds();
  return sanitizeBbox({ west: b.getWest(), south: b.getSouth(), east: b.getEast(), north: b.getNorth() });
}

// The active set filtered to the current category selection (list.js owns the selection). Client-side
// so a category switch is instant — no refetch of the 17k feed.
function typedFeatures() {
  const types = getTypes(); // CSV of org_types, or null for "everything"
  if (!types) return allFeatures;
  const set = new Set(types.split(","));
  return allFeatures.filter((f) => set.has(f.properties.org_type));
}

// Paint markers + list + banner for the current view. No network — a pure client render off the
// in-memory set. Called on every moveend/resize and after a (re)load or filter change.
function render() {
  if (!map) return;
  const bbox = viewportBbox();
  const zoom = map.getZoom();
  if (!bbox) {
    // Unsized container (boot race — the resize hooks re-fire) OR a view entirely in the noWrap void
    // past ±180 (a hard westward fling). Nothing to paint; point home.
    renderView(null, zoom);
    updateList({ mode: "points", features: [], outOfCoverage: true });
    const c = map.getContainer();
    if (c.offsetWidth > 0 && c.offsetHeight > 0) setStatus(loaded ? COVERAGE_COPY : "Loading donation locations…");
    return;
  }
  renderView(bbox, zoom);
  const inView = filterFeaturesToBbox(typedFeatures(), bbox);
  const inCoverage = inUSCoverage(bbox);

  // List: honest to the EXACT viewport. Over the cap -> the "zoom in to list" affordance (updateList
  // reads a mode:"clusters" payload as "too many"); out of coverage -> the point-home empty state.
  if (!inCoverage) updateList({ mode: "points", features: [], outOfCoverage: true });
  else if (inView.length > LIST_CAP) updateList({ mode: "clusters" });
  else updateList({ mode: "points", features: inView });

  // Banner: the loading message wins until the first load lands; then reflect what is actually in view.
  if (!loaded) setStatus("Loading donation locations…");
  else if (!inCoverage) setStatus(COVERAGE_COPY);
  else setStatus(inView.length ? null : EMPTY_COPY);
}

// One-shot load of the whole active set (boot + after a mutation). Rebuilds the cluster index and
// repaints. Monotonic seq: a slow load can't clobber a newer one (a submit refetch racing a filter).
async function loadAll() {
  const seq = ++reqSeq;
  if (!loaded) setStatus("Loading donation locations…");
  try {
    const fc = await fetchAllLocations();
    if (seq !== reqSeq) return; // superseded by a newer load
    allFeatures = fc.features || [];
    loaded = true;
    loadPoints(typedFeatures());
    render();
  } catch (e) {
    if (seq !== reqSeq) return;
    const status = e && e.status;
    if (status && status < 500) {
      // 4xx is a client-side bug — log, don't alarm; clear the banner only pre-first-load.
      console.warn("locations load rejected (client bug?)", e);
      if (!loaded) setStatus(null);
    } else {
      // 5xx or a network failure (fetch throws, no .status). Surface it — the map has no data.
      setStatus("Couldn't reach the server. It may still be starting — reload to retry.");
    }
  }
}

// Category filter changed: re-filter the already-loaded set and repaint. No refetch.
function applyFilter() {
  loadPoints(typedFeatures());
  render();
}

// External mutation (a new submission, a vote that flips a pin's visibility) — the server set changed,
// so reload the whole feed and rebuild the index.
function forceRefresh() { loadAll(); }

function debouncedRender() { clearTimeout(debounceTimer); debounceTimer = setTimeout(render, 120); }

async function boot() {
  initTheme();
  const meta = await loadMeta();
  map = initMap();
  app.map = map;
  app.refresh = forceRefresh;
  applyAttribution(map, meta.sources);
  initMarkers(map);
  initSearch(map);
  initList(map, applyFilter);
  initSubmitPanel();
  initLocateButton(map);
  initChrome(); // top-bar action cluster: layers/legend popovers + mobile FAB relocation

  // The container box changes with LAYOUT, not just window resizes: the zero-size boot race (hidden
  // tab, iframe) AND the desktop rail insets (the JS insets shift #map's left/right edges when a rail
  // opens). Watch it permanently, debounced past the .2s inset transition; invalidateSize emits
  // Leaflet's "resize", which re-derives the min zoom (map.js) and repaints via the hook below.
  const container = map.getContainer();
  if (typeof ResizeObserver !== "undefined") {
    let sizeSettle = null;
    const ro = new ResizeObserver(() => {
      clearTimeout(sizeSettle);
      sizeSettle = setTimeout(() => {
        if (container.offsetWidth > 0 && container.offsetHeight > 0) map.invalidateSize({ pan: false });
      }, 80);
    });
    ro.observe(container);
  }

  window.opendrop = app; // debug handle (also used by preview-based UI verification)
  initPlacePanel(map);

  // B6 insets, then the opening frame — IN THAT ORDER. syncInsets() settles the container box
  // SYNCHRONOUSLY so fitToCoverage frames the US in the box the user will actually see.
  const syncInsets = initMapInsets(map);
  syncInsets();
  fitToCoverage(map, meta.coverage);

  await loadAll();                     // fetch the whole set + first paint for the framed view
  map.on("moveend", debouncedRender);  // wired AFTER the first paint so boot's setView doesn't
  map.on("resize", debouncedRender);   // trigger an empty pre-data render

  // First-visit welcome hero (POTD-backed, closable, shown once). Fire-and-forget.
  maybeShowWelcomeHero();

  // Deep link: #bin/<id> opens the place panel directly — free shareable bin links.
  const m = /^#bin\/(\d+)$/.exec(location.hash || "");
  if (m) openPlacePanel(null, Number(m[1]));
}

boot();
