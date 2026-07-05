import { fetchLocations } from "./api.js";
import { loadMeta } from "./config.js";
import { getTypes, initList, updateList } from "./list.js";
import { initLocateButton } from "./locate.js";
import { applyAttribution, fitToCoverage, initMap } from "./map.js";
import { initMarkers, render } from "./markers.js";
import { initPlacePanel, openPlacePanel } from "./panel.js";
import { maybeShowWelcomeHero } from "./potd.js";
import { initSearch } from "./search.js";
import { app } from "./state.js";
import { initSubmitPanel } from "./submit.js";
import { initTheme } from "./theme.js";
import {
  US_DATA_ENVELOPE, bboxContains, expandBbox, filterFeaturesToBbox, isNationalView, sanitizeBbox,
} from "./viewport.js";

let map = null;
let debounceTimer = null;
let firstLoad = true;
let hasData = false;

// Over-fetch cache: the last request covered `bbox` (viewport grown by OVERFETCH) at `zoom` for
// `types`. While the viewport stays inside it at the same zoom, re-render from cache instead of
// refetching — the standard buffered-extent pattern (cf. Leaflet's own keepBuffer tile margin),
// which kills the request-per-small-pan problem without a manual "search this area" button.
const OVERFETCH = 1.5;
let cache = null;
// Monotonic request token: only the NEWEST request may write the cache or the DOM. Without it a
// slow response from pan A lands after fast pan B and clobbers B's view — and worse, a response
// in flight when a mutation calls forceRefresh() would silently repopulate the just-busted cache.
let reqSeq = 0;

function setStatus(text) {
  const el = document.getElementById("map-status");
  if (!el) return;
  if (text) {
    el.textContent = text;
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

function countFeatures(data) {
  if (!data) return 0;
  if (data.mode === "clusters") return (data.clusters || []).reduce((s, c) => s + (c.count || 0), 0);
  return (data.features || []).length;
}

function viewportBbox() {
  const b = map.getBounds();
  return sanitizeBbox({ west: b.getWest(), south: b.getSouth(), east: b.getEast(), north: b.getNorth() });
}

// Present `data` for the current viewport: markers see everything fetched (over-fetch margin keeps
// pans smooth); the list panel is filtered to what is actually in view so "N in view" stays honest.
function present(data, bbox) {
  render(data, bbox);
  if (data && data.mode !== "clusters") {
    updateList({ ...data, features: filterFeaturesToBbox(data.features, bbox) });
  } else {
    updateList(data);
  }
}

async function refresh() {
  const bbox = viewportBbox();
  if (!bbox) {
    // Container not laid out yet (zero-area bounds) — never a server problem. The resize hooks
    // below re-trigger once the map has real dimensions.
    firstLoad = false;
    return;
  }
  const zoom = map.getZoom();
  const types = getTypes();
  const national = isNationalView(bbox);

  // A national render totals region bubbles from the WHOLE data envelope; a regional cache only
  // covers its expanded viewport. They must never serve each other — a regional cache whose box
  // happens to contain a national viewport would report region totals from partial data (Hawaii/
  // Alaska silently dropped). So the cached view's national-ness must match the current view's.
  const hit = cache && cache.zoom === zoom && cache.types === types &&
    cache.national === national && (national || bboxContains(cache.bbox, bbox));
  if (hit) {
    present(cache.data, bbox); // still inside the last fetched area — no request needed
    // Keep the banner truthful on cache-hit pans: never leave a stale error/empty message up.
    setStatus(countFeatures(cache.data) > 0
      ? null
      : "No donation locations in this area — try zooming out, or add one.");
    return;
  }

  if (firstLoad) setStatus("Loading donation locations…");
  const seq = ++reqSeq;
  try {
    // A national view always fetches the FULL data envelope: region-bubble totals must be true
    // nationwide counts (not whatever slice the padded viewport happened to cover), and the one
    // cached response then serves every national pan.
    const fetchBbox = national ? US_DATA_ENVELOPE : expandBbox(bbox, OVERFETCH);
    const data = await fetchLocations(fetchBbox, "auto", types);
    if (seq !== reqSeq) return; // superseded by a newer pan or a mutation — drop this response
    cache = { bbox: fetchBbox, zoom, types, national, data };
    present(data, bbox);
    if (countFeatures(data) > 0) {
      hasData = true;
      setStatus(null);
    } else {
      setStatus("No donation locations in this area — try zooming out, or add one.");
    }
  } catch (e) {
    if (seq !== reqSeq) return; // stale failure — a newer request owns the UI now
    // Only claim the server is unreachable when it actually is: a network failure (fetch throws
    // TypeError, no .status) or a 5xx. A 4xx is a client-side bug — log it, don't alarm the user.
    const status = e && e.status;
    if (status && status < 500) {
      // 4xx is a client-side bug — log, don't alarm the user. Only clear the banner pre-first-load
      // (nothing on the map yet to contradict a cleared status).
      console.warn("locations request rejected (client bug?)", e);
      if (!hasData) setStatus(null);
    } else {
      // 5xx or network failure (fetch throws, no .status). Surface it ALWAYS — even after a
      // successful first load — so panning into a new area during a server blip isn't silent.
      setStatus("Couldn't reach the server. It may still be starting — pan the map to retry.");
    }
  } finally {
    firstLoad = false;
  }
}

function debouncedRefresh() {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(refresh, 300);
}

// External mutations (a new submission, a filter change) must bypass the over-fetch cache — and
// invalidate any response already in flight, which predates the mutation by definition.
function forceRefresh() {
  reqSeq++;
  cache = null;
  debouncedRefresh();
}

async function boot() {
  initTheme();
  const meta = await loadMeta();
  map = initMap();
  fitToCoverage(map, meta.coverage);
  app.map = map;
  app.refresh = forceRefresh;
  applyAttribution(map, meta.sources);
  initMarkers(map);
  initSearch(map);
  initList(map, forceRefresh);
  initSubmitPanel();
  initLocateButton(map);
  map.on("moveend", debouncedRefresh);
  map.on("resize", debouncedRefresh);

  // If the container had no size at init (hidden tab, iframe race), Leaflet computes a zero-area
  // view and every bounds is degenerate. Watch for the first real layout, then re-measure once.
  const container = map.getContainer();
  if (typeof ResizeObserver !== "undefined") {
    const ro = new ResizeObserver(() => {
      if (container.offsetWidth > 0 && container.offsetHeight > 0) {
        map.invalidateSize();
        ro.disconnect();
        debouncedRefresh();
      }
    });
    ro.observe(container);
  }

  window.opendrop = app; // debug handle (also used by preview-based UI verification)
  initPlacePanel(map);
  await refresh();

  // First-visit welcome hero (POTD-backed, closable, shown once). Fire-and-forget: it self-fetches
  // /api/potd and no-ops if already dismissed or POTD is unavailable — never blocks map startup.
  maybeShowWelcomeHero();

  // Deep link: #bin/<id> opens the place panel directly — free shareable bin links.
  const m = /^#bin\/(\d+)$/.exec(location.hash || "");
  if (m) openPlacePanel(null, Number(m[1]));
}

boot();
