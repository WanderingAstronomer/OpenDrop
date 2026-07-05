// Lazy, on-action Cloudflare Turnstile.
//
// Instead of rendering a widget the moment a panel opens (and minting a token the user may
// never use), we render it only when the user actually commits to an action, await a token,
// then proceed. In dev the CF test sitekey resolves instantly; in prod a managed challenge
// renders inline next to the button the user just pressed — exactly where a gesture is expected.

import { META } from "./config.js";
import { turnstileTheme } from "./theme.js";

function configured() {
  return !!(window.turnstile && META && META.turnstile_sitekey);
}

// Turnstile is EXPECTED (a prod sitekey is configured) but its script isn't on the page — almost
// always an ad/content blocker or a failed CDN load. Distinct from "no sitekey at all" (local dev),
// where resolving a null token is correct because the backend accepts it. When blocked, no write can
// ever succeed, so guard() below rejects up front instead of firing a doomed request that just 403s
// and dead-ends the user on an impossible "finish the security check" instruction.
function blocked() {
  return !!(META && META.turnstile_sitekey) && !window.turnstile;
}

// One user-facing message for every failed/again-needed verification, tuned to WHY it failed: a
// blocked script needs the user to allow the site and reload; a real challenge failure just needs a
// retry. Callers route their 403 branch through this so the wording is correct in both cases.
export function verifyFailMessage() {
  return blocked()
    ? "Couldn't load the security check — turn off any ad or content blocker for this site, then reload and try again."
    : "That security check didn't go through — please try again.";
}

// Render a widget into `host`, resolve with a token (or null when Turnstile isn't configured,
// e.g. local dev without keys). The widget is removed once it resolves so the host is reusable.
export function getToken(host, { action, size = "flexible" } = {}) {
  return new Promise((resolve) => {
    if (!configured()) { resolve(null); return; }
    let done = false;
    let wid = null;
    const finish = (v) => {
      if (done) return;
      done = true;
      try { if (wid !== null) window.turnstile.remove(wid); } catch (e) { /* already gone */ }
      host.classList.remove("ts-live");
      host.innerHTML = "";
      resolve(v);
    };
    host.classList.add("ts-live");
    try {
      wid = window.turnstile.render(host, {
        sitekey: META.turnstile_sitekey,
        theme: turnstileTheme(),
        size,
        action,
        callback: (t) => finish(t),
        "error-callback": () => finish(null),
        "timeout-callback": () => finish(null),
      });
    } catch (e) {
      finish(null);
    }
  });
}

// Convenience wrapper: show a busy state on `btn`, fetch a token into `host`, run `fn(token)`.
// The button stays disabled for the WHOLE operation — token mint AND the awaited `fn` (the POST) —
// so a second click can't fire a duplicate request while the first is still in flight. Restores
// the button once everything settles, regardless of outcome.
export async function guard(host, btn, { action, size } = {}, fn) {
  // A blocked Turnstile can never mint a token — reject before touching the button or the network,
  // so the caller's 403 branch shows one actionable message instead of a doomed round-trip.
  if (blocked()) throw { status: 403, code: "turnstile_unavailable" };
  const label = btn ? btn.textContent : null;
  if (btn) {
    btn.style.minWidth = `${btn.offsetWidth}px`; // pin width so "Verifying…" doesn't reflow the row
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    if (configured()) btn.textContent = "Verifying…";
  }
  try {
    const token = await getToken(host, { action, size });
    return await fn(token);
  } finally {
    if (btn) {
      btn.disabled = false;
      btn.removeAttribute("aria-busy");
      btn.style.minWidth = "";
      if (label != null) btn.textContent = label;
    }
  }
}
