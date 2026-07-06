// A small, self-contained before/after Leaflet map for one pending pin-move. Draws the ORIGINAL pin
// (blue, the immutable anchor the 2 km cap measures from) and the PROPOSED pin (orange), joined by a
// dashed line, framed to both. Uses SVG circleMarkers so it needs no marker-image assets, and the
// CARTO basemap (CSP-whitelisted; light/dark by theme). Interactions that hijack page scroll are off
// (scrollWheelZoom); drag / double-click / the +/- control still let the operator look closer.
//
// Returns the Leaflet map so the caller can .remove() it on teardown (a view swap), or null when
// Leaflet failed to load — in which case the card's textual coordinates + distance still stand alone.

const ORIGIN_COLOR = "#2b6cb0";    // blue — original / current anchor
const PROPOSED_COLOR = "#e08c2e";  // orange — proposed move (matches --medium family)

function tileUrl() {
  const dark = document.documentElement.getAttribute("data-theme") === "dark";
  return `https://{s}.basemaps.cartocdn.com/${dark ? "dark_all" : "light_all"}/{z}/{x}/{y}.png`;
}

export function buildMiniMap(container, origin, suggested) {
  if (typeof L === "undefined" || !container) return null;
  if (!Number.isFinite(origin?.lat) || !Number.isFinite(suggested?.lat)) return null;

  const map = L.map(container, {
    zoomControl: true,
    attributionControl: true,
    scrollWheelZoom: false,       // never steal the page scroll
    keyboard: false,
  });
  L.tileLayer(tileUrl(), {
    maxZoom: 20, subdomains: "abcd", noWrap: true,
    attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> &copy; <a href="https://carto.com/attributions">CARTO</a>',
  }).addTo(map);

  const o = [origin.lat, origin.lon];
  const s = [suggested.lat, suggested.lon];

  L.polyline([o, s], { color: PROPOSED_COLOR, weight: 2, dashArray: "5,6", opacity: 0.85 }).addTo(map);

  const dot = (color) => ({
    radius: 8, color: "#fff", weight: 2, fillColor: color, fillOpacity: 1,
  });
  L.circleMarker(o, dot(ORIGIN_COLOR)).addTo(map)
    .bindTooltip("Original", { direction: "top", offset: [0, -6] });
  L.circleMarker(s, dot(PROPOSED_COLOR)).addTo(map)
    .bindTooltip("Proposed", { direction: "top", offset: [0, -6] });

  map.fitBounds(L.latLngBounds([o, s]).pad(0.55), { maxZoom: 17, animate: false });
  return map;
}
