import { bucketColor } from "./confidence.js";
import { openPopover } from "./popover.js";

let map = null;
let clusterGroup = null;
let serverLayer = null;

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

export function render(data) {
  clusterGroup.clearLayers();
  serverLayer.clearLayers();
  if (!data) return;

  if (data.mode === "clusters") {
    (data.clusters || []).forEach((c) => {
      const size = Math.round(Math.min(54, 30 + Math.log2(c.count + 1) * 5));
      const icon = L.divIcon({
        html: `<div class="odc-cluster" style="width:${size}px;height:${size}px">${c.count}</div>`,
        className: "",
        iconSize: [size, size],
      });
      const m = L.marker([c.lat, c.lon], { icon });
      m.on("click", () => map.flyTo([c.lat, c.lon], Math.min(map.getZoom() + 3, 16)));
      serverLayer.addLayer(m);
    });
    return;
  }

  (data.features || []).forEach((f) => {
    const [lon, lat] = f.geometry.coordinates;
    const p = f.properties;
    const marker = L.circleMarker([lat, lon], {
      radius: 7, weight: 2, color: "#ffffff", fillColor: bucketColor(p.bucket), fillOpacity: 1,
    });
    marker.on("click", () => openPopover(map, marker, p.id));
    clusterGroup.addLayer(marker);
  });
}
