import { DEFAULT_VIEW } from "./config.js";

const OSM_ATTR =
  '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors';

export function initMap() {
  // Muted light basemap (OSM-derived via CARTO) — high contrast for the colored markers.
  const streetsLight = L.tileLayer(
    "https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}.png",
    { maxZoom: 20, subdomains: "abcd", attribution: `${OSM_ATTR} &copy; <a href="https://carto.com/attributions">CARTO</a>` }
  );
  // Full-detail OSM streets (the classic look).
  const streetsDetailed = L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: OSM_ATTR,
  });
  // Satellite imagery — drop boxes are often visible from above. Esri World Imagery (no key).
  const satellite = L.tileLayer(
    "https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}",
    { maxZoom: 19, attribution: "Tiles &copy; Esri — Source: Esri, Maxar, Earthstar Geographics, and the GIS User Community" }
  );

  const bases = { "Streets (light)": streetsLight, "Streets (detailed)": streetsDetailed, Satellite: satellite };

  let saved = null;
  try { saved = localStorage.getItem("opendrop_basemap"); } catch (e) { /* private mode */ }
  const initial = bases[saved] || streetsLight;

  const map = L.map("map", { zoomControl: true, layers: [initial] })
    .setView(DEFAULT_VIEW.center, DEFAULT_VIEW.zoom);

  L.control.layers(bases, {}, { position: "topright", collapsed: false }).addTo(map);

  map.on("baselayerchange", (e) => {
    try { localStorage.setItem("opendrop_basemap", e.name); } catch (err) { /* ignore */ }
  });

  return map;
}

export function applyAttribution(map, sources) {
  (sources || []).forEach((s) => {
    if (s.attribution) map.attributionControl.addAttribution(s.attribution);
  });
}
