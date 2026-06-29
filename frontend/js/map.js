import { DEFAULT_VIEW, MIN_ZOOM } from "./config.js";
import { toast } from "./toast.js";

const OSM_ATTR =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

export function initMap() {
  const streetsLight = L.tileLayer(
    "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
    { maxZoom: 20, subdomains: "abcd", attribution: `${OSM_ATTR} &copy; <a href="https://carto.com/attributions">CARTO</a>` }
  );
  const streetsDetailed = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: OSM_ATTR,
  });
  const satellite = L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 19, attribution: "Tiles &copy; Esri — Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community" }
  );

  const bases = { Light: streetsLight, Detailed: streetsDetailed, Satellite: satellite };

  let saved = null;
  try { saved = localStorage.getItem("opendrop_basemap"); } catch (e) { /* private mode */ }
  const initialName = bases[saved] ? saved : "Light";

  const map = L.map("map", { zoomControl: true, minZoom: MIN_ZOOM, layers: [bases[initialName]] })
    .setView(DEFAULT_VIEW.center, DEFAULT_VIEW.zoom);

  const layersCtl = L.control.layers(bases, {}, { position: "topright", collapsed: false }).addTo(map);
  // Title the basemap card and tag it for styling (segmented-control look lives in style.css).
  const layersEl = layersCtl.getContainer();
  layersEl.classList.add("odc-basemaps");
  const listEl = layersEl.querySelector(".leaflet-control-layers-base");
  if (listEl) {
    const h = L.DomUtil.create("div", "odc-basemaps-t", listEl);
    h.textContent = "Map";
    listEl.insertBefore(h, listEl.firstChild);
  }

  // Stronger card contrast over dark satellite imagery
  function applySatClass(name) {
    map.getContainer().classList.toggle("satellite-active", name === "Satellite");
  }
  applySatClass(initialName);
  map.on("baselayerchange", (e) => {
    try { localStorage.setItem("opendrop_basemap", e.name); } catch (err) { /* ignore */ }
    applySatClass(e.name);
  });

  // "Use my location" control (standard browser geolocation prompt)
  const Locate = L.Control.extend({
    options: { position: "topleft" },
    onAdd() {
      const a = L.DomUtil.create("a", "leaflet-bar leaflet-control odc-locate");
      a.href = "#";
      a.title = "Show my location";
      a.setAttribute("role", "button");
      a.setAttribute("aria-label", "Show my location");
      a.innerHTML = "◎";
      L.DomEvent.on(a, "click", L.DomEvent.stop).on(a, "click", () =>
        map.locate({ setView: true, maxZoom: 14, enableHighAccuracy: true })
      );
      return a;
    },
  });
  map.addControl(new Locate());

  let youAreHere = null;
  map.on("locationfound", (e) => {
    if (youAreHere) map.removeLayer(youAreHere);
    youAreHere = L.circleMarker(e.latlng, {
      radius: 8, color: "#2b6cb0", weight: 3, fillColor: "#4a90d9", fillOpacity: 0.6,
    }).addTo(map).bindTooltip("You are here");
  });
  map.on("locationerror", () =>
    toast("Couldn't get your location — check browser location permissions.", "error")
  );

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
