// End-to-end tests of the Worker against a real SQL engine: node:sqlite stands
// in for Cloudflare D1 (same SQLite dialect, same prepare/bind/first/all/run API).
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { DatabaseSync } from "node:sqlite";
import { beforeEach, test } from "node:test";

import { createHandler } from "../src/index.js";

class FakeStatement {
  constructor(db, sql, args = []) { this.db = db; this.sql = sql; this.args = args; }
  bind(...args) { return new FakeStatement(this.db, this.sql, args); }
  async first() { return this.db.prepare(this.sql).get(...this.args) ?? null; }
  async all() { return { results: this.db.prepare(this.sql).all(...this.args) }; }
  async run() {
    const r = this.db.prepare(this.sql).run(...this.args);
    return { meta: { changes: Number(r.changes), last_row_id: Number(r.lastInsertRowid) } };
  }
}
class FakeD1 {
  constructor() {
    this.db = new DatabaseSync(":memory:");
    this.db.exec(readFileSync(new URL("../schema.sql", import.meta.url), "utf8"));
  }
  prepare(sql) { return new FakeStatement(this.db, sql); }
  async batch(statements) { const out = []; for (const s of statements) out.push(await s.run()); return out; }
  rows(sql, ...args) { return this.db.prepare(sql).all(...args); }
}

const SECRET = "hook-secret";
const BASELINES = { lagos: 1561.22, fct: 1602.64, rivers: 1590.0, kano: 1607.27 };

let env, handle, clock, baselineUp;

beforeEach(() => {
  clock = new Date("2026-11-10T09:00:00.000Z");
  baselineUp = true;
  env = {
    DB: new FakeD1(),
    BASELINE_URL: "https://static.example/",
    TELEGRAM_WEBHOOK_SECRET: SECRET,
    REPORTER_SALT: "pepper",
    EXPORT_TOKEN: "export-token",
  };
  handle = createHandler({
    now: () => new Date(clock),
    fetch: async (url) => {
      if (!baselineUp) throw new Error("static site down");
      const m = String(url).match(/^https:\/\/static\.example\/v1\/prices\/([a-z-]+)\.json$/);
      if (!m || !BASELINES[m[1]]) return new Response("nope", { status: 404 });
      return Response.json({ fuels: { pms: { latest: { price: BASELINES[m[1]], period: "2026-05" } } } });
    },
  });
});

const advance = (minutes) => { clock = new Date(clock.getTime() + minutes * 60_000); };

async function say(userId, text, { secret = SECRET, chatType = "private" } = {}) {
  const res = await handle(new Request("https://bot.example/telegram/webhook", {
    method: "POST",
    headers: { "content-type": "application/json", "x-telegram-bot-api-secret-token": secret },
    body: JSON.stringify({
      update_id: 1,
      message: {
        message_id: 1, text, chat: { id: 1000 + userId, type: chatType },
        from: { id: userId, is_bot: false, first_name: "Ada", username: "ada_secret_handle" },
      },
    }),
  }), env);
  const body = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
  return { status: res.status, body, text: body?.text };
}

async function get(path, headers = {}) {
  const res = await handle(new Request("https://bot.example" + path, { headers }), env);
  return { status: res.status, headers: res.headers, body: await res.json() };
}

test("webhook rejects requests without Telegram's secret header", async () => {
  assert.equal((await say(1, "/report lagos nnpc 1250", { secret: "wrong" })).status, 403);
  assert.equal((await say(1, "/report lagos nnpc 1250", { secret: "" })).status, 403);
  assert.equal(env.DB.rows("SELECT COUNT(*) AS n FROM reports")[0].n, 0);
});

test("a report is stored, answered, and published as unverified", async () => {
  const r = await say(1, "/report lagos nnpc 1250");
  assert.equal(r.status, 200);
  assert.equal(r.body.method, "sendMessage");
  assert.equal(r.body.chat_id, 1001);
  assert.match(r.text, /Recorded: NNPC, Lagos - ₦1,250 per litre \(petrol\)/);
  assert.match(r.text, /unverified until a second person/);

  const [row] = env.DB.rows("SELECT * FROM reports");
  assert.equal(row.status, "accepted");
  assert.equal(row.checked_against, "official_average");
  assert.equal(row.baseline_price, 1561.22);
  assert.equal(row.baseline_period, "2026-05");
  assert.equal(row.created_at, "2026-11-10T09:00:00.000Z");

  const { body, headers } = await get("/v1/community/lagos.json");
  assert.equal(headers.get("access-control-allow-origin"), "*");
  assert.equal(body.fuels.pms.status, "unverified");
  assert.equal(body.fuels.pms.brands[0].price, 1250);
  assert.equal(body.state, "lagos");
});

test("no Telegram identity is stored anywhere", async () => {
  await say(424242, "/report lagos nnpc 1250");
  const dump = JSON.stringify([env.DB.rows("SELECT * FROM reports"), env.DB.rows("SELECT * FROM reporters")]);
  assert.ok(!dump.includes("424242"));
  assert.ok(!dump.includes("ada_secret_handle"));
  assert.ok(!dump.includes("Ada"));
  const [reporter] = env.DB.rows("SELECT * FROM reporters");
  assert.match(reporter.id, /^[0-9a-f]{32}$/);
  assert.equal(reporter.accepted, 1);
});

test("a second reporter corroborates; the same reporter repeating does not", async () => {
  await say(1, "/report lagos nnpc 1250");
  advance(5);
  await say(1, "/report lagos nnpc 1250");
  let lagos = (await get("/v1/community/lagos.json")).body.fuels.pms;
  assert.equal(lagos.brands[0].status, "unverified");
  assert.equal(lagos.reporters, 1);

  advance(30);
  const r = await say(2, "lagos nnpc 1260"); // bare text in a private chat, within 1.5%
  assert.match(r.text, /1 other reporter agrees/);
  lagos = (await get("/v1/community/lagos.json")).body.fuels.pms;
  assert.equal(lagos.brands[0].status, "corroborated");
  assert.equal(lagos.brands[0].reporters_agreeing, 2);
  assert.equal(lagos.status, "corroborated");
});

test("implausible prices are rejected, recorded, and never published", async () => {
  assert.match((await say(1, "/report lagos nnpc 125")).text, /doesn't look like a petrol price/);
  assert.match((await say(1, "/report lagos nnpc 700")).text, /far from the last official average .*₦1,561\.22, May 2026/);
  const rows = env.DB.rows("SELECT status, reject_reason FROM reports ORDER BY id");
  assert.deepEqual(rows.map((r) => [r.status, r.reject_reason]), [
    ["rejected", "out_of_bounds"], ["rejected", "far_from_official_average"],
  ]);
  assert.equal(env.DB.rows("SELECT rejected FROM reporters")[0].rejected, 2);
  assert.deepEqual((await get("/v1/community/lagos.json")).body.fuels, {});
});

test("once three others have reported, a far-off price is checked against them", async () => {
  await say(1, "/report kano nnpc 1600");
  await say(2, "/report kano total 1610");
  await say(3, "/report kano mrs 1590");
  const r = await say(4, "/report kano conoil 1050"); // inside the wide NBS band, far from the crowd
  assert.match(r.text, /far from other reports here this week \(around ₦1,600\)/);
  const ok = await say(4, "/report kano conoil 1620");
  assert.match(ok.text, /Recorded/);
  const last = env.DB.rows("SELECT checked_against FROM reports ORDER BY id DESC LIMIT 1")[0];
  assert.equal(last.checked_against, "recent_reports");
});

test("reports still work when the static baseline is unreachable", async () => {
  baselineUp = false;
  assert.match((await say(1, "/report abuja total 1310")).text, /Recorded: TotalEnergies, Abuja/);
  assert.equal(env.DB.rows("SELECT checked_against FROM reports")[0].checked_against, "bounds_only");
});

test("daily limit per reporter", async () => {
  for (let i = 0; i < 12; i++) {
    assert.match((await say(1, `/report lagos nnpc ${1250 + i}`)).text, /Recorded/);
  }
  assert.match((await say(1, "/report lagos nnpc 1250")).text, /12 reports in 24 hours/);
  assert.match((await say(2, "/report lagos nnpc 1250")).text, /Recorded/); // other people unaffected
  advance(24 * 60 + 1);
  assert.match((await say(1, "/report lagos nnpc 1250")).text, /Recorded/);
});

test("blocked reporters are refused", async () => {
  await say(1, "/report lagos nnpc 1250");
  env.DB.db.exec("UPDATE reporters SET blocked = 1");
  assert.match((await say(1, "/report lagos nnpc 1250")).text, /not being accepted/);
  assert.equal(env.DB.rows("SELECT COUNT(*) AS n FROM reports")[0].n, 1);
});

test("reports expire from the public feed after 72 hours but stay in the database", async () => {
  await say(1, "/report lagos nnpc 1250");
  advance(71 * 60);
  assert.equal((await get("/v1/community/lagos.json")).body.fuels.pms.reports, 1);
  advance(2 * 60);
  assert.deepEqual((await get("/v1/community/lagos.json")).body.fuels, {});
  assert.equal(env.DB.rows("SELECT COUNT(*) AS n FROM reports")[0].n, 1);
});

test("/price shows community reports next to the official average", async () => {
  await say(1, "/report ph rainoil 1450");
  await say(2, "/report ph rainoil 1450");
  const r = await say(3, "/price ph");
  assert.match(r.text, /^Port Harcourt/);
  assert.match(r.text, /Rainoil: ₦1,450 \(confirmed by 2\)/);
  assert.match(r.text, /Official state average: ₦1,590 \(NBS, May 2026\)/);
  assert.match((await say(3, "/price ibadan")).text, /Which city\?/);
});

test("help, parse errors, group chats and non-text updates", async () => {
  assert.match((await say(1, "/start")).text, /\/report <city> <station brand> <price>/);
  assert.match((await say(1, "/report ibadan nnpc 1250")).text, /pilot covers Lagos, Abuja, Port Harcourt, Kano only/);
  // In a group, ordinary chatter is ignored; commands still work.
  const chatter = await say(1, "anyone know lagos nnpc 1250?", { chatType: "supergroup" });
  assert.equal(chatter.body, "ok");
  assert.match((await say(1, "/report lagos nnpc 1250", { chatType: "supergroup" })).text, /Recorded/);

  const res = await handle(new Request("https://bot.example/telegram/webhook", {
    method: "POST",
    headers: { "x-telegram-bot-api-secret-token": SECRET },
    body: JSON.stringify({ update_id: 2, edited_message: { text: "/report lagos nnpc 1" } }),
  }), env);
  assert.equal(await res.text(), "ok");
});

test("community index lists every pilot area", async () => {
  await say(1, "/report abuja total 1310");
  const { body } = await get("/v1/community.json");
  assert.deepEqual(body.areas.map((a) => a.area), ["lagos", "abuja", "port-harcourt", "kano"]);
  assert.equal(body.areas[1].fuels.pms.median, 1310);
  assert.equal(body.window_hours, 72);
  assert.equal((await get("/v1/community/ibadan.json")).status, 404);
  assert.equal((await get("/nope")).status, 404);
});

test("export requires the bearer token and pages by id", async () => {
  await say(1, "/report lagos nnpc 1250");
  await say(1, "/report lagos nnpc 100"); // rejected rows are exported too
  await say(2, "/report kano mrs 1600");
  assert.equal((await get("/export/reports")).status, 401);
  assert.equal((await get("/export/reports", { authorization: "Bearer nope" })).status, 401);

  const auth = { authorization: "Bearer export-token" };
  const page1 = (await get("/export/reports?limit=2", auth)).body;
  assert.equal(page1.reports.length, 2);
  assert.equal(page1.next_after_id, 2);
  const page2 = (await get(`/export/reports?limit=2&after_id=${page1.next_after_id}`, auth)).body;
  assert.equal(page2.reports.length, 1);
  assert.equal(page2.next_after_id, null);
  assert.deepEqual(page1.reports.map((r) => r.status), ["accepted", "rejected"]);
});
