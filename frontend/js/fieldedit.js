// Crowd field corrections — propose a better name / type / owning org / address for a location,
// and vote on others' proposals. Mirrors the pin-correction flow (corrections.js): a sheet to
// propose, an in-popover list to confirm/reject. The DB applies a change once support reaches the
// engagement-tier threshold (migration 0009). Text fields carry no GPS weight, so each voice is 1.

import { fetchOrgs, postFieldCorrection, voteFieldCorrection } from "./api.js";
import { esc, orgTypeLabel } from "./confidence.js";
import { ORG_TYPE_LABELS, ORG_TYPES } from "./config.js";
import { app } from "./state.js";
import { toast } from "./toast.js";
import { guard } from "./turnstile.js";

const FIELDS = [
  { key: "name", label: "Name" },
  { key: "org_type", label: "Type" },
  { key: "org_name", label: "Org" },
  { key: "address", label: "Address" },
];

// Human-readable summary of a proposed change, for the in-popover vote list.
function describe(c) {
  if (c.field === "name") return `Rename to “${esc(c.proposed_value || "")}”`;
  if (c.field === "org_type") return `Set type to “${esc(orgTypeLabel(c.proposed_value))}”`;
  if (c.field === "org_name") return `Set org to “${esc(c.proposed_value || "")}”`;
  if (c.field === "address") {
    const a = c.proposed_address || {};
    const parts = [a.line, [a.city, a.state].filter(Boolean).join(", "), a.postal_code].filter(Boolean);
    return `Update address to ${esc(parts.join(" · "))}`;
  }
  return "Detail change proposed";
}

/* ---- popover section: open detail-change proposals + confirm/reject ---- */
export function mountFieldEdits(host, d) {
  const open = d.open_field_corrections || [];
  if (!open.length) { host.innerHTML = ""; return; }
  host.innerHTML = `<div class="po-fixes">
    <div class="po-fixes-t">Detail changes proposed</div>
    ${open.map((c) => {
      const left = Math.max(0, c.required_support - c.support);
      return `<div class="po-fix" data-cid="${c.id}">
        <div class="fix-what">${describe(c)}</div>
        ${c.note ? `<div class="fix-note">“${esc(c.note)}”</div>` : ""}
        <div class="fix-meter">${left > 0 ? `needs ${left} more` : "ready"} · ${c.support}/${c.required_support}</div>
        <div class="ts fix-ts"></div>
        <div class="fix-btns">
          <button class="btn tiny confirm" type="button">Looks right</button>
          <button class="btn tiny deny" type="button">No</button>
        </div>
      </div>`;
    }).join("")}
  </div>`;

  host.querySelectorAll(".po-fix").forEach((row) => {
    const cid = Number(row.dataset.cid);
    const tsHost = row.querySelector(".fix-ts");
    const onVote = async (confirm, btn) => {
      try {
        const res = await guard(tsHost, btn, { action: "confirm_field" }, (token) =>
          voteFieldCorrection(cid, { confirm, turnstile_token: token }));
        if (res.applied) {
          toast("Updated — thank you!", "success");
          app.refresh();
          app.map.closePopup();
        } else if (res.status === "rejected") {
          toast("Thanks — suggestion dismissed", "success");
          row.remove();
        } else {
          const left = Math.max(0, res.required_support - res.support);
          row.querySelector(".fix-meter").textContent =
            `${left > 0 ? `needs ${left} more` : "ready"} · ${res.support}/${res.required_support}`;
          toast("Thanks — recorded", "success");
        }
      } catch (e) {
        if (e.status === 409 && e.error?.code === "self_vote") toast("You can't vote on your own suggestion", "info");
        else if (e.status === 409) toast("That suggestion is already closed", "info");
        else if (e.status === 403) toast("Please complete the verification", "error");
        else toast("Couldn't record your vote", "error");
      }
    };
    row.querySelector(".confirm").onclick = (e) => onVote(true, e.currentTarget);
    row.querySelector(".deny").onclick = (e) => onVote(false, e.currentTarget);
  });
}

/* ---- proposal sheet ---- */
let sheet = null;
let onKey = null;
let sheetOpener = null;
let orgCache = null;  // organization names for the "whose bin?" dropdown, fetched once

function teardown() {
  if (onKey) { document.removeEventListener("keydown", onKey); onKey = null; }
  if (sheet) { sheet.remove(); sheet = null; }
  const o = sheetOpener;
  sheetOpener = null;
  try { if (o && o.focus) o.focus({ preventScroll: true }); } catch (e) { /* opener gone */ }
}

export function startFieldEdit(d) {
  const opener = document.activeElement;
  teardown();
  sheetOpener = opener;
  let field = "name";

  sheet = document.createElement("div");
  sheet.className = "pin-sheet";
  sheet.setAttribute("role", "dialog");
  sheet.setAttribute("aria-modal", "true");
  sheet.setAttribute("aria-label", "Suggest a detail change");
  sheet.innerHTML = `
    <div class="sheet-card">
      <div class="sheet-head">
        <strong>Suggest a detail change</strong>
        <button class="sheet-x" type="button" aria-label="Cancel">✕</button>
      </div>
      <p class="sheet-sub">Propose a better value — neighbors confirm it, then it updates for everyone.</p>
      <div class="seg fe-seg" role="tablist" aria-label="Which detail">
        ${FIELDS.map((f, i) => `<button type="button" class="${i === 0 ? "on" : ""}" data-field="${f.key}" role="tab" aria-selected="${i === 0}">${f.label}</button>`).join("")}
      </div>
      <div class="fe-input"></div>
      <label class="sheet-note-l" for="fe-note">Note <span class="opt">(optional)</span></label>
      <input id="fe-note" class="sheet-note" maxlength="500" placeholder="e.g. the sign out front says this" />
      <div class="ts sheet-ts"></div>
      <div class="sheet-actions">
        <button class="btn primary fe-submit" type="button">Submit change</button>
        <button class="btn ghost fe-cancel" type="button">Cancel</button>
      </div>
    </div>`;
  document.body.appendChild(sheet);

  const inputHost = sheet.querySelector(".fe-input");
  const tsHost = sheet.querySelector(".sheet-ts");

  function renderOrgInput() {
    const opts = orgCache || [];
    const cur = d.org_name || "";
    const inList = !!cur && opts.includes(cur);
    inputHost.innerHTML = `<label class="fe-l" for="fe-org">Whose donation bin / drive is this?</label>
      <select id="fe-org" class="fe-text">
        ${opts.map((o) => `<option value="${esc(o)}" ${o === cur ? "selected" : ""}>${esc(o)}</option>`).join("")}
        <option value="__new__" ${inList ? "" : "selected"}>+ Add a new org…</option>
      </select>
      <input id="fe-org-new" class="fe-text fe-org-new" maxlength="200" placeholder="Organization or drive name"
             value="${esc(inList ? "" : cur)}" ${inList ? "hidden" : ""} />`;
    const sel = inputHost.querySelector("#fe-org");
    const txt = inputHost.querySelector("#fe-org-new");
    sel.onchange = () => {
      txt.hidden = sel.value !== "__new__";
      if (sel.value === "__new__") txt.focus();
    };
  }

  function renderInput() {
    if (field === "name") {
      inputHost.innerHTML = `<label class="fe-l" for="fe-name">New name</label>
        <input id="fe-name" class="fe-text" maxlength="200" value="${esc(d.name || "")}" />`;
    } else if (field === "org_type") {
      inputHost.innerHTML = `<label class="fe-l" for="fe-type">Type</label>
        <select id="fe-type" class="fe-text">${ORG_TYPES.map((t) =>
          `<option value="${t}" ${t === d.org_type ? "selected" : ""}>${esc(ORG_TYPE_LABELS[t])}</option>`).join("")}</select>`;
    } else if (field === "org_name") {
      renderOrgInput();
    } else {
      const a = d.address || {};
      inputHost.innerHTML = `
        <label class="fe-l" for="fe-line">Street address</label>
        <input id="fe-line" class="fe-text" value="${esc(a.line || "")}" placeholder="123 Main St" />
        <div class="row">
          <div><label class="fe-l" for="fe-city">City</label><input id="fe-city" class="fe-text" value="${esc(a.city || "")}" /></div>
          <div><label class="fe-l" for="fe-state">State</label><input id="fe-state" class="fe-text" maxlength="2" value="${esc(a.state || "")}" placeholder="OH" /></div>
          <div><label class="fe-l" for="fe-zip">ZIP</label><input id="fe-zip" class="fe-text" value="${esc(a.postal_code || "")}" /></div>
        </div>`;
    }
  }

  async function ensureOrgs() {
    if (orgCache) return;
    orgCache = await fetchOrgs();
  }

  renderInput();

  sheet.querySelectorAll(".fe-seg button").forEach((b) => {
    b.onclick = async () => {
      field = b.dataset.field;
      sheet.querySelectorAll(".fe-seg button").forEach((x) => {
        const on = x === b;
        x.classList.toggle("on", on);
        x.setAttribute("aria-selected", String(on));
      });
      if (field === "org_name") await ensureOrgs();
      renderInput();
    };
  });

  sheet.querySelector(".sheet-x").onclick = teardown;
  sheet.querySelector(".fe-cancel").onclick = teardown;
  onKey = (e) => { if (e.key === "Escape") teardown(); };
  document.addEventListener("keydown", onKey);
  try { sheet.querySelector(".sheet-x").focus({ preventScroll: true }); } catch (e) { /* ignore */ }

  sheet.querySelector(".fe-submit").onclick = async (e) => {
    const btn = e.currentTarget;
    const note = sheet.querySelector("#fe-note").value.trim() || null;
    const payload = { field, note };
    if (field === "name") {
      const v = (sheet.querySelector("#fe-name").value || "").trim();
      if (v.length < 2) { toast("Enter a name (2+ characters)", "error"); return; }
      payload.value = v;
    } else if (field === "org_type") {
      payload.value = sheet.querySelector("#fe-type").value;
    } else if (field === "org_name") {
      const sel = sheet.querySelector("#fe-org");
      let v = sel.value;
      if (v === "__new__") v = (sheet.querySelector("#fe-org-new").value || "").trim();
      if (!v || v.length < 2) { toast("Choose or enter an organization", "error"); return; }
      payload.value = v;
    } else {
      const line = (sheet.querySelector("#fe-line").value || "").trim();
      if (!line) { toast("Enter a street address", "error"); return; }
      payload.address = {
        line,
        city: (sheet.querySelector("#fe-city").value || "").trim() || null,
        state: ((sheet.querySelector("#fe-state").value || "").trim().toUpperCase() || null),
        postal_code: (sheet.querySelector("#fe-zip").value || "").trim() || null,
      };
    }
    try {
      const res = await guard(tsHost, btn, { action: "field_correct" }, (token) =>
        postFieldCorrection(d.id, { ...payload, turnstile_token: token }));
      if (res.applied) {
        toast("Updated — thank you!", "success");
        app.refresh();
      } else {
        const left = Math.max(0, res.required_support - res.support);
        toast(left > 0
          ? `Saved — needs ${left} more confirmation${left === 1 ? "" : "s"} to apply`
          : "Saved — awaiting review", "success");
      }
      teardown();
    } catch (err) {
      const code = err.error?.code;
      if (err.status === 422 && code === "no_change") toast("That's already the current value", "info");
      else if (err.status === 422) toast(err.error?.message || "That value can't be used", "error");
      else if (err.status === 409) toast("You already proposed a change to this field", "info");
      else if (err.status === 429) toast("You've hit today's suggestion limit — try again tomorrow", "error");
      else if (err.status === 403) toast("Please complete the verification", "error");
      else if (err.status === 404) { toast("That location is no longer available", "error"); teardown(); }
      else toast("Couldn't submit the change — please try again", "error");
    }
  };
}
