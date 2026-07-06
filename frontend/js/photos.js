// Location photos: a thumbnail + count in the popover, and a Google-Maps-like gallery/upload
// modal. Pin corrections now live in their own drag-to-fix flow (corrections.js), so this
// module is purely about photos — no map-picking here anymore.

import { fetchImages, reportImage, uploadImage, voteImage } from "./api.js";
import { mountPotdPlaceholder } from "./potd.js";
import { app } from "./state.js";
import { toast } from "./toast.js";
import { guard, verifyFailMessage } from "./turnstile.js";

function modalEl() {
  return document.getElementById("photo-modal");
}

// Inline placeholder for a photo whose file 404s (e.g. taken down after the page loaded). Keeps
// layout intact instead of showing the browser's broken-image glyph.
const BROKEN_IMG =
  "data:image/svg+xml;utf8," +
  encodeURIComponent(
    "<svg xmlns='http://www.w3.org/2000/svg' width='160' height='120'>" +
      "<rect width='100%' height='100%' fill='#e5e7eb'/>" +
      "<text x='50%' y='50%' font-family='sans-serif' font-size='12' fill='#6b7280' " +
      "text-anchor='middle' dominant-baseline='middle'>photo unavailable</text></svg>",
  );

function attachFallback(img) {
  img.addEventListener("error", () => {
    if (img.dataset.fellBack) return;  // guard against a loop if the placeholder itself "errors"
    img.dataset.fellBack = "1";
    img.src = BROKEN_IMG;
    img.classList.add("ph-broken");
  });
}

// Visible, focusable descendants of a modal card (mirror of submit.js) — recomputed per keystroke so
// the Tab trap always reflects the current DOM after a gallery re-render.
function focusables(root) {
  return Array.from(root.querySelectorAll(
    'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), '
    + 'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
  )).filter((el) => el.offsetParent !== null);
}

let modalOpener = null;   // element to hand focus back to when the modal closes
let modalKeydown = null;  // active Escape handler (removed on close / re-render)

function closeModal() {
  const m = modalEl();
  m.classList.remove("open");
  m.setAttribute("aria-hidden", "true");
  m.innerHTML = "";
  if (modalKeydown) { document.removeEventListener("keydown", modalKeydown); modalKeydown = null; }
  const o = modalOpener;
  modalOpener = null;
  try { if (o && o.focus) o.focus({ preventScroll: true }); } catch (e) { /* opener gone */ }
}

// Arm Escape-to-close and move focus to the close button. Idempotent across re-renders of the
// same modal (the gallery re-renders itself when the "show low-rated" toggle changes).
function armModal(m) {
  if (modalKeydown) document.removeEventListener("keydown", modalKeydown);
  modalKeydown = (e) => {
    if (e.key === "Escape") { closeModal(); return; }
    if (e.key !== "Tab") return;
    // These modals have no interactive map behind them, so aria-modal is honest — trap Tab inside
    // the card. Scope to .modal-card (not the outer shell) and recompute per keystroke so re-renders
    // (toggle/vote/report) need no rewiring.
    const card = m.querySelector(".modal-card");
    if (!card) return;
    const f = focusables(card);
    if (!f.length) return;
    const first = f[0];
    const last = f[f.length - 1];
    if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
    else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
  };
  document.addEventListener("keydown", modalKeydown);
  const c = m.querySelector(".modal-close");
  if (c) try { c.focus({ preventScroll: true }); } catch (e) { /* not focusable yet */ }
}

/* ---- popover section: top photo + gallery/add buttons ---- */
export async function mountPhotos(host, locId) {
  host.innerHTML =
    `<div class="ph-top"></div>` +
    `<div class="ph-actions">` +
    `<button class="btn ghost ph-gallery" type="button">Photos</button>` +
    `<button class="btn ghost ph-add" type="button">Add photo</button></div>`;
  const gallery = host.querySelector(".ph-gallery");
  const add = host.querySelector(".ph-add");
  // Wire handlers BEFORE the await so a slow/failed fetch can never leave the buttons inert.
  gallery.onclick = () => openGallery(locId);
  add.onclick = () => openUpload(locId);
  // Fetch ALL statuses and hide only DOWNVOTED ('hidden') below — a brand-new 'pending' photo now
  // shows here (badged) so it can actually be found and confirmed, instead of dying invisible.
  const data = await fetchImages(locId, true);
  const imgs = (data.images || []).filter((im) => im.status !== "hidden"); // pending + visible
  if (data.failed) {
    gallery.textContent = "Photos";
    const top = host.querySelector(".ph-top");
    top.innerHTML = `<button class="btn ghost ph-retry" type="button">Couldn't load photos — retry</button>`;
    top.querySelector(".ph-retry").onclick = () => mountPhotos(host, locId);
    return;
  }
  gallery.textContent = `Photos (${imgs.length})`;
  const top = host.querySelector(".ph-top");
  if (imgs.length) {
    const first = imgs[0];
    const pending = first.status === "pending"; // top photo still awaiting its first vouch
    top.innerHTML =
      `<div class="ph-thumb-wrap">` +
      `<img class="ph-thumb" src="${first.url}" alt="Open photo gallery" role="button" tabindex="0" loading="lazy" />` +
      (pending ? `<span class="ph-thumb-badge">Unverified · tap to confirm</span>` : "") +
      `</div>`;
    const thumb = top.querySelector("img");
    attachFallback(thumb);
    const open = () => openGallery(locId);
    thumb.onclick = open;
    thumb.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); open(); } };
  } else {
    // No community photos yet: show Wikimedia's picture of the day as an attributed placeholder
    // (no-op if POTD is unavailable). The "Photos (0)" count + "Add photo" button are untouched.
    mountPotdPlaceholder(top);
  }
}

/* ---- gallery modal ---- */
// The modal shell (head, checkbox, close) is built ONCE here; the thumbnails render into .ph-grid
// via renderGrid(). The old code rebuilt the whole shell on every "show low-rated" toggle — which
// re-armed the modal and yanked focus back to the close button each time, so toggling flickered
// rapidly. Now the toggle only re-renders the grid; the shell stays put.
async function openGallery(locId, includeLow = false) {
  const m = modalEl();
  if (!m.classList.contains("open")) modalOpener = document.activeElement;  // capture once per session
  m.classList.add("open");
  m.setAttribute("aria-hidden", "false");
  m.innerHTML =
    `<div class="modal-card photo-card" role="dialog" aria-modal="true" aria-label="Photos">` +
    `<div class="modal-head"><strong>Photos</strong>` +
    `<label class="ph-low"><input type="checkbox" ${includeLow ? "checked" : ""}/> show low-rated photos</label>` +
    `<button class="modal-close" aria-label="Close">✕</button></div>` +
    `<div class="ph-grid" aria-busy="true"></div></div>`;
  m.querySelector(".modal-close").onclick = closeModal;
  const lowInput = m.querySelector(".ph-low input");
  lowInput.onchange = () => renderGrid(m, locId, lowInput.checked);
  armModal(m);
  await renderGrid(m, locId, includeLow);
}

// Render just the thumbnails into the already-mounted grid. Safe to call repeatedly (on toggle,
// after a vote, after a report) without touching the shell — so nothing flickers or steals focus.
let galleryReq = 0;
async function renderGrid(m, locId, includeLow) {
  const grid = m.querySelector(".ph-grid");
  if (!grid) return;
  const seq = ++galleryReq;
  grid.setAttribute("aria-busy", "true");
  const data = await fetchImages(locId, true); // always fetch all; `includeLow` only reveals DOWNVOTED
  if (seq !== galleryReq || !grid.isConnected) return;  // a newer toggle/refresh superseded this one
  grid.removeAttribute("aria-busy"); // ALWAYS clear busy on the live path — never hang on a failure
  grid.innerHTML = "";
  if (data.failed) {
    grid.innerHTML = `<p class="ph-empty">Couldn't load photos.</p>` +
      `<div class="ph-actions"><button class="btn ghost ph-retry" type="button">Retry</button></div>`;
    grid.querySelector(".ph-retry").onclick = () => renderGrid(m, locId, includeLow);
    return;
  }
  const imgs = (data.images || []).filter((im) => includeLow || im.status !== "hidden");
  if (!imgs.length) {
    grid.innerHTML = `<p class="ph-empty">No photos yet — add the first one.</p>`;
    return;
  }

  imgs.forEach((im) => {
    const card = document.createElement("div");
    card.className = "ph-card-item";
    card.innerHTML =
      `<a href="${im.url}" target="_blank" rel="noopener"><img src="${im.url}" alt="Location photo" loading="lazy" /></a>` +
      `<div class="ph-meta">` +
      (im.is_correction ? `<span class="ph-badge">Pin fix${im.applied ? " ✓ applied" : ""}</span>` : "") +
      (im.status === "pending" ? `<span class="ph-badge">Unverified — help confirm</span>` : "") +
      (im.status === "hidden" ? `<span class="ph-badge low">Low-rated</span>` : "") +
      `<span class="ph-score">👍 ${im.upvotes} 👎 ${im.downvotes}</span></div>` +
      `<div class="ts ph-ts"></div>` +
      `<div class="ph-votes"><button class="btn tiny ph-h" type="button">Helpful</button>` +
      `<button class="btn tiny ph-u" type="button">Not helpful</button>` +
      `<button class="btn tiny ph-r" type="button">Report</button></div>` +
      `<div class="ph-report"></div>`;
    attachFallback(card.querySelector("img"));
    const tsHost = card.querySelector(".ph-ts");
    const helpfulBtn = card.querySelector(".ph-h");
    const unhelpfulBtn = card.querySelector(".ph-u");
    helpfulBtn.onclick = () => doVote(im.id, "helpful", m, locId, includeLow, tsHost, helpfulBtn, unhelpfulBtn);
    unhelpfulBtn.onclick = () => doVote(im.id, "unhelpful", m, locId, includeLow, tsHost, unhelpfulBtn, helpfulBtn);
    card.querySelector(".ph-r").onclick = () =>
      openImageReport(card.querySelector(".ph-report"), im.id, m, locId, includeLow);
    grid.appendChild(card);
  });
}

// Inline "report this photo" form, revealed under a gallery card. Optional free-text reason; a
// single report just files a complaint, but once enough distinct reporters flag a photo the API
// soft-hides it (reversibly) and returns hidden=true.
function openImageReport(container, imgId, m, locId, includeLow) {
  if (container.dataset.open) { container.innerHTML = ""; delete container.dataset.open; return; }
  container.dataset.open = "1";
  container.innerHTML =
    `<textarea class="ph-report-reason" maxlength="500" rows="3" ` +
    `placeholder="What's wrong with this photo? (optional)" aria-label="Reason for report"></textarea>` +
    `<div class="ts ph-report-ts"></div>` +
    `<div class="ph-report-actions">` +
    `<button class="btn tiny danger ph-report-send" type="button">Send report</button>` +
    `<button class="btn tiny ph-report-cancel" type="button">Cancel</button></div>`;
  const tsHost = container.querySelector(".ph-report-ts");
  container.querySelector(".ph-report-cancel").onclick = () => { container.innerHTML = ""; delete container.dataset.open; };
  container.querySelector(".ph-report-send").onclick = async (e) => {
    const reason = container.querySelector(".ph-report-reason").value.trim();
    try {
      const d = await guard(tsHost, e.currentTarget, { action: "report" },
        (token) => reportImage(imgId, { reason: reason || null, turnstile_token: token }));
      toast(d.hidden ? "Reported — photo hidden pending review" : "Thanks — reported for review", "success");
      renderGrid(m, locId, includeLow);
    } catch (err) {
      if (err.status === 403) toast(verifyFailMessage(), "error");
      else if (err.status === 429) toast("Daily report limit reached", "error");
      else if (err.status === 422) toast(err.error?.message || "Report rejected", "error");
      else if (err.status === 404) toast("That photo is no longer available", "error");
      else toast("Couldn't file the report", "error");
    }
  };
}

async function doVote(imgId, vote, m, locId, includeLow, tsHost, btn, otherBtn) {
  if (otherBtn) otherBtn.disabled = true; // latch the sibling vote button; guard() disables `btn`
  try {
    const res = await guard(tsHost, btn, { action: "rate_photo" }, (token) => voteImage(imgId, vote, token));
    if (res && res.applied) {
      // The vote pushed a photo-validated pin correction over its threshold: the location MOVED.
      // Bust the over-fetch cache so the pin renders at its new position (mirrors panel.js).
      toast("Photo confirmed the fix — pin updated!", "success");
      app.refresh();
    } else {
      toast("Thanks — feedback recorded", "success");
    }
    renderGrid(m, locId, includeLow); // rebuilds the card (fresh buttons) — no manual restore on success
  } catch (e) {
    if (otherBtn) otherBtn.disabled = false; // restore the sibling for retry (guard() restored `btn`)
    if (e.status === 403) toast(verifyFailMessage(), "error");
    // 409 = self_vote: you can't vouch for your own photo (same IP as the uploader). Say so plainly —
    // the old generic "couldn't record your rating" read as a server failure. NOTE: a phone upload and
    // a desktop vote on the SAME network share a public IP, so this fires even across your own devices.
    else if (e.status === 409) toast("You can't confirm your own photo — someone else has to vouch for it", "error");
    else if (e.status === 429) toast("You already rated this photo", "error");
    else if (e.status === 404) toast("That photo is no longer available", "error");
    else toast("Couldn't record your rating", "error");
  }
}

/* ---- upload modal ---- */
function openUpload(locId) {
  const m = modalEl();
  if (!m.classList.contains("open")) modalOpener = document.activeElement;
  m.classList.add("open");
  m.setAttribute("aria-hidden", "false");
  m.innerHTML =
    `<div class="modal-card photo-card" role="dialog" aria-modal="true" aria-label="Add a photo">` +
    `<div class="modal-head"><strong>Add a photo</strong><button class="modal-close" aria-label="Close">✕</button></div>` +
    `<div class="ph-upload">` +
    `<input type="file" accept="image/jpeg,image/png,image/webp" class="ph-file" aria-label="Choose a photo" />` +
    `<p class="ph-hint">A clear photo of the bin or storefront helps others find and trust it. ` +
    `Wrong spot? Close this and use “Fix location” to move the pin.</p>` +
    `<div class="ts ph-ts"></div>` +
    `<div class="modal-actions"><button class="btn primary ph-submit" type="button">Upload</button>` +
    `<button class="btn ghost ph-cancel" type="button">Cancel</button></div>` +
    `<p class="consent-line">Upload only photos you have the right to share — no people without consent. ` +
    `Location metadata is stripped automatically. By uploading you agree to the ` +
    `<a href="/terms.html" target="_blank" rel="noopener">Terms</a> and ` +
    `<a href="/privacy.html" target="_blank" rel="noopener">Privacy Notice</a>.</p></div>`;

  const tsHost = m.querySelector(".ph-ts");
  m.querySelector(".modal-close").onclick = closeModal;
  m.querySelector(".ph-cancel").onclick = closeModal;
  armModal(m);
  m.querySelector(".ph-submit").onclick = async (e) => {
    const file = m.querySelector(".ph-file").files[0];
    if (!file) { toast("Choose a photo first", "error"); return; }
    const MAX_UPLOAD_BYTES = 6_000_000; // mirror of backend settings.image_max_bytes (413 stays source of truth)
    if (file.size > MAX_UPLOAD_BYTES) {
      toast("Image too large (max ~6 MB) — pick a smaller photo", "error");
      return; // fail fast instead of uploading multi-MB over LTE only to 413
    }
    const btn = e.currentTarget;
    try {
      await guard(tsHost, btn, { action: "upload" }, (token) => {
        // Token is minted; the multi-MB POST is the slow part — relabel so the button isn't stuck
        // on "Verifying…". guard() restores the original label in its finally.
        btn.textContent = "Uploading…";
        return uploadImage(locId, file, token, null);
      });
      toast("Photo added — it shows now, marked unverified until the community confirms it", "success");
      openGallery(locId); // swap the upload form for the gallery so they see their photo (pending) right away
    } catch (err) {
      if (err.status === 403) toast(verifyFailMessage(), "error");
      else if (err.status === 413) toast("Image too large (max ~6 MB)", "error");
      else if (err.status === 507) toast("Photo storage is temporarily full — please try again later", "error");
      else if (err.status === 422) toast("Unsupported image type", "error");
      else if (err.status === 429) toast("Daily upload limit reached", "error");
      else toast("Upload failed — try again", "error");
    }
  };
}
