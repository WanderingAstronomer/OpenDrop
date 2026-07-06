// Wikimedia Commons "Picture of the Day" — one cached daily image reused in two spots, each with
// the REQUIRED license attribution:
//   (a) the placeholder in a place's photo section when it has zero community photos, and
//   (b) a closable first-visit welcome hero on the map.
// The backend (/api/potd) is the single fetch and does all the graceful-degrade work; this module
// memoizes that one call and renders attribution-complete DOM. If POTD is unavailable, everything
// here renders nothing — the feature is purely additive.

import { API } from "./config.js";

const WELCOME_KEY = "od_welcome_seen";

// Memoized fetch: the PROMISE is cached (not just the value), so concurrent callers — the photo
// placeholder and the welcome hero both fire during startup — share one in-flight request. Resolves
// to the payload when available, or null on any failure/unavailable so callers can `if (!potd) return`.
let _potdPromise = null;
export function getPotd() {
  if (!_potdPromise) {
    _potdPromise = fetch(`${API}/potd`)
      .then((r) => (r.ok ? r.json() : null))
      .then((d) => (d && d.available ? d : null))
      .catch(() => null);  // network throw -> treat as unavailable, never reject
  }
  return _potdPromise;
}

// Build the shared "Photo of the day: {artist} · {license}" credit line. Attribution is mandatory,
// so this is reused verbatim by both the placeholder and the hero. All text goes through
// textContent (never innerHTML) so an upstream artist/description string can't inject markup.
function buildCredit(potd) {
  const credit = document.createElement("p");
  credit.className = "potd-credit";

  const prefix = document.createElement("span");
  prefix.textContent = "Photo of the day: ";
  credit.appendChild(prefix);

  if (potd.artist) {
    const who = document.createElement("span");
    who.textContent = potd.artist;
    credit.appendChild(who);
  }

  // License text links to the license deed (the attribution requirement).
  if (potd.license) {
    credit.appendChild(document.createTextNode(potd.artist ? " · " : ""));
    if (potd.license_url) {
      const lic = document.createElement("a");
      lic.href = potd.license_url;
      lic.target = "_blank";
      lic.rel = "noopener";
      lic.textContent = potd.license;
      credit.appendChild(lic);
    } else {
      const lic = document.createElement("span");
      lic.textContent = potd.license;
      credit.appendChild(lic);
    }
  }

  // Always offer a link back to the Commons file page (the canonical source/attribution page).
  if (potd.source_url) {
    credit.appendChild(document.createTextNode(" · "));
    const src = document.createElement("a");
    src.href = potd.source_url;
    src.target = "_blank";
    src.rel = "noopener";
    src.textContent = "Wikimedia Commons";
    credit.appendChild(src);
  }
  return credit;
}

// (a) Photo-section placeholder. Renders the POTD thumbnail into `hostEl` (the `.ph-top` element),
// wrapped in a link to the Commons file page, above the shared credit line. No-op when POTD is
// unavailable, so the section just shows its normal "Photos (0) / Add photo" affordances.
export async function mountPotdPlaceholder(hostEl) {
  if (!hostEl) return;
  const potd = await getPotd();
  if (!potd || !hostEl.isConnected) return;  // unavailable, or the popover closed while we fetched
  const thumb = potd.thumb_url || potd.image_url;
  if (!thumb) return;

  const wrap = document.createElement("div");
  wrap.className = "potd-placeholder";

  const link = document.createElement("a");
  link.href = potd.source_url || thumb;
  link.target = "_blank";
  link.rel = "noopener";
  const img = document.createElement("img");
  img.src = thumb;
  img.loading = "lazy";
  // alt makes clear this is a stand-in, not a photo of the actual location.
  img.alt = "No community photos yet — showing Wikimedia's picture of the day as a placeholder";
  link.appendChild(img);
  wrap.appendChild(link);
  wrap.appendChild(buildCredit(potd));

  hostEl.appendChild(wrap);
}

// (b) First-visit welcome hero. A bounded, NON-MODAL, closable card (never covers the whole map)
// that explains what OpenDrop is, shows the POTD image + attribution, and sets a localStorage flag
// on close so it shows exactly once. No-op if already seen or POTD is unavailable.
export async function maybeShowWelcomeHero(force) {
  if (!force) {
    try {
      if (localStorage.getItem(WELCOME_KEY)) return;
    } catch (e) {
      return;  // storage blocked (private mode) -> skip rather than risk showing it every load
    }
  }
  if (document.querySelector(".welcome-overlay")) return;  // already open (e.g. re-tapped "about")
  const potd = await getPotd();
  if (!potd) return;

  const opener = document.activeElement;  // hand focus back here on close

  // A centered greeting on a light scrim — dismissible every way (✕ / Got it / Escape / scrim click),
  // shown exactly once. Still not a hard modal: closing it leaves the live map behind.
  const overlay = document.createElement("div");
  overlay.className = "welcome-overlay";

  const card = document.createElement("section");
  card.className = "welcome-hero";
  card.setAttribute("role", "dialog");
  card.setAttribute("aria-modal", "false");
  card.setAttribute("aria-label", "Welcome to OpenDrop");

  // Close button (also the initial focus target).
  const close = document.createElement("button");
  close.type = "button";
  close.className = "welcome-x";
  close.setAttribute("aria-label", "Close welcome");
  close.textContent = "✕";  // ✕
  card.appendChild(close);

  const img = potd.image_url || potd.thumb_url;  // full-res for the full-aspect display
  if (img) {
    const media = document.createElement("a");
    media.href = potd.source_url || img;
    media.target = "_blank";
    media.rel = "noopener";
    media.className = "welcome-media";
    const el = document.createElement("img");
    el.src = img;
    el.loading = "lazy";
    el.alt = "Wikimedia's picture of the day";
    media.appendChild(el);
    card.appendChild(media);
  }

  const body = document.createElement("div");
  body.className = "welcome-body";
  const h = document.createElement("h2");
  h.className = "welcome-title";
  h.textContent = "Welcome to OpenDrop";
  const blurb = document.createElement("p");
  blurb.className = "welcome-blurb";
  blurb.textContent =
    "OpenDrop is a free, community-maintained map of clothing-donation drop-off locations across "
    + "the US. Anyone can add a spot, fix a pin, or confirm one's still there. Built as an "
    + "open-data project.";
  body.appendChild(h);
  body.appendChild(blurb);
  body.appendChild(buildCredit(potd));

  // Maker byline.
  const author = document.createElement("p");
  author.className = "welcome-author";
  author.appendChild(document.createTextNode("Built by "));
  const who = document.createElement("a");
  who.href = "https://wanderingastronomer.github.io";
  who.target = "_blank";
  who.rel = "noopener";
  who.textContent = "Andrew Brown";
  author.appendChild(who);
  body.appendChild(author);

  const gotit = document.createElement("button");
  gotit.type = "button";
  gotit.className = "btn primary welcome-ok";
  gotit.textContent = "Got it";
  body.appendChild(gotit);
  card.appendChild(body);
  overlay.appendChild(card);

  let onKey = null;
  const dismiss = () => {
    try { localStorage.setItem(WELCOME_KEY, "1"); } catch (e) { /* storage blocked — still close */ }
    if (onKey) { document.removeEventListener("keydown", onKey); onKey = null; }
    overlay.remove();
    try { if (opener && opener.focus) opener.focus({ preventScroll: true }); } catch (e) { /* gone */ }
  };
  close.onclick = dismiss;
  gotit.onclick = dismiss;
  overlay.addEventListener("click", (e) => { if (e.target === overlay) dismiss(); });  // scrim click

  // Escape closes; Tab is trapped inside the card so focus doesn't wander to the map behind it.
  onKey = (e) => {
    if (e.key === "Escape") { e.preventDefault(); dismiss(); return; }
    if (e.key !== "Tab") return;
    const f = Array.from(card.querySelectorAll(
      'a[href], button:not([disabled]), [tabindex]:not([tabindex="-1"])',
    )).filter((el) => el.offsetParent !== null);
    if (!f.length) return;
    const first = f[0];
    const last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };
  document.addEventListener("keydown", onKey);

  document.body.appendChild(overlay);
  try { close.focus({ preventScroll: true }); } catch (e) { /* not focusable yet */ }
}
