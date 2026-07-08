// Top-bar action wiring, two modes for one set of buttons (about / legend / theme / layers):
// desktop (>=1024px) shows them as a permanent inline row in the bar (no hamburger; popovers drop
// below the bar); mobile collapses them behind a ☰ that drops a vertical icon RAIL, popovers
// opening to the rail's LEFT. The theme icon toggles inline (wired in theme.js); "about" re-opens
// the welcome card. z-scale lives in style.css (popovers = 40).
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
  // Desktop keeps the rail permanently visible (an inline row in the bar), so "close" only ever
  // means the popovers there; on mobile it also folds the rail back behind the ☰.
  const desktopMq = window.matchMedia("(min-width: 1024px)");
  const closeAll = () => {
    closePops();
    if (!desktopMq.matches) {
      rail.hidden = true;
      menuBtn.setAttribute("aria-expanded", "false");
    }
  };

  // Mode seat: desktop = permanent toolbar (menu roles off — role="menu" implies a popup, which
  // it no longer is); mobile = the ☰-controlled menu rail, hidden at rest.
  const railBtns = Array.from(rail.querySelectorAll(".rail-btn"));
  const seatMode = (desktop) => {
    // Captured BEFORE anything hides: crossing the breakpoint mid-session (browser zoom, tablet
    // rotation) must not silently drop keyboard focus to <body> when the focused element hides.
    const focused = document.activeElement;
    const focusLoses = focused === menuBtn || rail.contains(focused)
      || pops.some(([, p]) => p && p.contains(focused));
    closePops();
    if (desktop) {
      rail.hidden = false;
      rail.setAttribute("role", "group");
      railBtns.forEach((b) => b.removeAttribute("role"));
      if (focusLoses && railBtns[0]) railBtns[0].focus();  // the ☰ goes display:none
    } else {
      rail.hidden = true;
      rail.setAttribute("role", "menu");
      railBtns.forEach((b) => b.setAttribute("role", "menuitem"));
      menuBtn.setAttribute("aria-expanded", "false");
      if (focusLoses) menuBtn.focus();  // the rail (and any popover) just hid
    }
  };
  seatMode(desktopMq.matches);
  const onMqChange = (e) => seatMode(e.matches);
  if (desktopMq.addEventListener) desktopMq.addEventListener("change", onMqChange);
  else if (desktopMq.addListener) desktopMq.addListener(onMqChange);  // Safari <14

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
    const railOpenMobile = !desktopMq.matches && !rail.hidden;
    if (!railOpenMobile && pops.every(([, p]) => !p || p.hidden)) return;
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

  // The info-circle opens the standalone "How to use OpenDrop" guide page (same tab).
  if (infoBtn) infoBtn.addEventListener("click", () => { closeAll(); window.location.href = "/guide.html"; });

  // The brand logo is the about button — tapping it re-opens the welcome card.
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
