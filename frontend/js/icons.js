// The app's single icon language: small inline stroke SVGs — 24 viewBox, stroke-width 2, round
// caps/joins, fill none, stroke currentColor, aria-hidden — matching the locate button's glyph.
// Color always comes from currentColor so both themes work automatically. No emoji anywhere.
const svg = (d, s = 16) =>
  `<svg viewBox="0 0 24 24" width="${s}" height="${s}" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${d}</svg>`;

export const icon = {
  pin: (s) => svg('<path d="M20 10c0 6-8 12-8 12S4 16 4 10a8 8 0 1 1 16 0Z"/><circle cx="12" cy="10" r="3"/>', s),
  clock: (s) => svg('<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>', s),
  check: (s) => svg('<path d="M20 6 9 17l-5-5"/>', s),
  x: (s) => svg('<path d="M18 6 6 18M6 6l12 12"/>', s),
  shield: (s) => svg('<path d="M12 3l7 3v5c0 4.5-3 8-7 10-4-2-7-5.5-7-10V6z"/>', s),
  sparkle: (s) => svg('<path d="M12 4l1.6 4.4L18 10l-4.4 1.6L12 16l-1.6-4.4L6 10l4.4-1.6z"/><path d="M18.5 15.5l.7 2 2 .7-2 .7-.7 2-.7-2-2-.7 2-.7z"/>', s),
  box: (s) => svg('<rect x="4" y="9" width="16" height="11" rx="1.5"/><path d="M4 9l2.5-4h11L20 9"/><path d="M10 13h4"/>', s),
  thumbUp: (s) => svg('<path d="M7 11v9H4a1 1 0 0 1-1-1v-7a1 1 0 0 1 1-1h3zm0 0l4-8a2 2 0 0 1 2 2v4h5a2 2 0 0 1 2 2.3l-1 6A2 2 0 0 1 17 19H7"/>', s),
  directions: (s) => svg('<path d="M9 18l-4 3V6l4-3 6 3 4-3v15l-4 3-6-3z"/><path d="M9 3v15M15 6v15"/>', s),
  external: (s) => svg('<path d="M7 17L17 7M9 7h8v8"/>', s),
  camera: (s) => svg('<rect x="3" y="5" width="18" height="14" rx="2"/><circle cx="9" cy="10" r="1.6"/><path d="M21 15l-5-5-9 9"/>', s),
  lines: (s) => svg('<line x1="4" y1="7" x2="20" y2="7"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="17" x2="13" y2="17"/>', s),
  broom: (s) => svg('<path d="M19 5 11 13"/><path d="M11 9l4 4"/><path d="M13 11l-6 6a2.1 2.1 0 0 1-3-3l6-6"/><path d="M6 15l3 3"/>', s),
  star: (s) => svg('<path d="M12 3l2.5 5.3 5.5.7-4 3.9 1 5.6L12 16.9 7.5 18.5l1-5.6-4-3.9 5.5-.7z"/>', s),
};
