import { DEFAULT_VIEW } from "./config.js";

export function initMap() {
  const map = L.map("map", { zoomControl: true }).setView(DEFAULT_VIEW.center, DEFAULT_VIEW.zoom);
  L.tileLayer("https://tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
  }).addTo(map);
  return map;
}

export function applyAttribution(map, sources) {
  (sources || []).forEach((s) => {
    if (s.attribution) map.attributionControl.addAttribution(s.attribution);
  });
}
