// Location photos: a thumbnail + count in the popover, and a Google-Maps-like gallery/upload
// modal. Pin corrections now live in their own drag-to-fix flow (corrections.js), so this
// module is purely about photos — no map-picking here anymore.

import { fetchImages, reportImage, uploadImage, voteImage } from "./api.js";
import { toast } from "./toast.js";
import { guard } from "./turnstile.js";

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
  modalKeydown = (e) => { if (e.key === "Escape") closeModal(); };
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
  const data = await fetchImages(locId, false);
  const imgs = data.images || [];
  host.querySelector(".ph-gallery").textContent = `Photos (${imgs.length})`;
  host.querySelector(".ph-gallery").onclick = () => openGallery(locId);
  host.querySelector(".ph-add").onclick = () => openUpload(locId);
  if (imgs.length) {
    const top = host.querySelector(".ph-top");
    top.innerHTML = `<img class="ph-thumb" src="${imgs[0].url}" alt="Open photo gallery" ` +
      `role="button" tabindex="0" loading="lazy" />`;
    const thumb = top.querySelector("img");
    attachFallback(thumb);
    thumb.onclick = () => openGallery(locId);
    thumb.onkeydown = (e) => {
      if (e.key === "Enter" || e.key === " ") { e.preventDefault(); openGallery(locId); }
    };
  }
}

/* ---- gallery modal ---- */
async function openGallery(locId, includeLow = false) {
  const m = modalEl();
  if (!m.classList.contains("open")) modalOpener = document.activeElement;  // capture once per session
  m.classList.add("open");
  m.setAttribute("aria-hidden", "false");
  m.innerHTML =
    `<div class="modal-card photo-card" role="dialog" aria-modal="true" aria-label="Photos">` +
    `<div class="modal-head"><strong>Photos</strong>` +
    `<label class="ph-low"><input type="checkbox" ${includeLow ? "checked" : ""}/> show low-rated / unverified</label>` +
    `<button class="modal-close" aria-label="Close">✕</button></div>` +
    `<div class="ph-grid"></div></div>`;
  m.querySelector(".modal-close").onclick = closeModal;
  m.querySelector(".ph-low input").onchange = (e) => openGallery(locId, e.target.checked);
  armModal(m);

  const grid = m.querySelector(".ph-grid");
  const imgs = (await fetchImages(locId, includeLow)).images || [];
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
      (im.status !== "visible" ? `<span class="ph-badge low">${im.status}</span>` : "") +
      `<span class="ph-score">👍 ${im.upvotes} 👎 ${im.downvotes}</span></div>` +
      `<div class="ts ph-ts"></div>` +
      `<div class="ph-votes"><button class="btn tiny ph-h" type="button">Helpful</button>` +
      `<button class="btn tiny ph-u" type="button">Not helpful</button>` +
      `<button class="btn tiny ph-r" type="button">Report</button></div>` +
      `<div class="ph-report"></div>`;
    attachFallback(card.querySelector("img"));
    const tsHost = card.querySelector(".ph-ts");
    card.querySelector(".ph-h").onclick = (e) => doVote(im.id, "helpful", locId, includeLow, tsHost, e.currentTarget);
    card.querySelector(".ph-u").onclick = (e) => doVote(im.id, "unhelpful", locId, includeLow, tsHost, e.currentTarget);
    card.querySelector(".ph-r").onclick = () =>
      openImageReport(card.querySelector(".ph-report"), im.id, locId, includeLow);
    grid.appendChild(card);
  });
}

// Inline "report this photo" form, revealed under a gallery card. Optional free-text reason; a
// single report just files a complaint, but once enough distinct reporters flag a photo the API
// soft-hides it (reversibly) and returns hidden=true.
function openImageReport(container, imgId, locId, includeLow) {
  if (container.dataset.open) { container.innerHTML = ""; delete container.dataset.open; return; }
  container.dataset.open = "1";
  container.innerHTML =
    `<input class="ph-report-reason" maxlength="500" type="text" ` +
    `placeholder="What's wrong with this photo? (optional)" aria-label="Reason for report" />` +
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
      openGallery(locId, includeLow);
    } catch (err) {
      if (err.status === 403) toast("Please complete the verification first", "error");
      else if (err.status === 429) toast("Daily report limit reached", "error");
      else if (err.status === 422) toast(err.error?.message || "Report rejected", "error");
      else if (err.status === 404) toast("That photo is no longer available", "error");
      else toast("Couldn't file the report", "error");
    }
  };
}

async function doVote(imgId, vote, locId, includeLow, tsHost, btn) {
  try {
    await guard(tsHost, btn, { action: "rate_photo" }, (token) => voteImage(imgId, vote, token));
    toast("Thanks — feedback recorded", "success");
    openGallery(locId, includeLow);
  } catch (e) {
    if (e.status === 403) toast("Please complete the verification first", "error");
    else if (e.status === 429) toast("You already rated this photo", "error");
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
    try {
      await guard(tsHost, e.currentTarget, { action: "upload" }, (token) => uploadImage(locId, file, token, null));
      toast("Photo uploaded — it appears once the community confirms it", "success");
      closeModal();
    } catch (err) {
      if (err.status === 403) toast("Please complete the verification", "error");
      else if (err.status === 413) toast("Image too large (max ~6 MB)", "error");
      else if (err.status === 422) toast("Unsupported image type", "error");
      else if (err.status === 429) toast("Daily upload limit reached", "error");
      else toast("Upload failed — try again", "error");
    }
  };
}
