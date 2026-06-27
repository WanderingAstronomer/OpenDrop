# OpenDrop Deduplication Scoping Report — Columbus, OH

Empirical scoping of the dedup problem using **real Columbus data on both sides**.
All numbers below were produced this run by `dedup_sample.py` against the two files
in this directory. No claim rests on memory.

## Data sets

**Set A — OSM (`osm_columbus_flat.json`)**: 42 features. Of these, **11 normalize to the
`goodwill` brand** and **3 to `salvation army`**. (Already on disk; provided.)

**Set B — Goodwill national locator (`org_feed_columbus.json`)**: **33 records, live-fetched
this run.** This is the strong outcome — it is genuine live data, not constructed.

### How Set B was obtained (live)
The goodwill.org `/locator/` page is an Elementor/WordPress page using the
`500d-goodwill-store-locator` plugin. The browser calls a WP `admin-ajax` action over GET:

```
GET https://www.goodwill.org/wp-admin/admin-ajax.php
    ?action=gwlf_get_locations
    &security=<nonce>      # read live from window.gwlfGlobal.nonce on /locator/
    &lat=39.96&lng=-82.99  # Columbus (zip 43215 context)
    &radius=25&cats=
```

Discovery path (all observed this run):
- `/locator/` inline JS exposes `gwlfGlobal = {ajaxUrl, nonce, pluginUrl}`.
- `…/500d-goodwill-store-locator/shortcodes/location-finder/location-finder.js`
  shows the exact request: **type GET**, nonce param named **`security`** (not `nonce`),
  params `action,security,lat,lng,radius,cats`; response is `response.data.data` (array).
- First attempt (`POST`, param `nonce=`) returned WordPress `-1` (bad referer/method).
  Correcting to **GET + `security=`** returned a 37 KB JSON body with 33 locations. high confidence.

Each record has `LocationLatitude1/Longitude1`, full street address, `calcd_ServicesOffered`,
and `ci_servD` (donation-accepted flag). 31 of 33 are donation-capable.

### Salvation Army endpoint (probed, documented, not used as Set B)
`satruck.org` runs ASP.NET MVC with a live Web API under `/apiservices/*`
(`zipinfo`, `pickup`, `trackthetruck`, `vehicle`, `donor`). The homepage JS references
`api.GetZipInfo` and an `/apiservices/zipinfo` controller; the route is live (it returned
an ASP.NET "no matching HTTP resource for this method" JSON rather than a 404 host error),
but the exact action signature was not pinned down this run. Goodwill was the preferred
target and yielded clean live data, so SA was left as a documented secondary lead.

## Name normalization rules (implemented in `dedup_sample.py`)
- Lowercase; strip `®`/`™`, then strip all non-alphanumerics.
- Remove noise phrases: `donation center`, `family store`, `thrift store/shop`,
  `retail store`, `outlet (store)`, `shopping center`, `store`, `shop`, `center`,
  `donation` (longest-match-first).
- Remove stopwords: `the, inc, llc, co, of, and, &, at` and street-type tokens.
- **Brand canonicalization** (the load-bearing rule): if `name` OR `operator` matches
  Goodwill / Salvation Army / Volunteers of America / Habitat for Humanity / Ohio Thrift,
  collapse to the single brand token. This is what lets OSM `name="Goodwill"` /
  `operator="Goodwill Columbus"` match feed `name="Whitehall Retail Store"` — both → `goodwill`.
- Similarity = **max(difflib SequenceMatcher ratio, token-set Jaccard)**, range 0–1.
  rapidfuzz `token_set_ratio` is computed as a cross-check column only; the recommendation
  uses the stdlib scorer. (rapidfuzz installed fine this run but is NOT required to run.)

## Threshold sweep — candidate-pair counts (A×B, both gates applied)

| dist (m) \ name-sim | 0.4 | 0.6 | 0.8 |
|---------------------|-----|-----|-----|
| 50                  | 5   | 5   | 5   |
| 100                 | 6   | 6   | 6   |
| 150                 | 7   | 7   | 7   |
| 300                 | 9   | 9   | 9   |
| 500                 | 9   | 9   | 9   |

**Key structural finding: the name-similarity threshold (0.4/0.6/0.8) changes nothing.**
Every Goodwill↔Goodwill pair scores **exactly 1.00** after brand canonicalization, and every
non-matching pair collapses to **≤0.24**. There is a wide empty band between 0.24 and 1.00, so
any name cut in `[0.4, 0.8]` behaves identically here. **Distance does all the discrimination
among same-brand pairs.**

## Manual adjudication (ground truth, all 11 Goodwill OSM records)

| OSM id | nearest feed store | dist (m) | verdict |
|--------|--------------------|---------:|---------|
| way/477191125 | Powell Rd./Sawmill Pkwy | 3.8 | TRUE dup |
| way/691754853 | New Albany Donation Center | 6.3 | TRUE dup |
| way/544062387 | Westerville Retail Store (addr 60 E Schrock ✓) | 25.9 | TRUE dup |
| way/745528496 | Canal Winchester Retail Store | 26.0 | TRUE dup |
| way/1369802814 | Renner Road Retail Store | 42.9 | TRUE dup |
| node/6916850536 | Whitehall Retail Store | 97.0 | TRUE dup |
| way/1355194543 | Westerville Goodwill (Northgate) | 112.8 | TRUE dup |
| node/9812853769 | S Hamilton Rd Retail Store | 162.4 | TRUE dup |
| way/426929489 | Sawmill Rd Retail Store (addr 6525 Sawmill ✓) | 260.6 | TRUE dup |
| **way/546696899** | **N High St Retail Store (2550 N High St)** | **513.8** | **TRUE dup — beyond 500m** |
| node/9115712797 | (872 Refugee Rd, 2.5 km) | 2527.0 | NO match — not in feed |

- **10 true duplicates** exist between the two sets. One OSM Goodwill (`node/9115712797`,
  near Brice/Reynoldsburg) has **no feed counterpart within 2.5 km** — a closed/relocated or
  non-affiliated store; correctly a non-match against this feed.
- The 3 Salvation Army OSM records have **no Goodwill within ~1.1 km** (nearest 1135 m),
  so they never enter any candidate gate. Correct.

## False-positive / false-negative behavior

**False positives = 0 at every gate** in the swept ranges (50–500 m × 0.4–0.8). The 9 candidate
pairs at the 300/500 m × 0.4 gate are all genuine same-store duplicates.

**The name gate is still load-bearing — don't drop it.** At a *distance-only* 300 m gate, two
**non-Goodwill** OSM stores sit near a Goodwill and would be wrongly merged:
- `Volunteers of America Thrift` ↔ `872 Refugee Road Store` — 240.3 m, name-sim **0.14**
- `One More Time ETC.` ↔ `Grandview Donation Center` — 279.3 m, name-sim **0.24**

The name gate (≥0.4) rejects both. So name similarity contributes **precision insurance**, not
recall tuning, in this data.

**False negatives by distance gate** (true dups missed):

| gate | true dups caught | missed (FN) |
|------|-----------------:|------------:|
| 50 m  | 5 / 10 | 5 |
| 100 m | 6 / 10 | 4 |
| 150 m | 7 / 10 | 3 |
| 300 m | 9 / 10 | 1 |
| 500 m | 9 / 10 | 1 |
| ~520 m+ | 10 / 10 | 0 |

The single stubborn FN (`way/546696899` ↔ N High St, 513.8 m) is a real duplicate where OSM
placed the node ~half a block from the feed's geocode of the same large store. Catching it needs
distance ~520 m+, which starts to risk precision elsewhere — better handled by an address/parcel
tie-breaker than by widening the radius blindly.

## Recommendation for OpenDrop merge logic

**Primary gate: distance ≤ 300 m AND name-similarity ≥ 0.4** (normalized, brand-canonicalized).

Rationale grounded in this run:
- 300 m captures **9 of 10** true duplicates with **zero** false positives.
- name-sim **0.4** is the safe floor: the real signal gap is 0.24 ↔ 1.00, so 0.4 sits squarely in
  the empty band — it rejects every near-by non-brand neighbor while accepting every brand match.
  Pushing to 0.6/0.8 buys nothing here and risks hurting messier feeds (abbreviations, store
  numbers), so **0.4 is the recommended name threshold**, not a higher one.
- Do **not** raise the distance gate to 500 m+ to chase the last duplicate: it adds the same 9
  pairs and one extra FN remains anyway. Instead add a **tier-2 rule**: distance ≤ 600 m **AND**
  (brand token identical **AND** matching house-number/street token) to recover the N-High-St case
  without lowering precision. OSM address coverage is partial (only ~5 of 11 Goodwill nodes carry
  `addr`), so tier-2 is a recall booster, not the primary key.

**Concrete merge predicate:**
```
is_dup(a, b) :=
   brand(a) == brand(b)  AND
   ( haversine(a,b) <= 300m  AND name_sim(a,b) >= 0.4 )
   OR
   ( haversine(a,b) <= 600m  AND name_sim(a,b) >= 0.4
       AND street_number_match(a,b) )
```

Caveats for generalization: this sample is single-brand (Goodwill) and brand-clean, so name-sim
looks trivially bimodal. On multi-brand merges and feeds with store numbers / suite letters
(e.g. the feed's Reynoldsburg Store and Brice Rd Outlet share one coordinate `39.92778,-82.83281`),
distance alone will *collide distinct stores*, and name-sim becomes the tie-breaker that matters.
Keep the name gate; tune the distance gate per-source if geocode quality varies.
