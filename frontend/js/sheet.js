// Shared bottom-sheet behaviour for the mobile (<1024px) chrome — the responsive redesign's ONE
// draggable sheet with three snap points. Both the location list and the place-details panel are
// sheet-styled surfaces driven by this helper; they are mutually exclusive (opening details hides
// the list sheet via CSS), which is how the design's "one sheet, two modes" maps onto our two
// existing components without moving DOM between them.
//
// Height-based (el.style.height), matching the prototype: snap points are peek (13dvh min 92px by
// default; opts.peekPx overrides — the place sheet's title-only mini-bar is ~72px), half = 55dvh,
// full = 92dvh. Content scrolls only at half or above. A drag ending below ~0.82× peek asks
// opts.onDismiss(startSnap) to dismiss the sheet (the summonable-sheet model); the callback
// returns false to decline, and the sheet settles back to peek instead — which is how the place
// sheet gets its two-stage dismiss (half → mini-bar → gone) while the list dismisses in one swipe.
import { prefersReducedMotion } from "./viewport.js";

const EASE = "height .28s cubic-bezier(.4,0,.2,1)";

export function snapPx(name) {
  const H = window.innerHeight;
  if (name === "peek") return Math.max(92, H * 0.13);
  if (name === "half") return H * 0.55;
  return H * 0.92; // full
}

/**
 * @param el       the sheet element (position:fixed bottom sheet in mobile CSS)
 * @param handles  drag-handle elements (pointer events attach here; touch-action:none in CSS)
 * @param opts     { content?: Element — gets overflow toggled; onSnap?: (name) => void;
 *                   peekPx?: () => number — per-sheet peek height (default snapPx("peek"));
 *                   onDismiss?: (startSnap) => boolean|void — drag ended below the dismiss
 *                   threshold; return false to settle back to peek instead }
 */
export function makeSheet(el, handles, opts = {}) {
  let snap = "peek";
  let drag = null;      // { startY, startH, startSnap }
  let liveH = null;
  let enabled = false;

  const peekH = () => (opts.peekPx ? opts.peekPx() : snapPx("peek"));
  const heightFor = (name) => (name === "peek" ? peekH() : snapPx(name));

  const applyScroll = () => {
    const c = opts.content;
    if (c) c.style.overflowY = snap === "peek" ? "hidden" : "auto";
  };

  const setSnap = (name, animate = true) => {
    snap = name;
    if (!enabled) return;
    el.style.transition = animate && !prefersReducedMotion() ? EASE : "none";
    el.style.height = heightFor(name) + "px";
    el.dataset.snap = name; // CSS hook — e.g. the place sheet's title-only peek styling
    applyScroll();
    opts.onSnap && opts.onSnap(name);
  };

  const onDown = (e) => {
    if (!enabled) return;
    drag = { startY: e.clientY, startH: el.offsetHeight, startSnap: snap };
    liveH = null;
    el.style.transition = "none";
    el.classList.add("dragging");
    try { e.currentTarget.setPointerCapture(e.pointerId); } catch (err) { /* already captured */ }
  };
  const onMove = (e) => {
    if (!drag || !enabled) return;
    let h = drag.startH + (drag.startY - e.clientY);
    h = Math.max(50, Math.min(h, window.innerHeight * 0.92));
    liveH = h;
    el.style.height = h + "px";
  };
  const onUp = () => {
    if (!drag || !enabled) return;
    const h = liveH != null ? liveH : drag.startH;
    const startSnap = drag.startSnap;
    // A real drag also fires a click on release — flag it (one microtask) so tap-to-toggle
    // handlers on the grab handle can tell a tap from a drag tail.
    if (Math.abs(h - drag.startH) > 6) {
      el.dataset.justDragged = "1";
      setTimeout(() => { delete el.dataset.justDragged; }, 0);
    }
    drag = null;
    liveH = null;
    el.classList.remove("dragging");
    const peek = peekH();
    if (h < peek * 0.82) {
      // Below the peek band = a dismiss gesture. The owner decides (list: gone in one swipe;
      // place: only from the mini-bar). A false return (or no handler) settles back to peek.
      if (opts.onDismiss && opts.onDismiss(startSnap) !== false) return;
      setSnap("peek");
      return;
    }
    // nearest of the three snap points
    const target = [["peek", peek], ["half", snapPx("half")], ["full", snapPx("full")]]
      .reduce((a, b) => (Math.abs(b[1] - h) < Math.abs(a[1] - h) ? b : a))[0];
    setSnap(target);
  };

  // A CANCELLED gesture (orientation change, incoming call, system edge-swipe) must never commit
  // a dismissal the user didn't finish — revert to the snap the drag started from instead of
  // running onUp's dismiss logic.
  const onCancel = () => {
    if (!drag) return;
    const startSnap = drag.startSnap;
    drag = null;
    liveH = null;
    el.classList.remove("dragging");
    if (enabled) setSnap(startSnap);
  };

  handles.forEach((hEl) => {
    if (!hEl) return;
    hEl.addEventListener("pointerdown", onDown);
    hEl.addEventListener("pointermove", onMove);
    hEl.addEventListener("pointerup", onUp);
    hEl.addEventListener("pointercancel", onCancel);
  });

  const onResize = () => { if (enabled && !drag) setSnap(snap, false); };
  window.addEventListener("resize", onResize);

  return {
    setSnap,
    snap: () => snap,
    // Mobile-only: enable applies the current snap height; disable clears every inline style the
    // helper owns so the desktop rail CSS is untouched by leftovers.
    enable() { enabled = true; setSnap(snap, false); },
    disable() {
      enabled = false;
      // Abort any in-flight drag: pointer capture survives a breakpoint crossing, and a live drag
      // left behind would keep writing inline heights onto the desktop rail (or worse, its release
      // below the dismiss threshold would dismiss a rail that has no pill to bring it back).
      drag = null;
      liveH = null;
      el.classList.remove("dragging");
      el.style.height = "";
      el.style.transition = "";
      delete el.dataset.snap;
      if (opts.content) opts.content.style.overflowY = "";
    },
    destroy() { window.removeEventListener("resize", onResize); },
  };
}
