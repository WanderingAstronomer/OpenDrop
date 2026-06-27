import { API } from "./config.js";

let map = null;
let timer = null;

export function initSearch(m) {
  map = m;
  const wrap = document.getElementById("search");
  const input = document.getElementById("search-input");
  const results = document.getElementById("search-results");
  if (!input || !results) return;

  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(() => run(input.value, results), 350);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") {
      e.preventDefault();
      const first = results.querySelector("li[data-pick]");
      if (first) first.click();
    } else if (e.key === "Escape") {
      results.innerHTML = "";
    }
  });
  // Close the dropdown when clicking away
  document.addEventListener("click", (e) => {
    if (wrap && !wrap.contains(e.target)) results.innerHTML = "";
  });
}

async function run(q, results) {
  q = (q || "").trim();
  if (q.length < 3) {
    results.innerHTML = "";
    return;
  }
  let items = [];
  try {
    const r = await fetch(`${API}/geosearch?q=${encodeURIComponent(q)}`);
    items = (await r.json()).results || [];
  } catch (e) {
    return; // silent — leave prior results
  }
  results.innerHTML = "";
  if (!items.length) {
    results.innerHTML = `<li class="empty">No matches</li>`;
    return;
  }
  items.forEach((res) => {
    const li = document.createElement("li");
    li.textContent = res.name;
    li.setAttribute("data-pick", "1");
    li.tabIndex = 0;
    li.onclick = () => {
      goTo(res);
      results.innerHTML = "";
    };
    li.onkeydown = (e) => { if (e.key === "Enter") li.click(); };
    results.appendChild(li);
  });
}

function goTo(res) {
  if (res.bbox) {
    map.fitBounds([[res.bbox.south, res.bbox.west], [res.bbox.north, res.bbox.east]], { maxZoom: 15 });
  } else {
    map.flyTo([res.lat, res.lon], 14);
  }
}
