// Pure logic for the community price bot: no I/O, so it runs unchanged in
// Cloudflare Workers and under `node --test`.

/** Pilot areas. A report must name one of these. `state` is the slug used by
 *  the static NBS API (/v1/prices/{state}.json). */
export const AREAS = [
  { id: "lagos", label: "Lagos", state: "lagos", aliases: ["lagos", "lag"] },
  { id: "abuja", label: "Abuja", state: "fct", aliases: ["abuja", "abj", "fct"] },
  {
    id: "port-harcourt", label: "Port Harcourt", state: "rivers",
    aliases: ["port harcourt", "portharcourt", "port-harcourt", "ph", "phc"],
  },
  { id: "kano", label: "Kano", state: "kano", aliases: ["kano"] },
];

/** Known station brands. Anything else is stored as an independent station. */
export const BRANDS = [
  { id: "nnpc", label: "NNPC", category: "nnpc", aliases: ["nnpc", "nnpcl", "nnpc retail", "nnpc mega"] },
  { id: "totalenergies", label: "TotalEnergies", category: "major", aliases: ["total", "totalenergies", "total energies"] },
  { id: "mobil", label: "Mobil (11 Plc)", category: "major", aliases: ["mobil", "11plc", "11 plc"] },
  { id: "ardova", label: "Ardova (AP)", category: "major", aliases: ["ardova", "ap", "forte", "forte oil"] },
  { id: "conoil", label: "Conoil", category: "major", aliases: ["conoil"] },
  { id: "mrs", label: "MRS", category: "major", aliases: ["mrs"] },
  { id: "nipco", label: "NIPCO", category: "independent", aliases: ["nipco"] },
  { id: "rainoil", label: "Rainoil", category: "independent", aliases: ["rainoil", "rain oil"] },
  { id: "northwest", label: "Northwest", category: "independent", aliases: ["northwest", "north west"] },
  { id: "aa-rano", label: "A.A. Rano", category: "independent", aliases: ["aa rano", "a a rano", "aarano", "rano"] },
  { id: "bovas", label: "Bovas", category: "independent", aliases: ["bovas"] },
  { id: "matrix", label: "Matrix", category: "independent", aliases: ["matrix"] },
  { id: "independent", label: "Independent station", category: "independent", aliases: ["independent", "ipman", "other", "others"] },
];

export const FUELS = {
  pms: { label: "petrol", aliases: ["pms", "petrol", "fuel"], min: 200, max: 5000 },
  ago: { label: "diesel", aliases: ["ago", "diesel"], min: 300, max: 7000 },
};

export const WINDOW_HOURS = 72; // how long a report counts
export const AGREE_TOLERANCE = 0.015; // two prices within 1.5% "agree"
export const DAILY_LIMIT = 12; // reports per reporter per 24h
// NBS figures are a month or more old and prices have moved ~20% in a single
// month, so the band against the official baseline has to be wide.
export const BASELINE_BAND = [0.5, 2.0];
export const COMMUNITY_BAND = [0.7, 1.3]; // against recent community median
export const COMMUNITY_MIN_REPORTS = 3;

const AREA_LIST = AREAS.map((a) => a.label).join(", ");

function clean(text) {
  return String(text)
    .toLowerCase()
    .replace(/[‘’'`]/g, "")
    .replace(/[^a-z0-9.,₦\s-]/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function parsePriceToken(token) {
  const t = token.replace(/^(₦|ngn|n)/, "");
  if (!/^\d{1,3}(,\d{3})+(\.\d{1,2})?$/.test(t) && !/^\d+(\.\d{1,2})?$/.test(t)) return null;
  const value = Number(t.replace(/,/g, ""));
  return Number.isFinite(value) ? value : null;
}

/**
 * Parse "/report lagos nnpc 1250 [diesel]" (the leading command is optional).
 * Returns { ok: true, area, brand, fuel, price } or { ok: false, error }.
 */
export function parseReport(text) {
  let body = String(text ?? "").trim().replace(/^\/report(@\w+)?/i, "");
  const tokens = clean(body).split(" ").filter(Boolean);
  if (tokens.length === 0) return { ok: false, error: "empty" };

  let fuel = "pms";
  let fuelSeen = 0;
  const prices = [];
  const rest = [];
  for (const tok of tokens) {
    const fuelId = Object.keys(FUELS).find((f) => FUELS[f].aliases.includes(tok));
    if (fuelId) {
      fuel = fuelId;
      fuelSeen += 1;
      continue;
    }
    const price = parsePriceToken(tok);
    // Numbers under 100 are never a pump price; they belong to a brand ("11 plc").
    if (price !== null && price >= 100) {
      prices.push(price);
      continue;
    }
    rest.push(tok.replace(/[.,₦]/g, ""));
  }
  if (prices.length === 0) return { ok: false, error: "no_price" };
  if (prices.length > 1) return { ok: false, error: "many_prices" };
  if (fuelSeen > 1) return { ok: false, error: "many_fuels" };

  const phrase = rest.filter(Boolean).join(" ");
  let area = null;
  let brandText = "";
  const candidates = AREAS.flatMap((a) => a.aliases.map((alias) => ({ a, alias })))
    .sort((x, y) => y.alias.length - x.alias.length);
  for (const { a, alias } of candidates) {
    if (phrase === alias || phrase.startsWith(alias + " ")) {
      area = a;
      brandText = phrase.slice(alias.length).trim();
      break;
    }
    if (phrase.endsWith(" " + alias)) {
      area = a;
      brandText = phrase.slice(0, -alias.length).trim();
      break;
    }
  }
  if (!area) return { ok: false, error: "unknown_area" };
  if (!brandText) return { ok: false, error: "no_brand" };

  let brand = BRANDS.find((b) => b.aliases.includes(brandText));
  if (!brand) {
    const slug = brandText.replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "");
    if (!slug || slug.length > 24) return { ok: false, error: "bad_brand" };
    brand = {
      id: "other-" + slug,
      label: brandText.replace(/\b[a-z]/g, (c) => c.toUpperCase()),
      category: "independent",
    };
  }
  return {
    ok: true,
    area: { id: area.id, label: area.label, state: area.state },
    brand: { id: brand.id, label: brand.label, category: brand.category },
    fuel,
    price: Math.round(prices[0] * 100) / 100,
  };
}

/**
 * Decide whether a parsed price is believable.
 * `baseline`: { price, period } from NBS, or null. `communityMedian`: number or null.
 */
export function validatePrice({ fuel, price, baseline, communityMedian }) {
  const f = FUELS[fuel];
  if (price < f.min || price > f.max) {
    return { ok: false, reason: "out_of_bounds", low: f.min, high: f.max };
  }
  if (communityMedian) {
    const [lo, hi] = COMMUNITY_BAND;
    if (price < communityMedian * lo || price > communityMedian * hi) {
      return { ok: false, reason: "far_from_recent_reports", reference: communityMedian };
    }
    return { ok: true, checked_against: "recent_reports" };
  }
  if (baseline?.price) {
    const [lo, hi] = BASELINE_BAND;
    if (price < baseline.price * lo || price > baseline.price * hi) {
      return { ok: false, reason: "far_from_official_average", reference: baseline.price, period: baseline.period };
    }
    return { ok: true, checked_against: "official_average" };
  }
  return { ok: true, checked_against: "bounds_only" };
}

export function median(values) {
  if (values.length === 0) return null;
  const s = [...values].sort((a, b) => a - b);
  const mid = s.length >> 1;
  return s.length % 2 ? s[mid] : Math.round(((s[mid - 1] + s[mid]) / 2) * 100) / 100;
}

// Newest first; the row id breaks ties so the result never depends on input
// order. fuelapi/community.py applies the same ordering.
const isNewer = (a, b) =>
  a.created_at > b.created_at || (a.created_at === b.created_at && (a.id ?? 0) > (b.id ?? 0));

/** Keep only each reporter's newest report per (area, fuel, brand). */
export function latestPerReporter(reports) {
  const newest = new Map();
  for (const r of reports) {
    const key = [r.area, r.fuel, r.brand, r.reporter_id].join("|");
    const seen = newest.get(key);
    if (!seen || isNewer(r, seen)) newest.set(key, r);
  }
  return [...newest.values()];
}

/**
 * Summarise accepted reports from the last WINDOW_HOURS.
 * Returns { [areaId]: { [fuel]: { status, median, ... , brands: [...] } } }.
 *
 * A brand's price is "corroborated" when two or more different reporters agree
 * within AGREE_TOLERANCE; a single report is published as "unverified".
 */
export function summarize(reports) {
  const groups = new Map();
  for (const r of latestPerReporter(reports)) {
    const key = [r.area, r.fuel, r.brand].join("|");
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(r);
  }

  const out = {};
  for (const rows of groups.values()) {
    rows.sort((a, b) => (isNewer(a, b) ? -1 : 1));
    const head = rows[0];
    const agreeing = rows.filter(
      (r) => Math.abs(r.price - head.price) <= head.price * AGREE_TOLERANCE,
    );
    const brand = {
      brand: head.brand,
      label: head.brand_label,
      category: head.brand_category,
      price: head.price,
      status: agreeing.length >= 2 ? "corroborated" : "unverified",
      reporters_agreeing: agreeing.length,
      reporters_total: rows.length,
      reported_at: head.created_at,
    };
    out[head.area] ??= {};
    out[head.area][head.fuel] ??= { brands: [], _rows: [] };
    out[head.area][head.fuel].brands.push(brand);
    out[head.area][head.fuel]._rows.push(...rows);
  }

  for (const area of Object.values(out)) {
    for (const [fuel, entry] of Object.entries(area)) {
      const rows = entry._rows;
      delete entry._rows;
      // Plain code-point order (not localeCompare) so Python sorts identically.
      entry.brands.sort((a, b) => a.price - b.price || (a.brand < b.brand ? -1 : a.brand > b.brand ? 1 : 0));
      const prices = entry.brands.map((b) => b.price);
      const times = rows.map((r) => r.created_at).sort();
      const reporters = new Set(rows.map((r) => r.reporter_id)).size;
      const corroborated = entry.brands.some((b) => b.status === "corroborated");
      area[fuel] = {
        status: corroborated ? "corroborated" : "unverified",
        unit: "NGN/litre",
        median: median(prices),
        min: Math.min(...prices),
        max: Math.max(...prices),
        reports: rows.length,
        reporters,
        oldest_report_at: times[0],
        newest_report_at: times[times.length - 1],
        brands: entry.brands,
      };
    }
  }
  return out;
}

export const naira = (n) =>
  "₦" + Number(n).toLocaleString("en-NG", { minimumFractionDigits: 0, maximumFractionDigits: 2 });

export function monthLabel(period) {
  const [y, m] = period.split("-").map(Number);
  return new Date(Date.UTC(y, m - 1, 1)).toLocaleDateString("en-GB", {
    month: "long", year: "numeric", timeZone: "UTC",
  });
}

export const HELP_TEXT = [
  "Send a pump price you saw today:",
  "",
  "/report <city> <station brand> <price>",
  "",
  "Examples:",
  "/report lagos nnpc 1250",
  "/report abuja total 1310",
  "/report ph rainoil 1450 diesel",
  "",
  `Cities in this pilot: ${AREA_LIST}.`,
  "Petrol is assumed unless you add \"diesel\".",
  "",
  "/price <city> shows the latest reports and the official NBS average.",
  "",
  "Your Telegram name and number are never stored. Reports are public data.",
].join("\n");

export function parseErrorText(code) {
  const usage = "\n\nTry: /report lagos nnpc 1250";
  switch (code) {
    case "no_price": return "I couldn't find a price in that." + usage;
    case "many_prices": return "I found more than one price. Send one report per message." + usage;
    case "many_fuels": return "Send petrol and diesel as separate reports." + usage;
    case "unknown_area": return `This pilot covers ${AREA_LIST} only. Start your report with one of those.` + usage;
    case "no_brand": return "Which station brand was it? For an unbranded station say \"independent\"." + usage;
    case "bad_brand": return "I couldn't read the station brand. Keep it to a short name." + usage;
    default: return HELP_TEXT;
  }
}

export function rejectionText(check, fuel) {
  const what = FUELS[fuel].label;
  switch (check.reason) {
    case "out_of_bounds":
      return `That doesn't look like a ${what} price per litre (expected between ${naira(check.low)} and ${naira(check.high)}). Check the figure and send it again.`;
    case "far_from_recent_reports":
      return `That is far from other reports here this week (around ${naira(check.reference)}). If the sign really says that, send it again with the brand spelled out and others can confirm it.`;
    case "far_from_official_average":
      return `That is far from the last official average for this state (${naira(check.reference)}, ${monthLabel(check.period)}). Check the figure and send it again.`;
    default:
      return "I couldn't accept that report.";
  }
}
