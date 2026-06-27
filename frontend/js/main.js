import { fetchLocations } from "./api.js";
import { loadMeta } from "./config.js";
import { getTypes, initList, updateList } from "./list.js";
import { applyAttribution, initMap } from "./map.js";
import { initMarkers, render } from "./markers.js";
import { setMap as setPhotosMap } from "./photos.js";
import { initSearch } from "./search.js";
import { initSubmitPanel } from "./submit.js";

let map = null;
let debounceTimer = null;
let firstLoad = true;
let hasData = false;

function setStatus(text) {
  const el = document.getElementById("map-status");
  if (!el) return;
  if (text) {
    el.textContent = text;
    el.hidden = false;
  } else {
    el.hidden = true;
  }
}

function countFeatures(data) {
  if (!data) return 0;
  if (data.mode === "clusters") return (data.clusters || []).reduce((s, c) => s + (c.count || 0), 0);
  return (data.features || []).length;
}

async function refresh() {
  if (firstLoad) setStatus("Loading donation locations…");
  try {
    const data = await fetchLocations(map.getBounds(), "auto", getTypes());
    render(data);
    updateList(data);
    if (countFeatures(data) > 0) {
      hasData = true;
      setStatus(null);
    } else {
      setStatus("No donation locations in this area — try zooming out, or add one.");
    }
  } catch (e) {
    // Keep existing markers on a transient error; only alarm if the map is empty.
    if (!hasData) setStatus("Couldn't reach the server. It may still be starting — pan the map to retry.");
  } finally {
    firstLoad = false;
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
  setPhotosMap(map);
  initSearch(map);
  initList(map, refresh);
  initSubmitPanel();
  map.on("moveend", debouncedRefresh);
  await refresh();
}

boot();
