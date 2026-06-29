// One-shot device geolocation → Leaflet LatLng (or null on denial/timeout/unsupported).
//
// Used to SNAP a pin to where the user is standing while they're actively placing it (drop-a-pin)
// or fixing one (correction). The coordinate only positions a pin the user is deliberately
// dropping — unlike the correction GPS check, nothing here is reduced to a stored boolean about
// the user's presence; it's the location data they chose to contribute.
export function currentPosition({ timeout = 8000 } = {}) {
  return new Promise((resolve) => {
    if (!navigator.geolocation) { resolve(null); return; }
    navigator.geolocation.getCurrentPosition(
      (pos) => resolve(L.latLng(pos.coords.latitude, pos.coords.longitude)),
      () => resolve(null),
      { enableHighAccuracy: true, timeout, maximumAge: 0 },
    );
  });
}
