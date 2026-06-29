// A single draggable "place the pin" session shared by the drop-a-pin (new location) and
// drag-to-fix (correction) flows. Drag the marker, click/tap the map to reposition it, or SNAP it
// to a precise point — your GPS fix or a searched address — via snapPinTo().

let marker = null;
let clickHandler = null;
let onMoveCb = null;  // module-scope so snapPinTo() can re-fire the active session's callback

export function startPinDrag(map, latlng, { label, onMove } = {}) {
  stopPinDrag(map);
  onMoveCb = onMove || null;
  marker = L.marker(latlng, { draggable: true, autoPan: true, keyboard: true, riseOnHover: true });
  marker.addTo(map);
  if (label) {
    marker.bindTooltip(label, { permanent: true, direction: "top", offset: [0, -34], className: "pin-tip" }).openTooltip();
  }
  const fire = () => onMoveCb && onMoveCb(marker.getLatLng());
  marker.on("dragend", fire);
  clickHandler = (e) => { marker.setLatLng(e.latlng); fire(); };
  map.on("click", clickHandler);
  return marker;
}

// Move the active pin to an exact point and recentre the map on it (used by "snap to my location"
// and "snap to searched address"). No-op if no pin session is running.
export function snapPinTo(map, latlng) {
  if (!marker) return null;
  marker.setLatLng(latlng);
  if (map) map.panTo(latlng);
  if (onMoveCb) onMoveCb(marker.getLatLng());
  return marker.getLatLng();
}

export function pinDragLatLng() {
  return marker ? marker.getLatLng() : null;
}

export function stopPinDrag(map) {
  if (clickHandler && map) { map.off("click", clickHandler); clickHandler = null; }
  if (marker && map) { map.removeLayer(marker); }
  marker = null;
  onMoveCb = null;
}
