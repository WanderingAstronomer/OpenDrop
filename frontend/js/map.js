import { DEFAULT_VIEW, MIN_ZOOM } from "./config.js";

const OSM_ATTR =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

// Filled by initMap(); read by js/chrome.js to build the layers popover.
let _basemaps = null;
export function basemaps() { return _basemaps; }

// Keep the camera over US data: the app is US-only, so panning to other continents (or onto
// repeated world copies) only produces empty views and confusing wrap artifacts. noWrap stops
// duplicate world copies of tiles; maxBounds + full viscosity keeps drags inside a padded
// US envelope (CONUS + AK + HI + PR). NOTE: neither guarantees in-range getBounds() values on
// wide viewports — Leaflet legally reports longitudes beyond ±180 there (Leaflet #1885), which is
// why every data request also passes through viewport.sanitizeBbox().
const US_MAX_BOUNDS = [[14, -180], [72, -60]];

export function initMap() {
  // A6 — retain more tiles around the viewport and skip per-frame tile updates during a zoom
  // animation, so back-pans and zoom steps reuse already-loaded tiles instead of refetching.
  const baseTuning = { updateWhenZooming: false, keepBuffer: 4 };
  const streetsLight = L.tileLayer(
    "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
    { maxZoom: 20, subdomains: "abcd", noWrap: true, ...baseTuning, attribution: `${OSM_ATTR} &copy; <a href="https://carto.com/attributions">CARTO</a>` }
  );
  const streetsDetailed = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, noWrap: true, ...baseTuning, attribution: OSM_ATTR,
  });
  const ESRI_ATTR = "Tiles &copy; Esri — Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community";
  const esriImagery = () => L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 19, noWrap: true, ...baseTuning, attribution: ESRI_ATTR }
  );
  const satellite = esriImagery();
  // Hybrid = imagery + Esri's reference overlays (roads + boundaries/place labels). A layer
  // instance can only live in ONE base layer, hence the second imagery instance. The reference
  // overlays are throttled (update when idle, not per pan/zoom frame) — three tile layers
  // repainting every frame is what made the first Hybrid cut feel heavy.
  const refOpts = { maxZoom: 19, noWrap: true, updateWhenIdle: true, updateWhenZooming: false, keepBuffer: 2 };
  const hybrid = L.layerGroup([
    esriImagery(),
    L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Transportation/MapServer/tile/{z}/{y}/{x}", refOpts),
    L.tileLayer("https://server.arcgisonline.com/ArcGIS/rest/services/Reference/World_Boundaries_and_Places/MapServer/tile/{z}/{y}/{x}", refOpts),
  ]);

  const bases = { Light: streetsLight, Detailed: streetsDetailed, Satellite: satellite, Hybrid: hybrid };

  let saved = null;
  try { saved = localStorage.getItem("opendrop_basemap"); } catch (e) { /* private mode */ }
  const initialName = bases[saved] ? saved : "Light";

  // A4 — on mobile (≤1023px) hide markers during the zoom animation instead of tweening ~400
  // layers per frame. Init-time only (matchMedia read once), matching the 1024px CSS cutover.
  const isMobile = !!(window.matchMedia && window.matchMedia("(max-width: 1023px)").matches);
  const map = L.map("map", {
    zoomControl: false, // our own +/- buttons live in the bottom-right stack (js below)
    minZoom: MIN_ZOOM,
    zoomSnap: 0.25, // fractional zoom so the dynamic min-zoom below can sit exactly at "US fills the screen"
    markerZoomAnimation: !isMobile, // A4 — mobile hides markers during the zoom anim (init-time only)
    layers: [bases[initialName]],
    maxBounds: US_MAX_BOUNDS,
    maxBoundsViscosity: 1.0,
  }).setView(DEFAULT_VIEW.center, DEFAULT_VIEW.zoom);

  // Attribution lives bottom-LEFT per the responsive redesign (the bottom-right corner belongs to
  // the zoom/locate/Add control stack; separate corners = no collision at any width).
  map.attributionControl.setPosition("bottomleft");

  // Zoom controls in the bottom-right chrome, above the locate button (short travel to the rest
  // of the navigation), instead of stranded alone in the top-left.
  const zin = document.getElementById("zoom-in");
  const zout = document.getElementById("zoom-out");
  if (zin) zin.onclick = () => map.zoomIn();
  if (zout) zout.onclick = () => map.zoomOut();

  // Never allow zooming out past "the US bounds fill the viewport width": beyond that there are
  // no tiles west of -180 and the dead space reads as a rendering bug ("map data not available").
  // 120 = the US_MAX_BOUNDS longitude span. Recomputed on resize; quarter-step snapped upward so
  // the viewport is never wider than the bounds.
  function fitMinZoom() {
    const px = map.getSize().x;
    if (!px) return;
    const z = Math.log2((px * 360) / (256 * 120));
    const min = Math.max(MIN_ZOOM, Math.ceil(z * 4) / 4);
    map.setMinZoom(min);
    if (map.getZoom() < min) map.setZoom(min);
  }
  map.whenReady(fitMinZoom);
  map.on("resize", fitMinZoom);

  // Basemap registry — the redesign replaces Leaflet's in-map layers card with a top-bar popover
  // (js/chrome.js), so the swap logic lives here and the popover just calls set(). Persistence and
  // the satellite-contrast class ride the same path the old control's baselayerchange handler took.
  function applySatClass(name) {
    map.getContainer().classList.toggle("satellite-active", name === "Satellite" || name === "Hybrid");
  }
  applySatClass(initialName);
  let currentBase = initialName;
  _basemaps = {
    names: () => Object.keys(bases),
    current: () => currentBase,
    set(name) {
      if (!bases[name] || name === currentBase) return;
      map.removeLayer(bases[currentBase]);
      map.addLayer(bases[name]);
      currentBase = name;
      try { localStorage.setItem("opendrop_basemap", name); } catch (err) { /* private mode */ }
      applySatClass(name);
    },
  };

  const attrEl = map.attributionControl && map.attributionControl.getContainer();
  // Collapsible attribution: the required ODbL + source credits were eating a strip of the map (on
  // mobile they wrapped to ~5 lines), so collapse them to one compact line by default; tapping the
  // strip (not a credit link) toggles the full text. Present + accessible, just out of the way.
  if (attrEl) {
    attrEl.classList.add("attr-collapsible");
    attrEl.title = "Map data & attribution — tap to expand";
    attrEl.addEventListener("click", (e) => {
      if (e.target.closest && e.target.closest("a")) return;  // let credit links through
      attrEl.classList.toggle("attr-open");
    });
  }

  // ("Show my location" lives in js/locate.js as a plain themed HTML button in the bottom-right
  // chrome stack — the old L.Control <a> carried the leaflet-bar class itself, which leaflet.css
  // never sizes/styles (it only targets ".leaflet-bar a" descendants), leaving an invisible,
  // effectively unclickable hit target.)

  return map;
}

export function applyAttribution(map, sources) {
  (sources || []).forEach((s) => {
    if (!s.attribution) return;
    // The basemap tiles already credit "© OpenStreetMap contributors"; skip a data-source credit
    // that just repeats OpenStreetMap so it isn't shown twice (the OSM data is ODbL — recorded on
    // the Source page + in the /api/export payload). Every other source still gets its own credit.
    if (/openstreetmap/i.test(s.attribution)) return;
    map.attributionControl.addAttribution(s.attribution);
  });
}

// Frame the initial view on wherever the live data actually is, using the coverage bbox from
// /api/meta. One metro fits tight; a state fits to the state; a national dataset is too wide to
// fitBounds sensibly (Alaska + Hawaii stretch the extent and drag the centroid into the Pacific),
// so for very wide coverage we keep the fixed national view instead. No coverage => keep default.
export function fitToCoverage(map, coverage) {
  if (!coverage || !Array.isArray(coverage.bbox)) return;
  const [s, w, n, e] = coverage.bbox;
  if (![s, w, n, e].every((x) => typeof x === "number" && isFinite(x))) return;
  const lonSpan = Math.abs(e - w);
  const latSpan = Math.abs(n - s);
  if (lonSpan > 60 || latSpan > 25) {
    map.setView(DEFAULT_VIEW.center, DEFAULT_VIEW.zoom);
    return;
  }
  map.fitBounds([[s, w], [n, e]], { padding: [30, 30], maxZoom: 13, animate: false });
}
