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

function onKeydown(e) {
  if (e.key === "Escape") closePanel(document.getElementById("submit-panel"));
}

function openPanel(panel) {
  token = null;
  panel.innerHTML = `<h2 tabindex="-1">Add a donation location</h2>
    <label for="f-name">Name</label><input id="f-name" autocomplete="organization" placeholder="e.g. St. Mark's clothing closet" />
    <label for="f-type">Type</label><select id="f-type">${optionsHtml()}</select>
    <label for="f-line">Street address</label><input id="f-line" autocomplete="address-line1" placeholder="123 Main St" />
    <div class="row">
      <div><label for="f-city">City</label><input id="f-city" autocomplete="address-level2" /></div>
      <div><label for="f-state">State</label><input id="f-state" maxlength="2" autocomplete="address-level1" placeholder="OH" /></div>
      <div><label for="f-zip">ZIP</label><input id="f-zip" inputmode="numeric" autocomplete="postal-code" pattern="\\d{5}(-\\d{4})?" /></div>
    </div>
    <div class="ts" style="margin-top:10px"></div>
    <div class="actions"><button class="primary" id="f-submit" type="button">Submit</button><button class="ghost" id="f-cancel" type="button">Cancel</button></div>`;
  panel.classList.remove("hidden");
  panel.setAttribute("aria-hidden", "false");

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
  document.addEventListener("keydown", onKeydown);
  panel.querySelector("#f-name").focus(); // move focus into the dialog
}

function closePanel(panel) {
  panel.classList.add("hidden");
  panel.setAttribute("aria-hidden", "true");
  panel.innerHTML = "";
  document.removeEventListener("keydown", onKeydown);
  const addBtn = document.getElementById("add-btn");
  if (addBtn) addBtn.focus(); // restore focus to the trigger
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
