// First-class light + dark theming.
//
// The actual <html data-theme> attribute is set by a tiny inline script in index.html
// BEFORE first paint (no flash). This module owns the runtime behaviour: persisting an
// explicit choice, following the OS while the user hasn't chosen, the ☀/☾ toggle button,
// and the helper that keeps Cloudflare Turnstile widgets in the matching theme.

const KEY = "opendrop_theme"; // "light" | "dark" when the user has chosen; absent => follow OS
const listeners = new Set();

function stored() {
  try { return localStorage.getItem(KEY); } catch (e) { return null; }
}

export function currentTheme() {
  return document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
}

// Cloudflare Turnstile accepts "light" | "dark" | "auto"; we hand it the resolved mode.
export function turnstileTheme() {
  return currentTheme();
}

function apply(theme) {
  document.documentElement.setAttribute("data-theme", theme);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute("content", theme === "dark" ? "#0f1b1e" : "#15616d");
  listeners.forEach((fn) => { try { fn(theme); } catch (e) { /* ignore listener errors */ } });
}

export function onThemeChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export function setTheme(theme, { persist = true } = {}) {
  if (persist) { try { localStorage.setItem(KEY, theme); } catch (e) { /* private mode */ } }
  apply(theme);
}

export function toggleTheme() {
  setTheme(currentTheme() === "dark" ? "light" : "dark");
}

export function initTheme() {
  // Sync derived bits (meta colour, listeners) with whatever the inline boot script set.
  apply(currentTheme());

  // Live-follow the OS only while the user hasn't made an explicit choice.
  try {
    const mq = window.matchMedia("(prefers-color-scheme: dark)");
    const onSys = (e) => { if (!stored()) apply(e.matches ? "dark" : "light"); };
    if (mq.addEventListener) mq.addEventListener("change", onSys);
    else if (mq.addListener) mq.addListener(onSys); // older Safari
  } catch (e) { /* no matchMedia */ }

  // Wire the on-page toggle button if present.
  const btn = document.getElementById("theme-toggle");
  if (btn) {
    const sync = () => {
      const dark = currentTheme() === "dark";
      btn.textContent = dark ? "☀" : "☾";
      const label = dark ? "Switch to light theme" : "Switch to dark theme";
      btn.title = label;
      btn.setAttribute("aria-label", label);
    };
    sync();
    onThemeChange(sync);
    btn.addEventListener("click", toggleTheme);
  }
}
