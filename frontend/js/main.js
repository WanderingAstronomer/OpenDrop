import { fetchLocations } from "./api.js";
import { loadMeta } from "./config.js";
import { applyAttribution, initMap } from "./map.js";
import { initMarkers, render } from "./markers.js";
import { initSubmitPanel } from "./submit.js";

let map = null;
let debounceTimer = null;

async function refresh() {
  try {
    const data = await fetchLocations(map.getBounds());
    render(data);
  } catch (e) {
    /* transient network/API error — leave existing markers in place */
  }
}

function debouncedRefresh() {
  clearTimeout(debounceTimer);
  debounceTimer = setTimeout(refresh, 300);
}

async function boot() {
  const meta = await loadMeta();
  map = initMap();
  applyAttribution(map, meta.sources);
  initMarkers(map);
  initSubmitPanel();
  map.on("moveend", debouncedRefresh);
  await refresh();
}

boot();
