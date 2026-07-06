// Top-right MENU wiring: a hamburger that drops a vertical icon RAIL (about / legend / theme /
// layers). Clicking a rail icon with a popover opens it to the LEFT of the rail; the theme icon
// toggles the map theme inline (wired in theme.js); "about" re-opens the welcome card. Same on
// desktop and mobile. z-scale lives in style.css (popovers = 40).
import { basemaps } from "./map.js";
import { maybeShowWelcomeHero } from "./potd.js";

// Basemap swatches for the layers popover (little tile chips).
const SWATCH = {
  Light: "linear-gradient(135deg,#e8ecdf,#cfd8cd)",
  Detailed: "linear-gradient(135deg,#d9e7c8,#b9d3a8)",
  Satellite: "linear-gradient(135deg,#3a5a3a,#7a8a5a)",
  Hybrid: "linear-gradient(135deg,#31502f,#8a9a6a)",
};

export function initChrome() {
  const menuBtn = document.getElementById("menu-btn");
  const rail = document.getElementById("menu-rail");
  const layersBtn = document.getElementById("layers-btn");
  const legendBtn = document.getElementById("legend-btn");
  const infoBtn = document.getElementById("info-btn");
  const layersPop = document.getElementById("layers-pop");
  const legendPop = document.getElementById("legend-pop");
  if (!menuBtn || !rail) return;

  const pops = [[layersBtn, layersPop], [legendBtn, legendPop]];
  const closePops = () => pops.forEach(([b, p]) => {
    if (p) p.hidden = true;
    if (b) b.setAttribute("aria-expanded", "false");
  });
  const closeAll = () => {
    closePops();
    rail.hidden = true;
    menuBtn.setAttribute("aria-expanded", "false");
  };

  menuBtn.addEventListener("click", () => {
    if (rail.hidden) {
      rail.hidden = false;
      menuBtn.setAttribute("aria-expanded", "true");
    } else {
      closeAll();
    }
  });

  // Outside-press close. (This replaced a full-screen click-away backdrop, which stacked ABOVE the
  // top bar and swallowed every rail tap — and blocked map drags while the menu was open.) A press
  // outside the menu/rail/popovers closes them WITHOUT consuming the press, so the same touch that
  // dismisses the menu can start a map drag.
  document.addEventListener("pointerdown", (e) => {
    if (rail.hidden && pops.every(([, p]) => !p || p.hidden)) return;
    const inside = [menuBtn, rail, layersPop, legendPop]
      .some((el) => el && el.contains(e.target));
    if (!inside) closeAll();
  });

  // A rail icon's popover opens to the LEFT; one at a time; the rail stays open so you can switch.
  const togglePop = (btn, pop) => {
    const opening = pop.hidden;
    closePops();
    if (opening) { pop.hidden = false; btn.setAttribute("aria-expanded", "true"); }
  };
  pops.forEach(([btn, pop]) => { if (btn && pop) btn.addEventListener("click", () => togglePop(btn, pop)); });

  // "About" re-opens the welcome card (forced past the shown-once flag); close the menu behind it.
  if (infoBtn) infoBtn.addEventListener("click", () => { closeAll(); maybeShowWelcomeHero(true); });

  // The brand logo is an about button too — tapping it re-opens the welcome card.
  const brand = document.querySelector(".brand");
  if (brand) brand.addEventListener("click", () => { closeAll(); maybeShowWelcomeHero(true); });

  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeAll(); });

  // Layers popover rows, from map.js's basemap registry.
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
}
