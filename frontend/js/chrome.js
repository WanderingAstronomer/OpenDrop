// App-chrome wiring for the responsive redesign: the top-bar action cluster (layers / legend /
// theme), their popovers, and the mobile relocation of the legend+theme pair into a bottom-left
// FAB stack. The z-scale contract lives in style.css (map 0 < controls/FABs 10 < rails/sheet 20 <
// top bar 30 < popovers 40).
import { basemaps } from "./map.js";

const DESKTOP_MQ = "(min-width: 1024px)";

// Basemap swatches for the layers popover (mirrors the design's little tile chips).
const SWATCH = {
  Light: "linear-gradient(135deg,#e8ecdf,#cfd8cd)",
  Detailed: "linear-gradient(135deg,#d9e7c8,#b9d3a8)",
  Satellite: "linear-gradient(135deg,#3a5a3a,#7a8a5a)",
  Hybrid: "linear-gradient(135deg,#31502f,#8a9a6a)",
};

export function initChrome() {
  const layersBtn = document.getElementById("layers-btn");
  const legendBtn = document.getElementById("legend-btn");
  const layersPop = document.getElementById("layers-pop");
  const legendPop = document.getElementById("legend-pop");
  const backdrop = document.getElementById("pop-backdrop");
  const cluster = document.getElementById("util-cluster");
  const barActions = document.querySelector(".topbar-actions");
  let opener = null; // focus returns here on close

  // ---- popover open/close (one at a time; backdrop click / Escape dismiss) ----
  const pops = [[layersBtn, layersPop], [legendBtn, legendPop]];
  function closeAll() {
    pops.forEach(([b, p]) => { if (p) p.hidden = true; if (b) b.setAttribute("aria-expanded", "false"); });
    if (backdrop) backdrop.hidden = true;
    if (opener) { try { opener.focus({ preventScroll: true }); } catch (e) { /* gone */ } opener = null; }
  }
  function toggle(btn, pop) {
    const opening = pop.hidden;
    closeAll();
    if (opening) {
      opener = btn;
      pop.hidden = false;
      backdrop.hidden = false;
      btn.setAttribute("aria-expanded", "true");
    }
  }
  pops.forEach(([btn, pop]) => { if (btn && pop) btn.addEventListener("click", () => toggle(btn, pop)); });
  if (backdrop) backdrop.addEventListener("click", closeAll);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeAll(); });

  // ---- layers popover: one row per basemap, driven by map.js's base registry ----
  const bm = basemaps();
  if (layersPop && bm) {
    const render = () => {
      layersPop.innerHTML = "";
      bm.names().forEach((name) => {
        const b = document.createElement("button");
        b.type = "button";
        b.className = "pop-row" + (bm.current() === name ? " active" : "");
        b.setAttribute("aria-pressed", String(bm.current() === name));
        const sw = document.createElement("span");
        sw.className = "pop-swatch";
        sw.style.background = SWATCH[name] || "var(--surface-3)";
        b.appendChild(sw);
        b.appendChild(document.createTextNode(name));
        b.onclick = () => { bm.set(name); render(); closeAll(); };
        layersPop.appendChild(b);
      });
    };
    render();
  }

  // ---- legend+theme cluster placement: top-bar (desktop) vs bottom-left FAB stack (mobile) ----
  // Same nodes both places (state + listeners survive), relocated on breakpoint crossings.
  const mq = window.matchMedia(DESKTOP_MQ);
  function place(desktop) {
    if (!cluster) return;
    if (desktop) {
      cluster.classList.add("in-bar");
      if (barActions && cluster.parentElement !== barActions) barActions.appendChild(cluster);
    } else {
      cluster.classList.remove("in-bar");
      if (cluster.parentElement !== document.body) document.body.appendChild(cluster);
    }
    closeAll(); // a popover anchored to the old placement would float wrong — just dismiss
  }
  place(mq.matches);
  mq.addEventListener?.("change", (e) => place(e.matches));
}
