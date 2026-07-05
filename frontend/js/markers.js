import { bucketColor } from "./confidence.js";
import { openPlacePanel, panelOnceClose } from "./panel.js";
import {
  REGION_COLLAPSE_SPAN_DEG, binPoints, formatCount, isNationalView, partitionRegions, prefersReducedMotion,
} from "./viewport.js";

let map = null;
let clusterGroup = null;
let serverLayer = null;
let selectedId = null; // id of the pin whose place panel is open — re-ring it after each render() rebuild

// Bin server cluster cells onto this screen-pixel grid before drawing. The server clusters in
// DEGREES (bbox-span/32), so a busy national/state view can return hundreds of cells; legibility
// depends on PIXELS. 64px sits between Supercluster's 40px and Leaflet.markercluster's 80px
// defaults, and caps visual density by screen size instead of data density.
const BIN_PX = 64;

function clusterIcon(cluster) {
  const n = cluster.getChildCount();
  const size = n < 10 ? 34 : n < 50 ? 40 : 48;
  return L.divIcon({
    html: `<div class="odc-cluster" style="width:${size}px;height:${size}px">${n}</div>`,
    className: "",
    iconSize: [size, size],
  });
}

export function initMarkers(m) {
  map = m;
  clusterGroup = L.markerClusterGroup({
    chunkedLoading: true,
    maxClusterRadius: 40,        // looser grouping than the default 80 -> neighborhoods separate sooner
    disableClusteringAtZoom: 16, // street level -> always individual pins (good for bins)
    showCoverageOnHover: false,  // drop the distracting blue coverage polygon
    spiderfyOnMaxZoom: true,
    iconCreateFunction: clusterIcon,
  });
  map.addLayer(clusterGroup);
  serverLayer = L.layerGroup().addTo(map);
}

function bubble(lat, lon, count, label, onClick, extraClass = "") {
  const size = Math.round(Math.min(64, 34 + Math.log2(count + 1) * 4));
  const icon = L.divIcon({
    html: `<div class="odc-cluster ${extraClass}" style="width:${size}px;height:${size}px">${label}</div>`,
    className: "",
    iconSize: [size, size],
  });
  const m = L.marker([lat, lon], { icon });
  m.on("click", onClick);
  serverLayer.addLayer(m);
}

// Toggle the selection ring on a circleMarker's SVG <path>. getElement() is null until the marker
// paints (and while it's collapsed inside a cluster), so guard it — the ring re-applies on the next
// render() when the pin is individually visible again.
function applySelectedClass(marker) {
  const el = marker.getElement && marker.getElement();
  if (el) el.classList.toggle("marker-selected", selectedId != null && marker.__odId === selectedId);
}

// Remove the ring from whatever pin currently wears it in the DOM — used on select/close instead of
// toggling a captured marker instance, which may be detached after a clearLayers() rebuild.
function clearSelectionRing() {
  document.querySelectorAll("path.marker-selected").forEach((el) => el.classList.remove("marker-selected"));
}

// `bbox` is the sanitized [w,s,e,n] viewport — its longitude span decides the national collapse.
export function render(data, bbox) {
  clusterGroup.clearLayers();
  serverLayer.clearLayers();
  if (!data) return;

  if (data.mode === "clusters") {
    const cells = data.clusters || [];

    if (isNationalView(bbox)) {
      // National view, decided by width/zoom ALONE (pan-invariant — see viewport.isNationalView).
      // Collapse to at most three fixed-anchor region bubbles (AK / HI / CONUS), the albersUsa inset
      // convention; they pan with the map, so dragging the edge no longer toggles bubbles<->cells.
      // Click flies into the region at a zoom deep enough to EXIT the national view on any screen
      // width (a fixed target zoom dead-ends on wide monitors, where even zoom 5 still spans >= the
      // collapse threshold).
      partitionRegions(cells).forEach((r) => {
        bubble(r.lat, r.lon, r.count, formatCount(r.count), () => {
          const px = map.getSize().x || 1024;
          const exitZoom = Math.max(r.zoom,
            Math.ceil(Math.log2((px * 360) / (256 * REGION_COLLAPSE_SPAN_DEG))));
          map.flyTo([r.lat, r.lon], exitZoom, { animate: !prefersReducedMotion() });
        }, "odc-region");
      });
      return;
    }

    // State/metro views: merge server cells on a screen-pixel grid so bubble density is capped by
    // pixels, not by how many degree-cells the server happened to cut.
    const pts = cells.map((c) => {
      const p = map.latLngToContainerPoint([c.lat, c.lon]);
      return { x: p.x, y: p.y, lat: c.lat, lon: c.lon, count: c.count };
    });
    binPoints(pts, BIN_PX).forEach((c) => {
      bubble(c.lat, c.lon, c.count, formatCount(c.count),
        () => map.flyTo([c.lat, c.lon], Math.min(map.getZoom() + 3, 16), { animate: !prefersReducedMotion() }));
    });
    return;
  }

  (data.features || []).forEach((f) => {
    const [lon, lat] = f.geometry.coordinates;
    const p = f.properties;
    const marker = L.circleMarker([lat, lon], {
      radius: 7, weight: 2, color: "#ffffff", fillColor: bucketColor(p.bucket), fillOpacity: 1,
      // Pulse spots with an OPEN community proposal awaiting confirmations, so contributors can
      // spot the ones that need a vote (CSS animation on the SVG path; static ring under
      // prefers-reduced-motion).
      className: p.has_pending ? "odc-pending" : "",
    });
    marker.__odId = p.id;
    marker.on("click", () => {
      selectedId = p.id;
      openPlacePanel(marker.getLatLng(), p.id);
      // selection ring (Mapbox-pattern sync cue); tracked by id so it survives the clearLayers()
      // rebuild on every pan. Clear any prior ring first (it may sit on a now-detached node), ring
      // this pin, and on close clear by DOM query — never via the captured (possibly stale) instance.
      clearSelectionRing();
      applySelectedClass(marker);
      panelOnceClose(() => { if (selectedId === p.id) { selectedId = null; clearSelectionRing(); } });
    });
    clusterGroup.addLayer(marker);
    if (p.id === selectedId) applySelectedClass(marker); // re-ring the open pin after a rebuild
  });
}
