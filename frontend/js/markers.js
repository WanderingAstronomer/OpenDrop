import { bucketColor } from "./confidence.js";
import { openPopover } from "./popover.js";

let map = null;
let clusterGroup = null;
let serverLayer = null;

export function initMarkers(m) {
  map = m;
  clusterGroup = L.markerClusterGroup({ chunkedLoading: true, maxClusterRadius: 50 });
  map.addLayer(clusterGroup);
  serverLayer = L.layerGroup().addTo(map);
}

export function render(data) {
  clusterGroup.clearLayers();
  serverLayer.clearLayers();
  if (!data) return;

  if (data.mode === "clusters") {
    (data.clusters || []).forEach((c) => {
      const size = Math.round(Math.min(54, 26 + Math.log2(c.count + 1) * 6));
      const icon = L.divIcon({
        html: `<div class="cluster-bubble" style="width:${size}px;height:${size}px">${c.count}</div>`,
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
      radius: 7, weight: 1, color: "#fff", fillColor: bucketColor(p.bucket), fillOpacity: 0.9,
    });
    marker.on("click", () => openPopover(map, marker, p.id));
    clusterGroup.addLayer(marker);
  });
}
