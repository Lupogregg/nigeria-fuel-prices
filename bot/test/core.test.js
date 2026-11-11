import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import { median, parseReport, summarize, validatePrice } from "../src/core.js";

test("parseReport: the documented forms", () => {
  const r = parseReport("/report lagos nnpc 1250");
  assert.equal(r.ok, true);
  assert.deepEqual(
    [r.area.id, r.area.state, r.brand.id, r.brand.category, r.fuel, r.price],
    ["lagos", "lagos", "nnpc", "nnpc", "pms", 1250],
  );

  const d = parseReport("/report ph rainoil 1450 diesel");
  assert.deepEqual([d.area.id, d.area.state, d.brand.id, d.fuel, d.price],
    ["port-harcourt", "rivers", "rainoil", "ago", 1450]);

  const a = parseReport("/report@NaijaFuelBot Abuja Total ₦1,310.50");
  assert.deepEqual([a.area.id, a.area.state, a.brand.id, a.price], ["abuja", "fct", "totalenergies", 1310.5]);
});

test("parseReport: loose word order, multi-word names, bare text", () => {
  assert.equal(parseReport("port harcourt nnpc 1400").area.id, "port-harcourt");
  assert.equal(parseReport("1250 nnpc lagos").area.id, "lagos");
  assert.equal(parseReport("kano petrol aa rano N1300").brand.id, "aa-rano");
  // "11" belongs to the brand, not the price
  const m = parseReport("/report lagos 11 plc 1290");
  assert.deepEqual([m.brand.id, m.price], ["mobil", 1290]);
});

test("parseReport: unknown brands are kept as independent stations", () => {
  const r = parseReport("/report kano danmarna oil 1280");
  assert.equal(r.ok, true);
  assert.deepEqual(r.brand, { id: "other-danmarna-oil", label: "Danmarna Oil", category: "independent" });
});

test("parseReport: errors", () => {
  const err = (t) => parseReport(t).error;
  assert.equal(err("/report"), "empty");
  assert.equal(err("/report lagos nnpc"), "no_price");
  assert.equal(err("/report lagos nnpc 1250 1300"), "many_prices");
  assert.equal(err("/report ibadan nnpc 1250"), "unknown_area");
  assert.equal(err("/report lagos 1250"), "no_brand");
  assert.equal(err("/report lagos petrol diesel nnpc 1250"), "many_fuels");
  assert.equal(err("/report lagos " + "x".repeat(40) + " 1250"), "bad_brand");
});

test("parseReport: markup and odd characters cannot get into stored fields", () => {
  const r = parseReport("/report lagos <b>evil</b>'; DROP;-- 1250");
  assert.equal(r.ok, true);
  assert.match(r.brand.id, /^other-[a-z0-9-]+$/);
  assert.match(r.brand.label, /^[A-Za-z0-9 -]+$/);
});

test("validatePrice: bounds, official baseline, recent reports", () => {
  const baseline = { price: 1561.22, period: "2026-05" };
  assert.equal(validatePrice({ fuel: "pms", price: 150, baseline }).reason, "out_of_bounds");
  assert.equal(validatePrice({ fuel: "pms", price: 9000, baseline }).reason, "out_of_bounds");
  assert.equal(validatePrice({ fuel: "pms", price: 700, baseline }).reason, "far_from_official_average");
  assert.deepEqual(validatePrice({ fuel: "pms", price: 1250, baseline }),
    { ok: true, checked_against: "official_average" });
  // A 20% move since the last NBS month must still be accepted.
  assert.equal(validatePrice({ fuel: "pms", price: 1561.22 * 1.2, baseline }).ok, true);
  // Recent community reports give a tighter check and take precedence.
  assert.equal(validatePrice({ fuel: "pms", price: 1000, baseline, communityMedian: 1600 }).reason,
    "far_from_recent_reports");
  assert.deepEqual(validatePrice({ fuel: "pms", price: 1580, baseline, communityMedian: 1600 }),
    { ok: true, checked_against: "recent_reports" });
  assert.deepEqual(validatePrice({ fuel: "pms", price: 1250, baseline: null, communityMedian: null }),
    { ok: true, checked_against: "bounds_only" });
});

test("median", () => {
  assert.equal(median([]), null);
  assert.equal(median([3, 1, 2]), 2);
  assert.equal(median([1, 2, 3, 4]), 2.5);
});

const row = (reporter, brand, price, at, extra = {}) => ({
  reporter_id: reporter, area: "lagos", fuel: "pms", brand, brand_label: brand.toUpperCase(),
  brand_category: "major", price, created_at: at, ...extra,
});

test("summarize: two different reporters corroborate; one reporter cannot", () => {
  const s = summarize([
    row("a", "nnpc", 1250, "2026-11-10T08:00:00.000Z"),
    row("b", "nnpc", 1255, "2026-11-10T09:00:00.000Z"), // within 1.5%
    // the same person repeating themselves five times is still one reporter
    ...[1, 2, 3, 4, 5].map((i) => row("c", "total", 1300, `2026-11-10T0${i}:00:00.000Z`)),
  ]).lagos.pms;

  const nnpc = s.brands.find((b) => b.brand === "nnpc");
  assert.equal(nnpc.status, "corroborated");
  assert.equal(nnpc.price, 1255); // newest report
  assert.equal(nnpc.reporters_agreeing, 2);

  const total = s.brands.find((b) => b.brand === "total");
  assert.equal(total.status, "unverified");
  assert.equal(total.reporters_total, 1);

  assert.equal(s.status, "corroborated");
  assert.equal(s.reporters, 3);
  assert.equal(s.reports, 3); // one per reporter per brand
  assert.equal(s.median, 1277.5);
  assert.deepEqual([s.min, s.max], [1255, 1300]);
  assert.equal(s.newest_report_at, "2026-11-10T09:00:00.000Z");
});

test("summarize: disagreeing reporters do not corroborate, newest price wins", () => {
  const s = summarize([
    row("a", "nnpc", 1250, "2026-11-10T08:00:00.000Z"),
    row("b", "nnpc", 1400, "2026-11-10T09:00:00.000Z"),
  ]).lagos.pms;
  assert.equal(s.brands[0].status, "unverified");
  assert.equal(s.brands[0].price, 1400);
  assert.equal(s.brands[0].reporters_total, 2);
  assert.equal(s.status, "unverified");
});

test("summarize: a reporter's correction replaces their earlier report", () => {
  const s = summarize([
    row("a", "nnpc", 1520, "2026-11-10T08:00:00.000Z"), // typo
    row("a", "nnpc", 1250, "2026-11-10T08:02:00.000Z"),
    row("b", "nnpc", 1250, "2026-11-10T07:00:00.000Z"),
  ]).lagos.pms;
  assert.equal(s.brands[0].price, 1250);
  assert.equal(s.brands[0].status, "corroborated");
});

test("summarize: areas and fuels are kept apart", () => {
  const s = summarize([
    row("a", "nnpc", 1250, "2026-11-10T08:00:00.000Z"),
    row("a", "nnpc", 1700, "2026-11-10T08:00:00.000Z", { fuel: "ago" }),
    row("b", "nnpc", 1310, "2026-11-10T08:00:00.000Z", { area: "abuja" }),
  ]);
  assert.deepEqual(Object.keys(s).sort(), ["abuja", "lagos"]);
  assert.deepEqual(Object.keys(s.lagos).sort(), ["ago", "pms"]);
  assert.equal(s.abuja.pms.median, 1310);
});

// The same cases are replayed against fuelapi/community.py (tests/test_community.py).
// If this fails after an intended rule change, regenerate the fixture with
// `node test/make_summary_fixture.js` and update the Python side to match.
const cases = JSON.parse(readFileSync(new URL("./fixtures/summary_cases.json", import.meta.url), "utf8"));
for (const c of cases) {
  test(`shared fixture: ${c.name}`, () => {
    assert.deepEqual(summarize(c.reports), c.expected);
  });
}
