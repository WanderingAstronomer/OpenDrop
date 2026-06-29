// Tiny shared app handles, set once at boot. Lets feature modules (corrections,
// attributes, drop-a-pin) reach the live map + the marker-refresh fn without
// threading them through every call site.
export const app = {
  map: null,
  refresh: () => {},
};
