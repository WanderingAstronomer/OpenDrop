// Community signals — perceived safety, bin condition, and how many bins are here.
//
// These sit alongside the "still here / gone" vote as soft, crowd-sourced context. One rating per
// person per attribute; re-rating overwrites. The rate panel is ONE FORM: picks are local until a
// single Save submits every touched rating in one batched API call/token. Your own ratings are
// remembered locally (localStorage "od-ratings") so revisits show a "You:" chip and pre-selected
// controls — the API returning the caller's own rating is a deferred follow-up.

import { postAttribute } from "./api.js";
import { esc } from "./confidence.js";
import { icon } from "./icons.js";
import { toast } from "./toast.js";
import { guard, verifyFailMessage } from "./turnstile.js";

const SCALES = {
  safety: { ic: "shield", label: "Feels safe", words: { 1: "Uneasy", 2: "Okay", 3: "Safe" }, max: 3 },
  condition: { ic: "broom", label: "Bin condition", words: { 1: "Rough", 2: "Okay", 3: "Clean" }, max: 3 },
  bins: { ic: "box", label: "Bins here", words: { 1: "One", 2: "Two", 3: "Three", 4: "Four+" }, max: 4 },
};
const ORDER = ["safety", "condition", "bins"];
const LS_KEY = "od-ratings";

function loadMine(locId) {
  try { return (JSON.parse(localStorage.getItem(LS_KEY) || "{}") || {})[locId] || {}; }
  catch (e) { return {}; }
}

function storeMine(locId, saved) {
  try {
    const all = JSON.parse(localStorage.getItem(LS_KEY) || "{}") || {};
    all[locId] = saved;
    localStorage.setItem(LS_KEY, JSON.stringify(all));
  } catch (e) { /* private mode — the chip just won't survive a reload */ }
}

function youChip(attr, v) {
  if (v == null) return "";
  const sc = SCALES[attr];
  const label = sc.words ? (sc.words[v] ?? v) : v; // ?? guards legacy bins values > 4 from old storage
  return `<span class="sig-you" title="Your rating">You: ${esc(String(label))}</span>`;
}

function summary(attr, agg, mineV) {
  const sc = SCALES[attr];
  const n = (agg && agg.count) || 0;
  if (!n) return `<span class="sig-empty" role="img" aria-label="No ratings yet">—</span>${youChip(attr, mineV)}`;
  if (attr === "bins") {
    const m = Math.round(agg.median || agg.avg || 0);
    const label = m >= 4 ? "4+ bins" : `~${m} bin${m === 1 ? "" : "s"}`;
    return `<span class="sig-val">${label}</span>` +
      `<span class="sig-n" title="Based on ${n} rating${n === 1 ? "" : "s"}" aria-label="Based on ${n} rating${n === 1 ? "" : "s"}">${n}</span>${youChip(attr, mineV)}`;
  }
  const avg = agg.avg || 0;
  const rounded = Math.max(1, Math.min(sc.max, Math.round(avg)));
  // dots only — no trailing word (varying word widths made the dot columns misalign across rows).
  // The exact rating still reads on hover via the label + count. Bins keep "~N bins" (no dots).
  const segs = [1, 2, 3].map((i) => `<span class="seg-dot ${i <= rounded ? "on" : ""}"></span>`).join("");
  return `<span class="sig-meter" title="${sc.words[rounded]}">${segs}</span>` +
    `<span class="sig-n" title="Based on ${n} rating${n === 1 ? "" : "s"}" aria-label="${sc.words[rounded]}, based on ${n} rating${n === 1 ? "" : "s"}">${n}</span>${youChip(attr, mineV)}`;
}

export function mountSignals(host, d) {
  let agg = d.attributes || {};
  const saved = loadMine(d.id);   // what THIS browser last submitted (local stopgap for "mine")
  const draft = {};               // value picked now; null = queued retraction; absent = untouched

  const allEmpty = () => ORDER.every((a) => !((agg[a] || {}).count));

  function renderSummary() {
    const none = host.querySelector(".sig-none");
    const rows = host.querySelector(".sig-rows");
    if (none) none.hidden = !allEmpty();
    if (rows) rows.hidden = allEmpty();
    host.querySelectorAll(".sig-row").forEach((row) => {
      const attr = row.dataset.attr;
      row.querySelector(".sig-data").innerHTML = summary(attr, agg[attr], saved[attr]);
    });
  }

  const segRow = (attr) => {
    const sc = SCALES[attr];
    const vals = Object.keys(sc.words).map(Number);
    return `
    <div class="rate-grp" data-attr="${attr}">
      <div class="rate-l-row">
        <span class="rate-l" id="rate-l-${attr}"><span class="sig-ic">${icon[sc.ic](14)}</span>${esc(sc.label)}</span>
        <button type="button" class="rate-clear" hidden>Clear</button>
      </div>
      <div class="seg seg-${vals.length}" role="radiogroup" aria-labelledby="rate-l-${attr}">
        ${vals.map((v) => `<button type="button" role="radio" aria-checked="false" tabindex="-1" data-v="${v}">${esc(sc.words[v])}</button>`).join("")}
      </div>
    </div>`;
  };

  host.innerHTML = `
    <div class="sig-title">Community Signals</div>
    <p class="sig-none" hidden>No community ratings yet — be the first.</p>
    <div class="sig-rows">
      ${ORDER.map((attr) => `
        <div class="sig-row" data-attr="${attr}">
          <span class="sig-label"><span class="sig-ic">${icon[SCALES[attr].ic](14)}</span>${esc(SCALES[attr].label)}</span>
          <span class="sig-data"></span>
        </div>`).join("")}
    </div>
    <button class="btn primary sig-rate-toggle rate-cta" type="button" aria-expanded="false"></button>
    <div class="sig-rate" hidden>
      <fieldset class="rate-fields">
        ${segRow("safety")}
        ${segRow("condition")}
        ${segRow("bins")}
      </fieldset>
      <div class="ts sig-ts"></div>
      <div class="rate-foot">
        <button type="button" class="btn quiet danger tiny rate-remove" hidden>Remove my ratings</button>
        <div class="rate-foot-actions">
          <button type="button" class="btn ghost rate-cancel">Cancel</button>
          <button type="button" class="btn primary rate-save" disabled>Save ratings</button>
        </div>
      </div>
    </div>`;

  renderSummary();

  const toggle = host.querySelector(".sig-rate-toggle");
  const panel = host.querySelector(".sig-rate");
  const tsHost = host.querySelector(".sig-ts");
  const fields = host.querySelector(".rate-fields");
  const saveBtn = host.querySelector(".rate-save");
  const removeBtn = host.querySelector(".rate-remove");

  const shown = (attr) => (attr in draft ? draft[attr] : saved[attr] ?? null);
  const dirty = () => ORDER.some((a) => (a in draft) && (draft[a] ?? null) !== (saved[a] ?? null));
  const syncSave = () => { saveBtn.disabled = !dirty(); };
  // One validated rating per person per spot: the entry point reads as CREATE ("Rate this spot")
  // or EDIT ("Edit your rating") depending on whether this browser already submitted one, and an
  // existing rating can be removed outright.
  const hasSaved = () => ORDER.some((a) => saved[a] != null);
  function syncOwnership() {
    const label = hasSaved() ? "Edit Your Rating" : "Click Here to Rate This Spot";
    toggle.innerHTML = `${icon.star(15)} ${label} ${icon.star(15)}`;
    removeBtn.hidden = !hasSaved();
  }

  function syncGroup(attr) {
    const grp = host.querySelector(`.rate-grp[data-attr="${attr}"]`);
    const v = shown(attr);
    grp.querySelector(".rate-clear").hidden = v == null;
    grp.querySelectorAll("[data-v]").forEach((b) => {
      const on = v != null && Number(b.dataset.v) === v;
      b.classList.toggle("chosen", on);
      b.setAttribute("aria-checked", String(on));
      b.tabIndex = on ? 0 : -1;
    });
    // roving tabindex needs one stop even when nothing is chosen
    if (v == null) grp.querySelector("[data-v]").tabIndex = 0;
  }

  function openForm(open) {
    if (open) { panel.removeAttribute("hidden"); toggle.hidden = true; }
    else { panel.setAttribute("hidden", ""); toggle.hidden = false; }
    toggle.setAttribute("aria-expanded", String(open));
  }
  toggle.onclick = () => { ORDER.forEach(syncGroup); openForm(true); };
  host.querySelector(".rate-cancel").onclick = () => {
    ORDER.forEach((a) => delete draft[a]);
    ORDER.forEach(syncGroup);
    syncSave();
    openForm(false);
  };

  // Set (or clear, when v == null) one attribute's draft pick. Clearing a saved rating queues a
  // retraction (draft = null); clearing an unsaved pick just forgets the local draft.
  function setDraft(attr, v) {
    if (v == null) {
      draft[attr] = saved[attr] != null ? null : undefined;
      if (draft[attr] === undefined) delete draft[attr];
    } else {
      draft[attr] = v;
    }
    syncGroup(attr);
    syncSave();
  }

  // Every group (safety, condition, bins) is the same segmented radiogroup: click a segment to pick,
  // click the current pick again to deselect, arrow keys roam. Each row's Clear is scoped to ITS OWN
  // group via grp.querySelector — the previous code reached grp.parentElement, which always returned
  // the fieldset's FIRST clear button (safety's), so condition's Clear was inert and safety's Clear
  // silently cleared condition, with no way to clear safety at all.
  host.querySelectorAll(".rate-grp").forEach((grp) => {
    const attr = grp.dataset.attr;
    grp.querySelectorAll("[data-v]").forEach((b) => {
      b.onclick = () => {
        const v = Number(b.dataset.v);
        setDraft(attr, shown(attr) === v ? null : v); // re-clicking the chosen segment deselects it
      };
      b.onkeydown = (e) => {
        if (e.key !== "ArrowLeft" && e.key !== "ArrowRight") return;
        e.preventDefault();
        const opts = [...grp.querySelectorAll("[data-v]")];
        const i = opts.indexOf(b);
        const next = opts[(i + (e.key === "ArrowRight" ? 1 : opts.length - 1)) % opts.length];
        next.focus();
        next.click();
      };
    });
    grp.querySelector(".rate-clear").onclick = () => setDraft(attr, null);
  });

  async function submitRatings(ratings, successMsg, busyBtn) {
    const label = busyBtn.textContent;
    fields.disabled = true;
    busyBtn.textContent = "Saving…";
    try {
      const res = await guard(tsHost, busyBtn, { action: "rate" }, (token) =>
        postAttribute(d.id, { ratings, turnstile_token: token }));
      agg = res.attributes || agg;
      ratings.forEach((r) => {
        if (r.value == null) delete saved[r.attribute];
        else saved[r.attribute] = r.value;
        delete draft[r.attribute];
      });
      storeMine(d.id, saved);
      renderSummary();
      syncSave();
      syncOwnership();
      openForm(false);
      toast(successMsg, "success");
    } catch (e) {
      if (e.status === 403) toast(verifyFailMessage(), "error");
      else if (e.status === 429) toast("That's today's limit for ratings — try again tomorrow", "info");
      else if (e.status === 404) toast("This spot was removed while you had it open.", "error");
      else {
        console.warn("rating save failed", e);
        toast("Couldn't save your ratings. Check your connection and try again.", "error");
      }
    } finally {
      fields.disabled = false;
      busyBtn.textContent = label;
    }
  }

  saveBtn.onclick = () => {
    const ratings = ORDER
      .filter((a) => (a in draft) && (draft[a] ?? null) !== (saved[a] ?? null))
      .map((attribute) => ({ attribute, value: draft[attribute] ?? null }));
    if (!ratings.length) return;
    const clearingAll = ratings.every((r) => r.value == null);
    submitRatings(ratings, clearingAll ? "Rating cleared." : "Ratings saved — thanks for helping out.", saveBtn);
  };

  // Removing an existing rating is destructive-ish: two-step confirm on the button itself.
  let removeArmed = null;
  removeBtn.onclick = () => {
    if (!removeArmed) {
      removeBtn.textContent = "Remove — are you sure?";
      removeArmed = setTimeout(() => {
        removeArmed = null;
        removeBtn.textContent = "Remove my ratings";
      }, 4000);
      return;
    }
    clearTimeout(removeArmed);
    removeArmed = null;
    removeBtn.textContent = "Remove my ratings";
    const ratings = ORDER.filter((a) => saved[a] != null).map((attribute) => ({ attribute, value: null }));
    if (!ratings.length) return;
    ORDER.forEach((a) => delete draft[a]);
    submitRatings(ratings, "Your ratings were removed.", removeBtn);
  };

  syncOwnership();
}
