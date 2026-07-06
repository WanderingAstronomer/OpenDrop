// Operator dashboard entry point: theme wiring, the OPERATOR_TOKEN sign-in gate, and a tiny
// tab router over the three views. Each view is rendered fresh into #content on activation and torn
// down on the way out, so the Leaflet mini-maps in the moves view are always created while visible
// (never zero-sized behind a hidden tab) and are cleaned up on every swap.

import { clearToken, getToken, listPendingMoves, setToken } from "./api.js";
import * as moves from "./moves.js";
import * as reports from "./reports.js";
import * as tools from "./tools.js";
import { isAuthError } from "./ui.js";
import { initTheme } from "../theme.js";
import { toast } from "../toast.js";

const VIEWS = { moves, reports, tools };

let gate, dash, tokenInput, authError, authSubmit, contentEl, signoutBtn, tabButtons;
let current = null;

function showView(name) {
  if (!VIEWS[name]) return;
  if (current && VIEWS[current].teardown) VIEWS[current].teardown();
  current = name;
  tabButtons.forEach((b) => {
    const on = b.dataset.view === name;
    b.classList.toggle("active", on);
    b.setAttribute("aria-selected", on ? "true" : "false");
  });
  VIEWS[name].render(contentEl);
}

function showDashboard() {
  gate.hidden = true;
  dash.hidden = false;
  signoutBtn.hidden = false;
  showView(current || "moves");
}

function showGate() {
  if (current && VIEWS[current].teardown) VIEWS[current].teardown();
  current = null;
  contentEl.innerHTML = "";
  clearToken();
  dash.hidden = true;
  signoutBtn.hidden = true;
  gate.hidden = false;
  try { tokenInput.focus(); } catch (e) { /* not focusable yet */ }
}

async function onSignIn(e) {
  e.preventDefault();
  const t = tokenInput.value.trim();
  if (!t) return;
  setToken(t);
  authError.hidden = true;
  authSubmit.disabled = true;
  authSubmit.textContent = "Checking…";
  try {
    await listPendingMoves();   // the token is validated by the first real operator call
    tokenInput.value = "";
    showDashboard();
  } catch (err) {
    clearToken();
    authError.hidden = false;
    authError.textContent = isAuthError(err)
      ? "Token rejected — or the admin surface is disabled (OPERATOR_TOKEN unset on the server)."
      : "Couldn't reach the server. Try again in a moment.";
  } finally {
    authSubmit.disabled = false;
    authSubmit.textContent = "Sign in";
  }
}

async function boot() {
  initTheme();

  gate = document.getElementById("auth-gate");
  dash = document.getElementById("dashboard");
  tokenInput = document.getElementById("auth-token");
  authError = document.getElementById("auth-error");
  authSubmit = document.getElementById("auth-submit");
  contentEl = document.getElementById("content");
  signoutBtn = document.getElementById("signout");
  tabButtons = Array.from(document.querySelectorAll(".tab"));

  document.getElementById("auth-form").addEventListener("submit", onSignIn);
  signoutBtn.addEventListener("click", () => { toast("Signed out.", "info"); showGate(); });
  tabButtons.forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));

  // A view whose list call 404s (token went bad / OPERATOR_TOKEN cleared server-side) fires this.
  window.addEventListener("admin:auth-lost", () => {
    if (dash.hidden) return;   // already at the gate
    toast("Session ended — sign in again.", "error");
    showGate();
  });

  // Resume a stored session without a dashboard flash: verify the token before revealing anything.
  if (getToken()) {
    try {
      await listPendingMoves();
      showDashboard();
    } catch (e) {
      if (isAuthError(e)) showGate();
      else showDashboard();   // server hiccup, not an auth failure — let the view show its retry
    }
  } else {
    showGate();
  }
}

boot();
