// OpenDrop Phase 1 — OSM Data Audit
// Region: Columbus, OH metro (bbox south,west,north,east)
// Captures the donation-relevant feature classes named in AGENTS.md plus
// adjacent classes (second_hand, recycling:shoes) to measure schema gaps.
[out:json][timeout:90];
(
  nwr["shop"="charity"](39.80,-83.25,40.18,-82.75);
  nwr["shop"="second_hand"](39.80,-83.25,40.18,-82.75);
  nwr["amenity"="recycling"]["recycling:clothes"](39.80,-83.25,40.18,-82.75);
  nwr["amenity"="recycling"]["recycling:shoes"](39.80,-83.25,40.18,-82.75);
);
out center tags;
