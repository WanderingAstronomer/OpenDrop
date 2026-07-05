import { DEFAULT_VIEW, MIN_ZOOM } from "./config.js";

const OSM_ATTR =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

// Keep the camera over US data: the app is US-only, so panning to other continents (or onto
// repeated world copies) only produces empty views and confusing wrap artifacts. noWrap stops
// duplicate world copies of tiles; maxBounds + full viscosity keeps drags inside a padded
// US envelope (CONUS + AK + HI + PR). NOTE: neither guarantees in-range getBounds() values on
// wide viewports — Leaflet legally reports longitudes beyond ±180 there (Leaflet #1885), which is
// why every data request also passes through viewport.sanitizeBbox().
const US_MAX_BOUNDS = [[14, -180], [72, -60]];

export function initMap() {
  const streetsLight = L.tileLayer(
    "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
    { maxZoom: 20, subdomains: "abcd", noWrap: true, attribution: `${OSM_ATTR} &copy; <a href="https://carto.com/attributions">CARTO</a>` }
  );
  const streetsDetailed = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, noWrap: true, attribution: OSM_ATTR,
  });
  const ESRI_ATTR = "Tiles &copy; Esri — Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community";
  const esriImagery = () => L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 19, noWrap: true, attribution: ESRI_ATTR }
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

  const map = L.map("map", {
    zoomControl: false, // our own +/- buttons live in the bottom-right stack (js below)
    minZoom: MIN_ZOOM,
    zoomSnap: 0.25, // fractional zoom so the dynamic min-zoom below can sit exactly at "US fills the screen"
    layers: [bases[initialName]],
    maxBounds: US_MAX_BOUNDS,
    maxBoundsViscosity: 1.0,
  }).setView(DEFAULT_VIEW.center, DEFAULT_VIEW.zoom);

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

  // Collapse the basemap card into its icon on phones — expanded, it overlaps the top-center search
  // bar (which un-zooms to full width below 900px). Desktop keeps it open; re-sync on breakpoint
  // crossings so a narrowed window collapses and a widened one re-opens.
  const wideMq = window.matchMedia("(min-width: 768px)");
  const layersCtl = L.control.layers(bases, {}, { position: "topright", collapsed: !wideMq.matches }).addTo(map);
  wideMq.addEventListener?.("change", (e) => { if (e.matches) layersCtl.expand(); else layersCtl.collapse(); });
  // Title the basemap card and tag it for styling (segmented-control look lives in style.css).
  const layersEl = layersCtl.getContainer();
  layersEl.classList.add("odc-basemaps");
  const listEl = layersEl.querySelector(".leaflet-control-layers-base");
  if (listEl) {
    const h = L.DomUtil.create("div", "odc-basemaps-t", listEl);
    h.textContent = "Map";
    listEl.insertBefore(h, listEl.firstChild);
  }

  // Stronger card contrast over dark imagery (Satellite AND Hybrid)
  function applySatClass(name) {
    map.getContainer().classList.toggle("satellite-active", name === "Satellite" || name === "Hybrid");
  }
  applySatClass(initialName);
  map.on("baselayerchange", (e) => {
    try { localStorage.setItem("opendrop_basemap", e.name); } catch (err) { /* ignore */ }
    applySatClass(e.name);
  });

  // ("Show my location" lives in js/locate.js as a plain themed HTML button in the bottom-right
  // chrome stack — the old L.Control <a> carried the leaflet-bar class itself, which leaflet.css
  // never sizes/styles (it only targets ".leaflet-bar a" descendants), leaving an invisible,
  // effectively unclickable hit target.)

  return map;
}

export function applyAttribution(map, sources) {
  (sources || []).forEach((s) => {
    if (s.attribution) map.attributionControl.addAttribution(s.attribution);
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
