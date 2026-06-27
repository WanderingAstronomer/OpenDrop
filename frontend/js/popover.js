import { fetchDetail } from "./api.js";
import { bucketColor, bucketLabel, esc, orgTypeLabel } from "./confidence.js";
import { toast } from "./toast.js";
import { mountVote } from "./vote.js";

function addrHtml(a) {
  if (!a) return "";
  const parts = [a.line, [a.city, a.state].filter(Boolean).join(", "), a.postal_code].filter(Boolean);
  return parts.length ? `<div class="addr">📍 ${esc(parts.join(" · "))}</div>` : "";
}

function confHtml(d) {
  return `<div class="conf"><span class="dot" style="background:${bucketColor(d.bucket)}"></span>
    ${bucketLabel(d.bucket)} (${Math.round(d.confidence)}) · 👍 ${d.upvotes} 👎 ${d.denies}</div>`;
}

export async function openPopover(map, marker, id) {
  let d;
  try {
    d = await fetchDetail(id);
  } catch (e) {
    toast("Couldn't load that location", "error");
    return;
  }
  const div = document.createElement("div");
  div.className = "popover";
  div.innerHTML = `<h3>${esc(d.name)}</h3>
    <div class="meta">${esc(orgTypeLabel(d.org_type))}${d.org_name ? ` · ${esc(d.org_name)}` : ""}</div>
    ${addrHtml(d.address)}
    ${d.hours_raw ? `<div class="hours">🕑 ${esc(d.hours_raw)}</div>` : ""}
    <div class="conf-slot">${confHtml(d)}</div>
    <div class="vote-area"></div>`;
  L.popup({ minWidth: 240, maxWidth: 300, autoPan: true })
    .setLatLng(marker.getLatLng())
    .setContent(div)
    .openOn(map);
  mountVote(div.querySelector(".vote-area"), d.id, (u) => {
    const slot = div.querySelector(".conf-slot");
    if (slot) slot.innerHTML = confHtml(u);
  });
}
