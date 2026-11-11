// Regenerates test/fixtures/summary_cases.json: report sets and the summary
// core.js produces for them. The JSON is the contract that keeps the Worker
// (core.js) and the static build (fuelapi/community.py) on the same rules.
//
//   node test/make_summary_fixture.js
//
// Review the diff before committing: this file defines "correct" for both sides.
import { mkdirSync, writeFileSync } from "node:fs";

import { summarize } from "../src/core.js";

let nextId = 1;
const r = (reporter, area, state, brand, price, at, extra = {}) => ({
  id: nextId++, reporter_id: reporter, area, state, brand,
  brand_label: extra.label ?? brand.toUpperCase(), brand_category: extra.category ?? "major",
  fuel: extra.fuel ?? "pms", price, created_at: at,
});

const cases = [
  {
    name: "two reporters corroborate one brand; a repeating reporter does not",
    reports: [
      r("aaaaaaaa01", "lagos", "lagos", "nnpc", 1250, "2026-11-10T08:00:00.000Z", { category: "nnpc" }),
      r("aaaaaaaa02", "lagos", "lagos", "nnpc", 1255, "2026-11-10T09:00:00.000Z", { category: "nnpc" }),
      r("aaaaaaaa03", "lagos", "lagos", "totalenergies", 1300, "2026-11-10T01:00:00.000Z"),
      r("aaaaaaaa03", "lagos", "lagos", "totalenergies", 1300, "2026-11-10T02:00:00.000Z"),
      r("aaaaaaaa03", "lagos", "lagos", "totalenergies", 1300, "2026-11-10T03:00:00.000Z"),
    ],
  },
  {
    name: "disagreement: the newest price leads, nothing is corroborated",
    reports: [
      r("aaaaaaaa01", "kano", "kano", "mrs", 1250, "2026-11-10T08:00:00.000Z"),
      r("aaaaaaaa02", "kano", "kano", "mrs", 1400, "2026-11-10T09:00:00.000Z"),
    ],
  },
  {
    name: "a correction replaces the reporter's earlier report",
    reports: [
      r("aaaaaaaa01", "abuja", "fct", "conoil", 1520, "2026-11-10T08:00:00.000Z"),
      r("aaaaaaaa01", "abuja", "fct", "conoil", 1250, "2026-11-10T08:02:00.000Z"),
      r("aaaaaaaa02", "abuja", "fct", "conoil", 1250, "2026-11-10T07:00:00.000Z"),
    ],
  },
  {
    name: "areas and fuels stay apart; even count takes the mean of the middle two",
    reports: [
      r("aaaaaaaa01", "lagos", "lagos", "nnpc", 1250, "2026-11-10T08:00:00.000Z", { category: "nnpc" }),
      r("aaaaaaaa01", "lagos", "lagos", "mrs", 1275.55, "2026-11-10T08:05:00.000Z"),
      r("aaaaaaaa01", "lagos", "lagos", "nnpc", 1700, "2026-11-10T08:10:00.000Z", { fuel: "ago", category: "nnpc" }),
      r("aaaaaaaa02", "port-harcourt", "rivers", "rainoil", 1450, "2026-11-10T08:00:00.000Z", { category: "independent" }),
      r("aaaaaaaa03", "port-harcourt", "rivers", "other-danmarna-oil", 1440, "2026-11-10T08:30:00.000Z",
        { label: "Danmarna Oil", category: "independent" }),
    ],
  },
  {
    name: "identical timestamps: the higher id is the newer report; equal prices sort by brand id",
    reports: [
      r("aaaaaaaa01", "kano", "kano", "aa-rano", 1300, "2026-11-10T08:00:00.000Z", { category: "independent" }),
      r("aaaaaaaa01", "kano", "kano", "aa-rano", 1310, "2026-11-10T08:00:00.000Z", { category: "independent" }),
      r("aaaaaaaa02", "kano", "kano", "ab", 1310, "2026-11-10T08:00:00.000Z", { category: "independent" }),
      r("aaaaaaaa03", "kano", "kano", "a-z", 1310, "2026-11-10T08:00:00.000Z", { category: "independent" }),
    ],
  },
];

for (const c of cases) c.expected = summarize(c.reports);
mkdirSync(new URL("./fixtures/", import.meta.url), { recursive: true });
writeFileSync(new URL("./fixtures/summary_cases.json", import.meta.url), JSON.stringify(cases, null, 2) + "\n");
console.log(`wrote ${cases.length} cases`);
