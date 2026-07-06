// "Show my location" button — a plain HTML button OUTSIDE Leaflet's control system.
//
// The previous implementation was a custom L.Control whose <a> carried the leaflet-bar class
// itself; leaflet.css only sizes/styles DESCENDANT anchors (".leaflet-bar a"), so the element
// rendered with no dimensions, no background, and no hover state — an invisible hit target users
// read as "not clickable". A plain button in the page chrome sidesteps that entire failure class,
// themes with the app's CSS variables (including its tooltip), and meets the 44px touch-target
// guidance (WCAG 2.5.5 AAA / Apple HIG) that Leaflet's 26-30px controls don't.
import { toast } from "./toast.js";
import { US_DATA_ENVELOPE, prefersReducedMotion } from "./viewport.js";

const BLOCKED_COPY =
  "Location access is blocked. Enable it for this site in your browser's settings, then try again.";
const FAILED_COPY =
  "Couldn't find your location. Check that location services are on and try again.";
const OUTSIDE_COPY =
  "You appear to be outside OpenDrop's US coverage — the map only lists US locations.";

// Firefox's dismissed-without-answering prompt fires NEITHER locationfound NOR locationerror, so
// a watchdog is the only way the button ever leaves the busy state there.
const WATCHDOG_MS = 15000;

function inUS(latlng) {
  const [w, s, e, n] = US_DATA_ENVELOPE;
  return latlng.lat >= s && latlng.lat <= n && latlng.lng >= w && latlng.lng <= e;
}

export function initLocateButton(map) {
  const btn = document.getElementById("locate-btn");
  if (!btn) return;
  let youAreHere = null;
  let busy = false;     // synchronous latch — the CSS class alone races across the awaited
  let watchdog = null;  // permissions query (two fast activations could both pass a class check)

  const setBusy = (b) => {
    busy = b;
    btn.disabled = b;
    btn.classList.toggle("busy", b);
    btn.setAttribute("aria-busy", b ? "true" : "false");
    if (!b && watchdog) {
      clearTimeout(watchdog);
      watchdog = null;
    }
  };

  map.on("locationfound", (e) => {
    setBusy(false);
    // The map is clamped to the US envelope (maxBounds); centering on an overseas position would
    // just slam the camera into the bounds edge with the marker unreachable. Say so instead.
    if (!inUS(e.latlng)) {
      toast(OUTSIDE_COPY, "error");
      return;
    }
    if (youAreHere) map.removeLayer(youAreHere);
    // The classic blue you-are-here dot: white ring + soft pulsing halo (styled in style.css;
    // pulse respects prefers-reduced-motion there). A divIcon, NOT a circleMarker — data pins are
    // circle markers, and this must read as a different kind of thing. The marker pane also sits
    // above the pins' overlay pane, which is why the dot is deliberately INERT: interactive:false
    // keeps it (and its oversized animated halo) out of hit-testing so it can never swallow a tap
    // meant for a pin beneath it, and keyboard:false keeps it out of the tab order (a divIcon
    // ignores alt, so it would surface to screen readers as a nameless do-nothing "button").
    // The legend's "You are here" row is the label.
    youAreHere = L.marker(e.latlng, {
      icon: L.divIcon({
        className: "you-marker",
        html: '<span class="you-dot"></span>',
        iconSize: [18, 18],
        iconAnchor: [9, 9],
      }),
      interactive: false,
      keyboard: false,
    }).addTo(map);
    map.setView(e.latlng, Math.max(map.getZoom(), 15), { animate: !prefersReducedMotion() });
  });

  map.on("locationerror", (err) => {
    setBusy(false);
    // code 1 = permission denied (also how insecure-origin blocking surfaces; localhost counts as
    // a secure context, production runs HTTPS). code 2/3 = unavailable / timeout.
    toast(err && err.code === 1 ? BLOCKED_COPY : FAILED_COPY, "error");
  });

  btn.addEventListener("click", async () => {
    if (busy) return;
    setBusy(true);
    // Progressive enhancement: when the Permissions API is available and says "denied", the page
    // cannot re-prompt — explain instead of firing a locate() that silently fails.
    try {
      const perm = await navigator.permissions?.query?.({ name: "geolocation" });
      if (perm && perm.state === "denied") {
        setBusy(false);
        toast(BLOCKED_COPY, "error");
        return;
      }
    } catch (e) { /* Permissions API absent/limited (older Firefox/Safari) — locate() decides */ }
    watchdog = setTimeout(() => {
      map.stopLocate();
      setBusy(false);
      toast(FAILED_COPY, "error");
    }, WATCHDOG_MS);
    // setView:false — the locationfound handler owns the camera so it can refuse to chase an
    // out-of-coverage position into the maxBounds wall.
    map.locate({ setView: false, enableHighAccuracy: true, timeout: 10000 });
  });
}
