// PRIMARY view: the two-band photo pin-move review queue (migration 0013). Each held move (Band B:
// 250 m–2 km, ≥4 independent upvoters) gets a card with the evidence photo, a before/after mini-map,
// the exact coordinates + distance, the vote tally, and Approve / Reject. Approve commits the move
// (moves the pin, writes a revertible moderation_audit row); Reject drops it (the pin never moves).

import { applyMove, listPendingMoves, rejectMove } from "./api.js";
import { formatCoord, formatDistance, relativeTime } from "./fmt.js";
import { buildMiniMap } from "./minimap.js";
import { el, flagAuthLost, isAuthError, reportError } from "./ui.js";
import { toast } from "../toast.js";

let maps = [];   // live Leaflet instances, torn down on view swap / reload
let listEl = null;
let countEl = null;

// Placeholder for an evidence photo whose file 404s (e.g. taken down after it queued) — keeps the
// card layout instead of the browser's broken-image glyph. Mirrors photos.js.
const BROKEN_IMG =
  "data:image/svg+xml;utf8," +
  encodeURIComponent(
    "<svg xmlns='http://www.w3.org/2000/svg' width='160' height='120'>" +
      "<rect width='100%' height='100%' fill='#e5e7eb'/>" +
      "<text x='50%' y='50%' font-family='sans-serif' font-size='12' fill='#6b7280' " +
      "text-anchor='middle' dominant-baseline='middle'>photo unavailable</text></svg>",
  );

function photoImg(url) {
  const img = el("img", { src: url, alt: "Evidence photo for this proposed pin move", loading: "lazy" });
  img.addEventListener("error", () => {
    if (img.dataset.fellBack) return;
    img.dataset.fellBack = "1";
    img.src = BROKEN_IMG;
  });
  return img;
}

// ~5 m in degrees — below this the current pin and the immutable origin are the same spot for display.
const COORD_EPS = 5e-5;

function disableActions(card) {
  card.querySelectorAll("button").forEach((b) => { b.disabled = true; });
}
function enableActions(card) {
  card.querySelectorAll("button").forEach((b) => { b.disabled = false; });
}

function dropCard(card) {
  card.remove();
  const remaining = listEl.querySelectorAll(".move-card").length;
  if (countEl) countEl.textContent = remaining ? `${remaining} awaiting review` : "";
  if (!remaining) listEl.appendChild(emptyState());
}

function emptyState() {
  return el("div", { class: "empty-state" }, [
    el("p", { text: "Nothing awaiting review — the photo-move queue is clear. ✓" }),
  ]);
}

async function onApprove(card, imgId) {
  disableActions(card);
  try {
    await applyMove(imgId);
    toast("Move applied — the pin was updated.", "success");
    dropCard(card);
  } catch (e) {
    enableActions(card);
    if (e.status === 409 && e.error?.code === "too_far") {
      toast("This move now exceeds the 2 km cap — reject it instead.", "error");
    } else if (e.status === 409) {
      toast("Already resolved elsewhere.", "info");
      dropCard(card);
    } else if (e.status === 404) {
      // apply-move only 404s from the operator gate (not_pending is a 409), so this is auth.
      flagAuthLost();
    } else {
      reportError(e, "Couldn't apply the move.");
    }
  }
}

async function onReject(card, imgId) {
  disableActions(card);
  try {
    await rejectMove(imgId);
    toast("Move rejected — the pin is unchanged.", "success");
    dropCard(card);
  } catch (e) {
    enableActions(card);
    if (e.status === 404) {
      // reject-move 404s for BOTH a bad token AND an already-resolved row. We listed it as pending a
      // moment ago, so treat it as a race (already resolved) and drop the card; a truly-bad token
      // will surface on the next Refresh (its list call 404s -> auth gate).
      toast("Already resolved elsewhere.", "info");
      dropCard(card);
    } else {
      reportError(e, "Couldn't reject the move.");
    }
  }
}

function moveCard(m) {
  const origin = { lat: m.origin_lat, lon: m.origin_lon };
  const suggested = { lat: m.suggested_lat, lon: m.suggested_lon };
  const moved = Number.isFinite(m.current_lat)
    && (Math.abs(m.current_lat - m.origin_lat) > COORD_EPS || Math.abs(m.current_lon - m.origin_lon) > COORD_EPS);

  const badges = el("div", { class: "move-badges" }, [
    el("span", { class: "badge badge-dist", title: "Move distance from the original pin (the 2 km cap anchor)" },
      [`${formatDistance(m.distance_m)} from original`]),
    el("span", { class: "badge", title: "Distinct independent helpful upvoters (excludes the photo's submitter)" },
      [`👥 ${m.independent_voters} voters`]),
    el("span", { class: "badge", title: "Photo score (helpful − not-helpful)" }, [`👍 score ${m.score}`]),
    m.photo_removed ? el("span", { class: "badge badge-warn", title: "The evidence photo was hidden after this queued" }, ["photo hidden"]) : null,
  ]);

  const coords = el("div", { class: "move-coords" }, [
    el("div", {}, [el("span", { class: "coord-key" }, ["Original"]), " ", el("code", { text: formatCoord(m.origin_lat, m.origin_lon) })]),
    moved ? el("div", {}, [el("span", { class: "coord-key" }, ["Current pin"]), " ", el("code", { text: formatCoord(m.current_lat, m.current_lon) })]) : null,
    el("div", {}, [el("span", { class: "coord-key proposed" }, ["Proposed"]), " ", el("code", { text: formatCoord(m.suggested_lat, m.suggested_lon) })]),
  ]);

  const mapDiv = el("div", { class: "move-map", role: "img", "aria-label": "Before/after map: original pin and proposed pin" });

  const title = el("div", { class: "move-title" }, [
    el("a", { href: `/#bin/${m.location_id}`, target: "_blank", rel: "noopener", title: "Open this location on the map" },
      [m.location_name || `Location ${m.location_id}`]),
    el("span", { class: "move-id" }, [`#${m.location_id}`]),
  ]);

  const actions = el("div", { class: "move-actions" }, [
    el("button", { class: "btn primary", type: "button", onClick: (e) => onApprove(e.target.closest(".move-card"), m.image_id) }, ["Approve move"]),
    el("button", { class: "btn danger", type: "button", onClick: (e) => onReject(e.target.closest(".move-card"), m.image_id) }, ["Reject"]),
  ]);

  const photoLink = el("a", { class: "move-photo", href: m.photo_url, target: "_blank", rel: "noopener", title: "Open the full photo" }, [photoImg(m.photo_url)]);

  const card = el("div", { class: "move-card", dataset: { imageId: m.image_id } }, [
    photoLink,
    el("div", { class: "move-main" }, [
      title,
      badges,
      mapDiv,
      coords,
      el("div", { class: "move-meta muted" }, [`Queued ${relativeTime(m.created_at)}`]),
      actions,
    ]),
  ]);

  // Defer the Leaflet init until the card is in the DOM (below), so the container has its CSS height.
  card._mountMap = () => { const map = buildMiniMap(mapDiv, origin, suggested); if (map) maps.push(map); };
  return card;
}

function renderList(moves) {
  listEl.innerHTML = "";
  listEl.removeAttribute("aria-busy");
  if (countEl) countEl.textContent = moves.length ? `${moves.length} awaiting review` : "";
  if (!moves.length) { listEl.appendChild(emptyState()); return; }
  for (const m of moves) {
    const card = moveCard(m);
    listEl.appendChild(card);
    card._mountMap();   // container now laid out -> Leaflet sizes correctly
  }
}

function teardownMaps() {
  maps.forEach((mp) => { try { mp.remove(); } catch (e) { /* already gone */ } });
  maps = [];
}

async function load() {
  teardownMaps();
  listEl.setAttribute("aria-busy", "true");
  listEl.innerHTML = "";
  listEl.appendChild(el("p", { class: "muted loading-line", text: "Loading the review queue…" }));
  try {
    const data = await listPendingMoves();
    renderList(data.pending_moves || []);
  } catch (e) {
    if (isAuthError(e)) { flagAuthLost(); return; }
    listEl.innerHTML = "";
    listEl.removeAttribute("aria-busy");
    listEl.appendChild(el("div", { class: "empty-state" }, [
      el("p", { text: "Couldn't load the queue — the server may be unreachable." }),
      el("button", { class: "btn ghost", type: "button", onClick: load }, ["Retry"]),
    ]));
  }
}

export function render(container) {
  container.innerHTML = "";
  countEl = el("span", { class: "view-count muted" });
  const head = el("div", { class: "view-head" }, [
    el("h2", { text: "Pending photo moves" }),
    countEl,
    el("button", { class: "btn ghost view-refresh", type: "button", onClick: load }, ["↻ Refresh"]),
  ]);
  listEl = el("div", { class: "moves-list", "aria-busy": "true" });
  container.appendChild(head);
  container.appendChild(el("p", { class: "view-lead muted", text:
    "Community-proposed pin moves of 250 m–2 km, held for a human. Approve to move the pin (revertible), or reject to leave it." }));
  container.appendChild(listEl);
  load();
}

export function teardown() {
  teardownMaps();
  listEl = countEl = null;
}
