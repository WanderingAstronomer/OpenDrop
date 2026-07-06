// Shared UI primitives for the operator dashboard.
import { toast } from "../toast.js";

// Tiny hyperscript. STRING children become text nodes (never parsed as HTML), so operator-facing
// user data — location names, report reasons — is XSS-safe by construction without threading esc()
// through every call site. props keys: `class`, `text` (textContent), `html` (trusted static markup
// ONLY), `dataset`, `on<Event>` handlers, or any other key => setAttribute.
export function el(tag, props = {}, children = []) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(props)) {
    if (v == null) continue;
    if (k === "class") node.className = v;
    else if (k === "text") node.textContent = v;
    else if (k === "html") node.innerHTML = v;
    else if (k === "dataset") Object.assign(node.dataset, v);
    else if (k.startsWith("on") && typeof v === "function") node.addEventListener(k.slice(2).toLowerCase(), v);
    else node.setAttribute(k, v);
  }
  for (const c of [].concat(children)) {
    if (c == null || c === false) continue;
    node.appendChild(typeof c === "string" ? document.createTextNode(c) : c);
  }
  return node;
}

// The operator surface 404s (not 401/403) on a wrong/absent token — indistinguishable from a missing
// route (require_operator). On a LIST load that means the token went bad, so bounce to the gate.
export function isAuthError(e) {
  return !!e && (e.status === 404 || e.status === 401 || e.status === 403);
}

export function flagAuthLost() {
  window.dispatchEvent(new CustomEvent("admin:auth-lost"));
}

// Generic action-failure toast. Prefers the server's {error:{message}}; callers add specifics for
// known codes before falling back to this.
export function reportError(e, fallback = "Something went wrong — try again") {
  const msg = (e && e.error && e.error.message) || fallback;
  toast(msg, "error");
}
