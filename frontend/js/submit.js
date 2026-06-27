import { postSubmit } from "./api.js";
import { META, ORG_TYPE_LABELS, ORG_TYPES } from "./config.js";
import { toast } from "./toast.js";

let token = null;

export function initSubmitPanel() {
  const panel = document.getElementById("submit-panel");
  const btn = document.getElementById("add-btn");
  btn.onclick = () => (panel.classList.contains("hidden") ? openPanel(panel) : closePanel(panel));
}

function optionsHtml() {
  return ORG_TYPES.map((t) => `<option value="${t}">${ORG_TYPE_LABELS[t]}</option>`).join("");
}

function openPanel(panel) {
  token = null;
  panel.innerHTML = `<h2>Add a donation location</h2>
    <label>Name</label><input id="f-name" placeholder="e.g. St. Mark's clothing closet" />
    <label>Type</label><select id="f-type">${optionsHtml()}</select>
    <label>Street address</label><input id="f-line" placeholder="123 Main St" />
    <div class="row">
      <div><label>City</label><input id="f-city" /></div>
      <div><label>State</label><input id="f-state" maxlength="2" placeholder="OH" /></div>
      <div><label>ZIP</label><input id="f-zip" /></div>
    </div>
    <div class="ts" style="margin-top:10px"></div>
    <div class="actions"><button class="primary" id="f-submit">Submit</button><button class="ghost" id="f-cancel">Cancel</button></div>`;
  panel.classList.remove("hidden");

  const tsEl = panel.querySelector(".ts");
  if (window.turnstile && META && META.turnstile_sitekey) {
    try {
      window.turnstile.render(tsEl, {
        sitekey: META.turnstile_sitekey, size: "compact", callback: (t) => { token = t; },
      });
    } catch (e) { /* ignore */ }
  }
  panel.querySelector("#f-cancel").onclick = () => closePanel(panel);
  panel.querySelector("#f-submit").onclick = () => doSubmit(panel);
}

function closePanel(panel) {
  panel.classList.add("hidden");
  panel.innerHTML = "";
}

function val(panel, sel) {
  const v = panel.querySelector(sel).value.trim();
  return v || null;
}

async function doSubmit(panel) {
  const name = (panel.querySelector("#f-name").value || "").trim();
  if (!name) { toast("Please enter a name", "error"); return; }
  const state = (val(panel, "#f-state") || "");
  const payload = {
    name,
    org_type: panel.querySelector("#f-type").value,
    address: {
      line: val(panel, "#f-line"),
      city: val(panel, "#f-city"),
      state: state ? state.toUpperCase() : null,
      postal_code: val(panel, "#f-zip"),
    },
    turnstile_token: token,
  };
  try {
    const d = await postSubmit(payload);
    if (d.status === "duplicate") toast("That location looks like it already exists — thanks!", "info");
    else if (d.status === "promoted") toast("Added! It appears once the community confirms it", "success");
    else toast("Submitted for review — thank you!", "success");
    closePanel(panel);
  } catch (e) {
    if (e.status === 403) toast("Please complete the verification", "error");
    else if (e.status === 429) toast("Daily submission limit reached", "error");
    else if (e.status === 422) toast("Couldn't locate that address — saved for review", "info");
    else toast("Submission failed — please try again", "error");
  }
}
