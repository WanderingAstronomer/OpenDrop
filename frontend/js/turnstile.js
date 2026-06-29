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
  const label = btn ? btn.textContent : null;
  if (btn) {
    btn.disabled = true;
    if (configured()) btn.textContent = "Verifying…";
  }
  try {
    const token = await getToken(host, { action, size });
    return await fn(token);
  } finally {
    if (btn) { btn.disabled = false; if (label != null) btn.textContent = label; }
  }
}
