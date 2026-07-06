import { fetchLocations } from "./api.js";
import { initChrome } from "./chrome.js";
import { loadMeta } from "./config.js";
import { getTypes, initList, updateList } from "./list.js";
import { initLocateButton } from "./locate.js";
import { applyAttribution, fitToCoverage, initMap, initMapInsets } from "./map.js";
import { initMarkers, render } from "./markers.js";
import { initPlacePanel, openPlacePanel } from "./panel.js";
import { maybeShowWelcomeHero } from "./potd.js";
import { initSearch } from "./search.js";
import { app } from "./state.js";
import { initSubmitPanel } from "./submit.js";
import { initTheme } from "./theme.js";
import {
  US_DATA_ENVELOPE, cacheHit, expandBbox, filterFeaturesToBbox, inUSCoverage,
  isNationalView, sanitizeBbox,
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
// INVARIANT (A1): render() gets the SAME data object on a cache hit — never clone before render(), or the
// idempotent guard breaks and every pan rebuilds. (updateList's spread copy below is fine.)
function present(data, bbox) {
  render(data, bbox);
  if (data && data.mode !== "clusters") {
    updateList({ ...data, features: filterFeaturesToBbox(data.features, bbox) });
  } else {
    updateList(data);
  }
}

const COVERAGE_COPY = "OpenDrop covers the United States — head back west, or search a US city.";
// ONE shared instance, not a factory: render() is idempotent by data identity (A1), so repeated
// overseas pans present the same object and skip the rebuild instead of re-clearing every time.
const EMPTY_FC = { mode: "points", type: "FeatureCollection", features: [], outOfCoverage: true };

async function refresh() {
  const bbox = viewportBbox();
  if (!bbox) {
    firstLoad = false;
    // Two very different null-bbox cases: an UNSIZED container (boot race — stay silent, the
    // resize hooks re-trigger) vs a SIZED view lying entirely in the noWrap void past ±180
    // (a hard westward fling near the Aleutians) — that one must present the honest empty state,
    // not leave stale rows and a cleared banner over grey void.
    const c = map.getContainer();
    if (c.offsetWidth > 0 && c.offsetHeight > 0) {
      reqSeq++;
      present(EMPTY_FC, [0, 0, 0, 0]);
      setStatus(COVERAGE_COPY);
    }
    return;
  }
  const zoom = map.getZoom();
  const types = getTypes();
  // The world is freely pannable (B5), so "national-width" says nothing about WHERE the view is:
  // a min-zoom view over Europe is 65°+ wide with every region-bubble anchor off-screen. Only
  // collapse to the envelope fetch when US data territory is actually on screen (the real data
  // boxes, not the envelope's empty ocean padding); otherwise say so honestly.
  const inCoverage = inUSCoverage(bbox);
  const national = isNationalView(bbox) && inCoverage;
  const emptyCopy = inCoverage
    ? "No donation locations in this area — try zooming out, or add one."
    : COVERAGE_COPY;

  // No US data territory on screen: the result is knowably empty without a round-trip. Present
  // the empty state honestly and skip the fetch — counting a fetch's over-reach margin here
  // would clear the banner over a blank ocean (the 1.5x expansion of a min-zoom Paris view
  // reaches all the way back into US data). The seq bump invalidates any in-flight US response
  // so it can't land on top of the overseas view.
  if (!inCoverage) {
    reqSeq++;
    present(EMPTY_FC, bbox);
    setStatus(emptyCopy);
    firstLoad = false;
    return;
  }

  // A national render totals region bubbles from the WHOLE data envelope; a regional cache only
  // covers its expanded viewport. They must never serve each other — a regional cache whose box
  // happens to contain a national viewport would report region totals from partial data (Hawaii/
  // Alaska silently dropped). So the cached view's national-ness must match the current view's.
  const hit = cacheHit(cache, { zoom, types, national, bbox });
  if (hit) {
    present(cache.data, bbox); // still inside the last fetched area — no request needed
    // Keep the banner truthful on cache-hit pans: never leave a stale error/empty message up. Count
    // what's actually IN the live viewport, not the whole over-fetched cache — otherwise a zoom-tolerant
    // points hit into an empty corner of the cached box would clear the empty banner while the visible
    // map (and the list, which present() already filters) show nothing.
    const inView = cache.data && cache.data.mode !== "clusters"
      ? filterFeaturesToBbox(cache.data.features, bbox).length
      : countFeatures(cache.data);
    setStatus(inView > 0 ? null : emptyCopy);
    return;
  }

  if (firstLoad) setStatus("Loading donation locations…");
  const seq = ++reqSeq;
  try {
    // A national view always fetches the FULL data envelope: region-bubble totals must be true
    // nationwide counts (not whatever slice the padded viewport happened to cover), and the one
    // cached response then serves every national pan.
    const fetchBbox = national ? US_DATA_ENVELOPE : expandBbox(bbox, OVERFETCH);
    // Pass the map zoom so the server picks the density tier (state band vs zoom-aware grid, B8).
    const data = await fetchLocations(fetchBbox, "auto", types, zoom);
    if (seq !== reqSeq) return; // superseded by a newer pan or a mutation — drop this response
    cache = { bbox: fetchBbox, zoom, types, national, data };
    present(data, bbox);
    if (countFeatures(data) > 0) {
      hasData = true;
      setStatus(null);
    } else {
      setStatus(emptyCopy);
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
  app.map = map;
  app.refresh = forceRefresh;
  applyAttribution(map, meta.sources);
  initMarkers(map);
  initSearch(map);
  initList(map, forceRefresh);
  initSubmitPanel();
  initLocateButton(map);
  initChrome(); // top-bar action cluster: layers/legend popovers + mobile FAB relocation
  map.on("moveend", debouncedRefresh);
  map.on("resize", debouncedRefresh);

  // The container's box changes with LAYOUT, not just window resizes: the zero-size boot race
  // (hidden tab, iframe) AND the desktop rail insets (B6 — the css :has rules shift #map's
  // left/right edges when a rail opens, so the visible map IS the map). Watch it permanently,
  // debounced past the .2s inset transition's per-frame fires; invalidateSize emits Leaflet's
  // "resize", which re-derives the min zoom (map.js) and refetches via the hook above — bounds,
  // fetches, and "N in view" all track what the eye actually sees. pan:false keeps the top-left
  // anchor still, so opening a rail trims the covered side instead of re-centering everything.
  const container = map.getContainer();
  if (typeof ResizeObserver !== "undefined") {
    let sizeSettle = null;
    const ro = new ResizeObserver(() => {
      clearTimeout(sizeSettle);
      sizeSettle = setTimeout(() => {
        if (container.offsetWidth > 0 && container.offsetHeight > 0) {
          map.invalidateSize({ pan: false });
        }
      }, 80);
    });
    ro.observe(container);
  }

  window.opendrop = app; // debug handle (also used by preview-based UI verification)
  initPlacePanel(map);

  // B6 insets, then the opening frame — IN THAT ORDER. initList already opened the desktop rail
  // above; syncInsets() settles the container box SYNCHRONOUSLY (the MutationObserver variant is
  // a microtask and hasn't run yet inside this task), so fitToCoverage frames the US in the box
  // the user will actually see, not the full width the rail is about to cover.
  const syncInsets = initMapInsets(map);
  syncInsets();
  fitToCoverage(map, meta.coverage);
  await refresh();

  // First-visit welcome hero (POTD-backed, closable, shown once). Fire-and-forget: it self-fetches
  // /api/potd and no-ops if already dismissed or POTD is unavailable — never blocks map startup.
  maybeShowWelcomeHero();

  // Deep link: #bin/<id> opens the place panel directly — free shareable bin links.
  const m = /^#bin\/(\d+)$/.exec(location.hash || "");
  if (m) openPlacePanel(null, Number(m[1]));
}

boot();
