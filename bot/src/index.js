// Cloudflare Worker: Telegram webhook + public community-price JSON.
//
// Bindings (see wrangler.toml):
//   DB                       D1 database (schema.sql)
//   BASELINE_URL             base URL of the static NBS API, e.g. https://you.github.io/nigeria-fuel-prices
//   TELEGRAM_WEBHOOK_SECRET  secret; must match the one given to Telegram's setWebhook
//   REPORTER_SALT            secret; keys the hash that replaces Telegram user ids
//   EXPORT_TOKEN             secret; bearer token for /export/reports
//
// Replies are sent as the webhook response body, so the Worker never needs the
// bot token.

import {
  AREAS, DAILY_LIMIT, WINDOW_HOURS, COMMUNITY_MIN_REPORTS, FUELS, HELP_TEXT,
  latestPerReporter, median, monthLabel, naira, parseErrorText, parseReport,
  rejectionText, summarize, validatePrice,
} from "./core.js";

const JSON_HEADERS = {
  "content-type": "application/json; charset=utf-8",
  "access-control-allow-origin": "*",
};

const json = (body, status = 200, extra = {}) =>
  new Response(JSON.stringify(body, null, 2), { status, headers: { ...JSON_HEADERS, ...extra } });

const hoursAgo = (now, h) => new Date(now.getTime() - h * 3600_000).toISOString();

function timingSafeEqual(a, b) {
  const x = new TextEncoder().encode(String(a ?? ""));
  const y = new TextEncoder().encode(String(b ?? ""));
  let diff = x.length ^ y.length;
  for (let i = 0; i < Math.max(x.length, y.length); i++) diff |= (x[i] ?? 0) ^ (y[i] ?? 0);
  return diff === 0;
}

async function reporterHash(salt, userId) {
  const key = await crypto.subtle.importKey(
    "raw", new TextEncoder().encode(salt), { name: "HMAC", hash: "SHA-256" }, false, ["sign"],
  );
  const mac = await crypto.subtle.sign("HMAC", key, new TextEncoder().encode(String(userId)));
  return [...new Uint8Array(mac)].slice(0, 16).map((b) => b.toString(16).padStart(2, "0")).join("");
}

/** Latest NBS state average for a fuel, or null if the static API is unreachable. */
async function fetchBaseline(env, fetchImpl, state, fuel) {
  if (!env.BASELINE_URL) return null;
  try {
    const url = `${env.BASELINE_URL.replace(/\/+$/, "")}/v1/prices/${state}.json`;
    const res = await fetchImpl(url, { cf: { cacheTtl: 3600, cacheEverything: true } });
    if (!res.ok) return null;
    const latest = (await res.json())?.fuels?.[fuel]?.latest;
    return latest?.price ? { price: latest.price, period: latest.period } : null;
  } catch {
    return null;
  }
}

async function acceptedSince(env, since, area = null, fuel = null) {
  let sql = `SELECT id, reporter_id, area, state, brand, brand_label, brand_category, fuel, price, created_at
             FROM reports WHERE status = 'accepted' AND created_at >= ?`;
  const args = [since];
  if (area) { sql += " AND area = ?"; args.push(area); }
  if (fuel) { sql += " AND fuel = ?"; args.push(fuel); }
  const { results } = await env.DB.prepare(sql).bind(...args).all();
  return results;
}

function areaDocument(area, summary, now) {
  return {
    area: area.id,
    label: area.label,
    state: area.state,
    baseline_url: `/v1/prices/${area.state}.json`,
    fuels: summary?.[area.id] ?? {},
    generated_at: now.toISOString(),
  };
}

const COMMUNITY_NOTE =
  `Crowd-sourced pump prices reported through Telegram in the last ${WINDOW_HOURS} hours. ` +
  "\"corroborated\" means two or more different reporters agree within 1.5%; " +
  "\"unverified\" is a single report. This is not official data and is never blended with the NBS averages.";

// ---------------------------------------------------------------- Telegram

async function handleReport(env, deps, text, userId) {
  const parsed = parseReport(text);
  if (!parsed.ok) return parseErrorText(parsed.error);

  const now = deps.now();
  const nowIso = now.toISOString();
  const reporterId = await reporterHash(env.REPORTER_SALT, userId);

  await env.DB.prepare(
    `INSERT INTO reporters (id, first_seen, last_seen) VALUES (?, ?, ?)
     ON CONFLICT (id) DO UPDATE SET last_seen = excluded.last_seen`,
  ).bind(reporterId, nowIso, nowIso).run();
  const reporter = await env.DB.prepare("SELECT blocked FROM reporters WHERE id = ?")
    .bind(reporterId).first();
  if (reporter?.blocked) return "Reports from this account are not being accepted.";

  const recent = await env.DB.prepare(
    "SELECT COUNT(*) AS n FROM reports WHERE reporter_id = ? AND created_at >= ?",
  ).bind(reporterId, hoursAgo(now, 24)).first();
  if ((recent?.n ?? 0) >= DAILY_LIMIT) {
    return `That's ${DAILY_LIMIT} reports in 24 hours, which is the limit. Thank you - please come back tomorrow.`;
  }

  const { area, brand, fuel, price } = parsed;
  const baseline = await fetchBaseline(env, deps.fetch, area.state, fuel);
  const week = latestPerReporter(await acceptedSince(env, hoursAgo(now, 24 * 7), area.id, fuel))
    .filter((r) => r.reporter_id !== reporterId);
  const communityMedian = week.length >= COMMUNITY_MIN_REPORTS ? median(week.map((r) => r.price)) : null;

  const check = validatePrice({ fuel, price, baseline, communityMedian });
  await env.DB.batch([
    env.DB.prepare(
      `INSERT INTO reports (reporter_id, area, state, brand, brand_label, brand_category, fuel,
                            price, status, reject_reason, checked_against, baseline_price,
                            baseline_period, created_at)
       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
    ).bind(
      reporterId, area.id, area.state, brand.id, brand.label, brand.category, fuel, price,
      check.ok ? "accepted" : "rejected", check.ok ? null : check.reason,
      check.ok ? check.checked_against : null, baseline?.price ?? null, baseline?.period ?? null,
      nowIso,
    ),
    env.DB.prepare(
      check.ok
        ? "UPDATE reporters SET accepted = accepted + 1 WHERE id = ?"
        : "UPDATE reporters SET rejected = rejected + 1 WHERE id = ?",
    ).bind(reporterId),
  ]);
  if (!check.ok) return rejectionText(check, fuel);

  const summary = summarize(await acceptedSince(env, hoursAgo(now, WINDOW_HOURS), area.id, fuel));
  const mine = summary[area.id]?.[fuel]?.brands.find((b) => b.brand === brand.id);
  const others = (mine?.reporters_agreeing ?? 1) - 1;
  const lines = [
    `Recorded: ${brand.label}, ${area.label} - ${naira(price)} per litre (${FUELS[fuel].label}). Thank you.`,
  ];
  if (others > 0) {
    lines.push(`${others} other reporter${others === 1 ? " agrees" : "s agree"}, so this price is now marked corroborated.`);
  } else {
    lines.push("It shows as unverified until a second person reports the same price.");
  }
  return lines.join("\n");
}

async function handlePrice(env, deps, text) {
  const arg = text.replace(/^\/price(@\w+)?/i, "").trim().toLowerCase().replace(/\s+/g, " ");
  const area = AREAS.find((a) => a.aliases.includes(arg) || a.id === arg);
  if (!area) return `Which city? Try: /price lagos\nCities in this pilot: ${AREAS.map((a) => a.label).join(", ")}.`;

  const now = deps.now();
  const summary = summarize(await acceptedSince(env, hoursAgo(now, WINDOW_HOURS), area.id));
  const lines = [`${area.label}`];
  for (const fuel of Object.keys(FUELS)) {
    const s = summary[area.id]?.[fuel];
    const baseline = await fetchBaseline(env, deps.fetch, area.state, fuel);
    if (!s && !baseline) continue;
    lines.push("", FUELS[fuel].label.toUpperCase());
    if (s) {
      for (const b of s.brands) {
        const mark = b.status === "corroborated" ? `confirmed by ${b.reporters_agreeing}` : "unverified";
        lines.push(`${b.label}: ${naira(b.price)} (${mark})`);
      }
    } else {
      lines.push(`No reports in the last ${WINDOW_HOURS} hours.`);
    }
    if (baseline) {
      lines.push(`Official state average: ${naira(baseline.price)} (NBS, ${monthLabel(baseline.period)})`);
    }
  }
  if (lines.length === 1) lines.push("", "No reports yet. Be the first: /report " + area.aliases[0] + " nnpc 1250");
  return lines.join("\n");
}

async function handleTelegram(request, env, deps) {
  if (!env.TELEGRAM_WEBHOOK_SECRET ||
      !timingSafeEqual(request.headers.get("x-telegram-bot-api-secret-token"), env.TELEGRAM_WEBHOOK_SECRET)) {
    return new Response("forbidden", { status: 403 });
  }
  let update;
  try {
    update = await request.json();
  } catch {
    return new Response("bad request", { status: 400 });
  }
  const msg = update?.message;
  const text = typeof msg?.text === "string" ? msg.text.trim().slice(0, 300) : "";
  // Always answer 200 so Telegram doesn't retry; only text from a person gets a reply.
  if (!msg?.chat?.id || !msg?.from?.id || msg.from.is_bot || !text) return new Response("ok");

  const isPrivate = msg.chat.type === "private";
  let reply = null;
  try {
    if (/^\/(start|help)\b/i.test(text)) reply = HELP_TEXT;
    else if (/^\/price\b/i.test(text)) reply = await handlePrice(env, deps, text);
    else if (/^\/report\b/i.test(text)) reply = await handleReport(env, deps, text, msg.from.id);
    else if (isPrivate && !text.startsWith("/")) reply = await handleReport(env, deps, text, msg.from.id);
    else if (isPrivate) reply = HELP_TEXT;
  } catch (err) {
    console.error("handler error", err?.stack ?? err);
    reply = "Something went wrong on our side. Please try again in a minute.";
  }
  if (!reply) return new Response("ok");
  return json({ method: "sendMessage", chat_id: msg.chat.id, text: reply, disable_web_page_preview: true });
}

// ------------------------------------------------------------------- HTTP

async function handleCommunity(env, deps, areaId) {
  const now = deps.now();
  const summary = summarize(await acceptedSince(env, hoursAgo(now, WINDOW_HOURS)));
  const cache = { "cache-control": "public, max-age=300" };
  if (areaId) {
    const area = AREAS.find((a) => a.id === areaId);
    if (!area) return json({ error: "unknown area", areas: AREAS.map((a) => a.id) }, 404);
    return json({ note: COMMUNITY_NOTE, window_hours: WINDOW_HOURS, ...areaDocument(area, summary, now) }, 200, cache);
  }
  return json({
    note: COMMUNITY_NOTE,
    window_hours: WINDOW_HOURS,
    generated_at: now.toISOString(),
    areas: AREAS.map((a) => {
      const { generated_at: _drop, ...doc } = areaDocument(a, summary, now);
      return doc;
    }),
  }, 200, cache);
}

async function handleExport(request, env, url) {
  const auth = request.headers.get("authorization") ?? "";
  if (!env.EXPORT_TOKEN || !timingSafeEqual(auth, `Bearer ${env.EXPORT_TOKEN}`)) {
    return json({ error: "unauthorized" }, 401);
  }
  const afterId = Number.parseInt(url.searchParams.get("after_id") ?? "0", 10) || 0;
  const limit = Math.min(Math.max(Number.parseInt(url.searchParams.get("limit") ?? "500", 10) || 500, 1), 1000);
  const { results } = await env.DB.prepare(
    `SELECT id, reporter_id, area, state, brand, brand_label, brand_category, fuel, price, status,
            reject_reason, checked_against, baseline_price, baseline_period, created_at
     FROM reports WHERE id > ? ORDER BY id LIMIT ?`,
  ).bind(afterId, limit).all();
  return json({
    reports: results,
    next_after_id: results.length === limit ? results[results.length - 1].id : null,
  }, 200, { "cache-control": "no-store" });
}

export function createHandler(overrides = {}) {
  const deps = {
    fetch: (...args) => fetch(...args),
    now: () => new Date(),
    ...overrides,
  };
  return async function handle(request, env) {
    const url = new URL(request.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (request.method === "POST" && path === "/telegram/webhook") {
      return handleTelegram(request, env, deps);
    }
    if (request.method === "OPTIONS") {
      return new Response(null, {
        status: 204,
        headers: { "access-control-allow-origin": "*", "access-control-allow-methods": "GET, OPTIONS" },
      });
    }
    if (request.method !== "GET") return json({ error: "method not allowed" }, 405);

    if (path === "/") {
      return json({
        name: "Nigeria fuel prices - community reports",
        note: COMMUNITY_NOTE,
        endpoints: { all: "/v1/community.json", area: "/v1/community/{area}.json" },
        areas: AREAS.map((a) => a.id),
      });
    }
    if (path === "/v1/community.json") return handleCommunity(env, deps, null);
    const m = path.match(/^\/v1\/community\/([a-z-]+)\.json$/);
    if (m) return handleCommunity(env, deps, m[1]);
    if (path === "/export/reports") return handleExport(request, env, url);
    return json({ error: "not found" }, 404);
  };
}

export default { fetch: createHandler() };
