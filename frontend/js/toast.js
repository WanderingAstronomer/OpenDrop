// Toasts: errors are real alerts (role=alert), linger 7s, and pause while hovered so they can
// actually be read; info/success keep the quick 4s. Kinds: "info" | "success" | "error".
export function toast(message, kind = "info") {
  const host = document.getElementById("toast-host");
  if (!host) return;
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  if (kind === "error") el.setAttribute("role", "alert");
  host.appendChild(el);
  requestAnimationFrame(() => el.classList.add("show"));
  const ttl = kind === "error" ? 7000 : 4000;
  let t = setTimeout(hide, ttl);
  el.onmouseenter = () => clearTimeout(t);
  el.onmouseleave = () => { t = setTimeout(hide, 2000); };
  // Touch has no hover: a tap pauses the auto-dismiss and grants a fresh window so a long error
  // message can be finished before it self-dismisses.
  el.addEventListener("touchstart", () => { clearTimeout(t); t = setTimeout(hide, 5000); }, { passive: true });
  function hide() {
    el.classList.remove("show");
    setTimeout(() => el.remove(), 300);
  }
}
