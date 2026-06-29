// Community signals — perceived safety, bin condition, and how many bins are here.
//
// These sit alongside the "still here / gone" vote as soft, crowd-sourced context. One rating
// per person per attribute; re-rating overwrites. Ratings save the moment you tap (each is a
// deliberate act), revealing the lazy Turnstile right under the controls.

import { deleteAttribute, postAttribute } from "./api.js";
import { esc } from "./confidence.js";
import { toast } from "./toast.js";
import { guard } from "./turnstile.js";

const SCALES = {
  safety: { icon: "🛡️", label: "Feels safe", words: { 1: "Sketchy", 2: "Okay", 3: "Great" }, max: 3 },
  condition: { icon: "🧹", label: "Bin condition", words: { 1: "Worn", 2: "Okay", 3: "Tidy" }, max: 3 },
  bins: { icon: "🗑️", label: "Bins here", words: null, max: 50 },
};
const ORDER = ["safety", "condition", "bins"];

function summary(attr, agg) {
  const sc = SCALES[attr];
  const n = (agg && agg.count) || 0;
  if (!n) return `<span class="sig-empty">no reports yet</span>`;
  if (attr === "bins") {
    const m = Math.round(agg.median || agg.avg || 0);
    return `<span class="sig-val">~${m} bin${m === 1 ? "" : "s"}</span><span class="sig-n">${n}</span>`;
  }
  const avg = agg.avg || 0;
  const rounded = Math.max(1, Math.min(sc.max, Math.round(avg)));
  const segs = [1, 2, 3].map((i) =>
    `<span class="seg-dot ${i <= rounded ? "on" : ""}"></span>`).join("");
  return `<span class="sig-meter">${segs}</span><span class="sig-val">${sc.words[rounded]}</span><span class="sig-n">${n}</span>`;
}

export function mountSignals(host, d) {
  let agg = d.attributes || {};

  function renderSummary() {
    host.querySelectorAll(".sig-row").forEach((row) => {
      const attr = row.dataset.attr;
      const sc = SCALES[attr];
      row.querySelector(".sig-data").innerHTML = summary(attr, agg[attr]);
      void sc;
    });
  }

  host.innerHTML = `
    <div class="sig-title">Community signals</div>
    <div class="sig-rows">
      ${ORDER.map((attr) => `
        <div class="sig-row" data-attr="${attr}">
          <span class="sig-label">${SCALES[attr].icon} ${esc(SCALES[attr].label)}</span>
          <span class="sig-data"></span>
        </div>`).join("")}
    </div>
    <button class="sig-rate-toggle" type="button" aria-expanded="false">＋ Rate this spot</button>
    <div class="sig-rate" hidden>
      <div class="rate-grp" data-attr="safety">
        <span class="rate-l" id="rate-l-safety">${SCALES.safety.icon} Feels safe</span>
        <div class="seg" role="group" aria-labelledby="rate-l-safety">${[1, 2, 3].map((v) => `<button type="button" data-v="${v}" aria-pressed="false">${SCALES.safety.words[v]}</button>`).join("")}</div>
      </div>
      <div class="rate-grp" data-attr="condition">
        <span class="rate-l" id="rate-l-condition">${SCALES.condition.icon} Condition</span>
        <div class="seg" role="group" aria-labelledby="rate-l-condition">${[1, 2, 3].map((v) => `<button type="button" data-v="${v}" aria-pressed="false">${SCALES.condition.words[v]}</button>`).join("")}</div>
      </div>
      <div class="rate-grp" data-attr="bins">
        <span class="rate-l">${SCALES.bins.icon} Bins here</span>
        <div class="stepper">
          <button type="button" class="dec" aria-label="Fewer">−</button>
          <output class="bins-out">1</output>
          <button type="button" class="inc" aria-label="More">＋</button>
          <button type="button" class="bins-save">Save</button>
        </div>
      </div>
      <p class="rate-hint">Tap your choice again to clear it.</p>
      <div class="ts sig-ts"></div>
    </div>`;

  renderSummary();

  const toggle = host.querySelector(".sig-rate-toggle");
  const panel = host.querySelector(".sig-rate");
  const tsHost = host.querySelector(".sig-ts");
  toggle.onclick = () => {
    const open = panel.hasAttribute("hidden");
    if (open) panel.removeAttribute("hidden"); else panel.setAttribute("hidden", "");
    toggle.setAttribute("aria-expanded", String(open));
    toggle.textContent = open ? "Hide rating" : "＋ Rate this spot";
  };

  let saving = false;
  const mine = {};  // this session's own pick per attribute, so we know what "tap again" clears

  function markChosen(attribute, value) {
    const grp = host.querySelector(`.rate-grp[data-attr="${attribute}"]`);
    if (!grp) return;
    grp.querySelectorAll("[data-v]").forEach((b) => {
      const on = value != null && Number(b.dataset.v) === value;
      b.classList.toggle("chosen", on);
      b.setAttribute("aria-pressed", String(on));
    });
  }

  async function save(attribute, value, btn) {
    if (saving) return;  // one rating at a time — every control shares a single Turnstile host
    saving = true;
    try {
      const res = await guard(tsHost, btn, { action: "rate" }, (token) =>
        postAttribute(d.id, { attribute, value, turnstile_token: token }));
      agg = res.attributes || agg;
      mine[attribute] = value;
      renderSummary();
      markChosen(attribute, value);
      toast("Thanks — recorded", "success");
    } catch (e) {
      if (e.status === 403) toast("Please complete the verification", "error");
      else if (e.status === 422) toast("That value is out of range", "error");
      else if (e.status === 404) toast("That location is no longer available", "error");
      else toast("Couldn't record your rating", "error");
    } finally {
      saving = false;
    }
  }

  async function clearRating(attribute, btn) {
    if (saving) return;
    saving = true;
    try {
      const res = await guard(tsHost, btn, { action: "rate" }, (token) =>
        deleteAttribute(d.id, attribute, token));
      agg = res.attributes || agg;
      mine[attribute] = undefined;
      renderSummary();
      markChosen(attribute, null);
      toast("Rating cleared", "info");
    } catch (e) {
      if (e.status === 403) toast("Please complete the verification", "error");
      else if (e.status === 404) toast("That location is no longer available", "error");
      else toast("Couldn't clear your rating", "error");
    } finally {
      saving = false;
    }
  }

  host.querySelectorAll('.rate-grp[data-attr="safety"] [data-v], .rate-grp[data-attr="condition"] [data-v]')
    .forEach((b) => {
      b.onclick = () => {
        const attr = b.closest(".rate-grp").dataset.attr;
        const v = Number(b.dataset.v);
        if (mine[attr] === v) clearRating(attr, b);  // tap the chosen segment again to deselect
        else save(attr, v, b);
      };
    });

  const out = host.querySelector(".bins-out");
  let bins = 1;
  const setBins = (n) => { bins = Math.max(1, Math.min(SCALES.bins.max, n)); out.textContent = bins; };
  host.querySelector(".dec").onclick = () => setBins(bins - 1);
  host.querySelector(".inc").onclick = () => setBins(bins + 1);
  host.querySelector(".bins-save").onclick = (e) => save("bins", bins, e.currentTarget);
}
