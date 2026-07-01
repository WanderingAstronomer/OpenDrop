// Unit tests for the pure presentation helpers in js/confidence.js.
// `esc` is the app's single HTML-escaping primitive — every user-supplied string rendered via
// innerHTML passes through it, so an escape miss is a stored-XSS hole. It gets the most scrutiny.
import { test } from "node:test";
import assert from "node:assert/strict";

import { bucketColor, bucketLabel, orgTypeLabel, esc } from "../js/confidence.js";
import { BUCKET_COLORS, ORG_TYPE_LABELS } from "../js/config.js";

test("bucketColor returns the mapped colour for each known bucket", () => {
  assert.equal(bucketColor("high"), BUCKET_COLORS.high);
  assert.equal(bucketColor("medium"), BUCKET_COLORS.medium);
  assert.equal(bucketColor("low"), BUCKET_COLORS.low);
});

test("bucketColor falls back to the low colour for unknown/empty buckets", () => {
  assert.equal(bucketColor("bogus"), BUCKET_COLORS.low);
  assert.equal(bucketColor(""), BUCKET_COLORS.low);
  assert.equal(bucketColor(undefined), BUCKET_COLORS.low);
  assert.equal(bucketColor(null), BUCKET_COLORS.low);
});

test("bucketLabel maps known buckets and labels everything else 'Unknown'", () => {
  assert.equal(bucketLabel("high"), "High confidence");
  assert.equal(bucketLabel("medium"), "Medium confidence");
  assert.equal(bucketLabel("low"), "Low / unverified");
  assert.equal(bucketLabel("nope"), "Unknown");
  assert.equal(bucketLabel(undefined), "Unknown");
});

test("orgTypeLabel maps known org types and falls back for unknowns", () => {
  // every configured org type resolves to its configured label
  for (const [type, label] of Object.entries(ORG_TYPE_LABELS)) {
    assert.equal(orgTypeLabel(type), label);
  }
  assert.equal(orgTypeLabel("???"), "Donation location");
  assert.equal(orgTypeLabel(undefined), "Donation location");
});

test("esc escapes all five HTML-significant characters", () => {
  assert.equal(esc("&"), "&amp;");
  assert.equal(esc("<"), "&lt;");
  assert.equal(esc(">"), "&gt;");
  assert.equal(esc('"'), "&quot;");
  assert.equal(esc("'"), "&#39;");
});

test("esc neutralises script/attribute injection payloads", () => {
  assert.equal(
    esc('<script>alert("x")</script>'),
    "&lt;script&gt;alert(&quot;x&quot;)&lt;/script&gt;",
  );
  assert.equal(
    esc("<img src=x onerror='steal()'>"),
    "&lt;img src=x onerror=&#39;steal()&#39;&gt;",
  );
});

test("esc escapes a literal entity so it renders as text, not markup", () => {
  // a user who types the characters "&lt;" must see them, not a '<' — so '&' becomes '&amp;'
  assert.equal(esc("&lt;"), "&amp;lt;");
});

test("esc coerces non-strings (null/undefined/number/bool) without throwing", () => {
  assert.equal(esc(null), "");
  assert.equal(esc(undefined), "");
  assert.equal(esc(42), "42");
  assert.equal(esc(0), "0");
  assert.equal(esc(false), "false");
});

test("esc leaves a clean string untouched", () => {
  assert.equal(esc("Goodwill of Central Ohio"), "Goodwill of Central Ohio");
});
