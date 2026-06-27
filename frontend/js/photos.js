import { fetchImages, uploadImage, voteImage } from "./api.js";
import { META } from "./config.js";
import { toast } from "./toast.js";

let map = null;
let pendingMarker = null;
let mapClickHandler = null;
let suggested = null;

export function setMap(m) {
  map = m;
}

function modalEl() {
  return document.getElementById("photo-modal");
}
function closeModal() {
  const m = modalEl();
  m.classList.remove("open", "picking");
  m.setAttribute("aria-hidden", "true");
  m.innerHTML = "";
}

/* ---- popover section: top photo + gallery/add buttons ---- */
export async function mountPhotos(host, locId, latlng) {
  host.innerHTML =
    `<div class="ph-top"></div>` +
    `<div class="ph-actions">` +
    `<button class="ph-gallery" type="button">📷 Photos</button>` +
    `<button class="ph-add" type="button">＋ Add photo / fix pin</button></div>`;
  const data = await fetchImages(locId, false);
  const imgs = data.images || [];
  host.querySelector(".ph-gallery").textContent = `📷 Photos (${imgs.length})`;
  host.querySelector(".ph-gallery").onclick = () => openGallery(locId);
  host.querySelector(".ph-add").onclick = () => openUpload(locId, latlng);
  if (imgs.length) {
    const top = host.querySelector(".ph-top");
    top.innerHTML = `<img class="ph-thumb" src="${imgs[0].url}" alt="Photo of this location" loading="lazy" />`;
    top.querySelector("img").onclick = () => openGallery(locId);
  }
}

/* ---- gallery modal (Google-Maps-like) ---- */
async function openGallery(locId, includeLow = false) {
  const m = modalEl();
  m.classList.add("open");
  m.classList.remove("picking");
  m.setAttribute("aria-hidden", "false");
  m.innerHTML =
    `<div class="modal-card photo-card" role="dialog" aria-label="Photos">` +
    `<div class="modal-head"><strong>Photos</strong>` +
    `<label class="ph-low"><input type="checkbox" ${includeLow ? "checked" : ""}/> show low-rated / unverified</label>` +
    `<button class="modal-close" aria-label="Close">✕</button></div>` +
    `<div class="ph-grid"></div></div>`;
  m.querySelector(".modal-close").onclick = closeModal;
  m.querySelector(".ph-low input").onchange = (e) => openGallery(locId, e.target.checked);

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
      (im.is_correction ? `<span class="ph-badge">📍 pin fix${im.applied ? " ✓ applied" : ""}</span>` : "") +
      (im.status !== "visible" ? `<span class="ph-badge low">${im.status}</span>` : "") +
      `<span class="ph-score">👍 ${im.upvotes} 👎 ${im.downvotes}</span></div>` +
      `<div class="ph-votes"><button class="ph-h" type="button">Helpful</button>` +
      `<button class="ph-u" type="button">Not helpful</button></div>`;
    card.querySelector(".ph-h").onclick = () => doVote(im.id, "helpful", locId, includeLow);
    card.querySelector(".ph-u").onclick = () => doVote(im.id, "unhelpful", locId, includeLow);
    grid.appendChild(card);
  });
}

async function doVote(imgId, vote, locId, includeLow) {
  try {
    await voteImage(imgId, vote);
    toast("Thanks — feedback recorded", "success");
    openGallery(locId, includeLow);
  } catch (e) {
    if (e.status === 429) toast("You already rated this photo", "error");
    else toast("Couldn't record your rating", "error");
  }
}

/* ---- upload modal (+ optional click-the-map pin correction) ---- */
function openUpload(locId) {
  const m = modalEl();
  m.classList.add("open");
  m.classList.remove("picking");
  m.setAttribute("aria-hidden", "false");
  suggested = null;
  clearPendingMarker();
  let token = null;
  m.innerHTML =
    `<div class="modal-card photo-card" role="dialog" aria-label="Add a photo">` +
    `<div class="modal-head"><strong>Add a photo</strong><button class="modal-close" aria-label="Close">✕</button></div>` +
    `<div class="ph-upload">` +
    `<input type="file" accept="image/jpeg,image/png,image/webp" class="ph-file" aria-label="Choose a photo" />` +
    `<label class="ph-fix"><input type="checkbox" class="ph-fixchk" /> 📍 The pin is in the wrong spot — let me mark the right place</label>` +
    `<p class="ph-fixhint" hidden>Click the map to drop the corrected pin, then Upload. If enough people find your photo helpful, the pin moves there.</p>` +
    `<div class="ph-ts"></div>` +
    `<div class="modal-actions"><button class="primary ph-submit" type="button">Upload</button>` +
    `<button class="ghost ph-cancel" type="button">Cancel</button></div></div>`;

  const tsEl = m.querySelector(".ph-ts");
  if (window.turnstile && META && META.turnstile_sitekey) {
    try {
      window.turnstile.render(tsEl, { sitekey: META.turnstile_sitekey, size: "compact", callback: (t) => { token = t; } });
    } catch (e) { /* ignore */ }
  }
  const fixChk = m.querySelector(".ph-fixchk");
  fixChk.onchange = () => {
    m.querySelector(".ph-fixhint").hidden = !fixChk.checked;
    if (fixChk.checked) enableMapPick();
    else { disableMapPick(); suggested = null; clearPendingMarker(); }
  };
  m.querySelector(".modal-close").onclick = cancelUpload;
  m.querySelector(".ph-cancel").onclick = cancelUpload;
  m.querySelector(".ph-submit").onclick = async () => {
    const file = m.querySelector(".ph-file").files[0];
    if (!file) { toast("Choose a photo first", "error"); return; }
    if (fixChk.checked && !suggested) { toast("Click the map to mark the corrected location", "error"); return; }
    try {
      await uploadImage(locId, file, token, suggested);
      toast("Photo uploaded — it appears once the community confirms it", "success");
      cancelUpload();
    } catch (e) {
      if (e.status === 403) toast("Please complete the verification", "error");
      else if (e.status === 413) toast("Image too large (max ~6 MB)", "error");
      else if (e.status === 422) toast("Unsupported image type", "error");
      else if (e.status === 429) toast("Daily upload limit reached", "error");
      else toast("Upload failed — try again", "error");
    }
  };
}

function cancelUpload() {
  disableMapPick();
  clearPendingMarker();
  closeModal();
}

// Picking mode: make the modal non-blocking so map clicks pass through.
function enableMapPick() {
  modalEl().classList.add("picking");
  mapClickHandler = (e) => {
    suggested = { lat: e.latlng.lat, lon: e.latlng.lng };
    setPendingMarker(e.latlng);
    toast("Corrected location set — now press Upload", "info");
  };
  map.on("click", mapClickHandler);
}
function disableMapPick() {
  modalEl().classList.remove("picking");
  if (mapClickHandler) { map.off("click", mapClickHandler); mapClickHandler = null; }
}
function setPendingMarker(latlng) {
  clearPendingMarker();
  pendingMarker = L.marker(latlng).addTo(map).bindTooltip("Corrected location").openTooltip();
}
function clearPendingMarker() {
  if (pendingMarker) { map.removeLayer(pendingMarker); pendingMarker = null; }
}
