# Nigeria Fuel Prices API

Free, static JSON for petrol (PMS), diesel (AGO), household kerosene (DPK) and
cooking gas (LPG) prices in Nigeria.

Three kinds of evidence are published side by side and **never blended**:

| Layer | What it is | Freshness | Strength |
|---|---|---|---|
| **NBS baseline** | Official state averages for all 36 states + FCT: petrol monthly since January 2016, diesel since June 2015, kerosene (per litre) and cooking gas (per 12.5 kg refill) since November 2023 | Monthly, published 3–6 weeks after month end | Official, complete, slow |
| **News signals** | Prices stated in news headlines, extracted by rule | Hours to days | A lead, not a measurement |
| **Community reports** | Pump prices people report through a Telegram bot (pilot: Lagos, Abuja, Port Harcourt, Kano) | Last 72 hours | Only as good as its reporters |

Every figure says which layer it came from and what date it describes.
**The NBS figures are a baseline, not live pump prices** – compare `period` with
today's date.

## Architecture

```
            reads (free, static, cached)                 writes
┌───────────────────────────────────────────┐   ┌──────────────────────────┐
│ GitHub Actions, every 6 hours             │   │ Cloudflare Worker + D1   │
│  ingest NBS workbooks  ─┐                 │   │  Telegram webhook        │
│  fetch news headlines  ─┼─> data/ (git) ──┼─> │  validates, stores       │
│  pull accepted reports ─┘        │        │<──┼─ GET /export/reports     │
│                    build ────────┘        │   └──────────────────────────┘
│                      └─> public/v1/*.json │
└──────────────┬────────────────────────────┘
               └─> GitHub Pages or Cloudflare Pages
```

- **Reads** are plain files: no server, no cold start, no database to rate-limit.
- **Writes** go to one Worker. The reports it accepts are copied into
  `data/community/reports.csv` on the next run and published by the same build.
- Everything published is derived from `data/`, which is in git, so git history
  is the price history. The build is deterministic: the same `data/` always
  produces byte-identical `public/v1/`.
- No PostGIS or geo queries: nothing here is station-level yet.

## Static API

| Endpoint | Contents |
|---|---|
| `/v1/index.json` | Coverage and list of endpoints |
| `/v1/national.json` | National average, three highest and lowest states, zone averages, MoM/YoY |
| `/v1/states.json` | All states with slug, zone and latest price |
| `/v1/prices/{state}.json` | One state: NBS figure, rank, change, full history, plus that state's news signals and community reports |
| `/v1/fuels/{fuel}/latest.json` | Every state for the latest month; `fuel` is `pms`, `ago`, `dpk` or `lpg` |
| `/v1/fuels/{fuel}/history.json` / `.csv` | The whole monthly series |
| `/v1/news.json` | News signals from the last 45 days |
| `/v1/community.json` | Community reports from the last 72 hours (once the bot is connected) |
| `/v1/community/history.json` | Daily medians of community reports |
| `/v1/meta/sources.json` | Which NBS workbooks the numbers came from, with SHA-256 |
| `/v1/meta/issues.json` | Problems found in the source files and what was left out |

State slugs are lowercase with hyphens (`akwa-ibom`, `cross-river`). The FCT is
`fct`; `abuja` works too.

```json
// GET /v1/prices/lagos.json  (abridged)
{
  "state": "Lagos", "slug": "lagos", "zone": "South West", "currency": "NGN",
  "fuels": {
    "pms": {
      "unit": "NGN/litre",
      "latest": {
        "period": "2026-05", "period_end": "2026-05-31", "price": 1561.22,
        "granularity": "monthly state average",
        "national_average": 1596.25, "difference_vs_national_pct": -2.19,
        "rank_cheapest": 12, "states_ranked": 37,
        "mom_change_pct": 5.05, "yoy_change_pct": 44.95
      },
      "history": [{ "period": "2016-01", "price": 91.1 }, "..."]
    }
  },
  "news_signals": {
    "as_of": "2026-11-10",
    "signals": [{
      "fuel": "pms", "price": 1355.0, "unit": "NGN/litre", "price_type": "pump",
      "state": "Lagos", "marketer": "nnpc", "direction": "down", "outlets": 1,
      "last_reported_at": "2026-11-09T15:48:51Z",
      "nbs_reference": { "price": 1561.22, "period": "2026-05" },
      "headlines": [{ "outlet": "...", "title": "...", "link": "..." }]
    }]
  },
  "community": { "as_of": "...", "window_hours": 72, "areas": [] }
}
```

## Running it

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements-dev.txt     # Windows; use .venv/bin/pip elsewhere

python -m fuelapi ingest            # fetch NBS workbooks not yet downloaded
python -m fuelapi news              # poll news feeds, store new price headlines
python -m fuelapi community         # pull accepted reports from the bot (needs two env vars)
python -m fuelapi build             # rebuild public/v1 from data/
python -m fuelapi update            # all of the above

python -m fuelapi ingest --backfill # one-off: every NBS workbook back to 2017
python -m fuelapi news --days 30    # one-off: seed a month of headlines
python -m fuelapi parse some.xlsx   # show what the NBS parser reads from a workbook
python -m fuelapi extract "NNPC cuts petrol price to N1,325 in Abuja"   # try the news extractor
python -m pytest                    # tests use real NBS workbooks and real headlines
```

`public/` is the deployable site. Preview it with `python -m http.server -d public`.

## Layer 1: NBS baseline

### Where the data comes from

- **2024-11 onward:** NBS microdata catalog, entry
  [157](https://microdata.nigerianstat.gov.ng/index.php/catalog/157) (petrol) and
  [158](https://microdata.nigerianstat.gov.ng/index.php/catalog/158) (diesel). Each
  month is a ZIP holding the PDF report and an Excel workbook; only the workbook is kept.
- **2017 to 2024-10:** the old NBS eLibrary (`nigerianstat.gov.ng/resource/*.xlsx`),
  which stopped updating in December 2024.
- **Fallback:** both NBS hosts are often unreachable. When the catalog does not
  answer, the ingester fetches the same files from the Internet Archive's Wayback
  Machine. Archived copies lag NBS by days to weeks, and a month the Archive has
  not captured stays missing until NBS is reachable.

### How the numbers are chosen

- The parser finds the table by structure (a header row of month columns, rows
  labelled with state names), because NBS has changed the layout many times.
- Each workbook covers its own month plus the previous month and the same month a
  year earlier, so months overlap between workbooks. The value from a month's own
  report wins; otherwise the newest report. Disagreements above 1% are listed in
  `meta/issues.json` as `revision`.
- If a workbook's name says one month and its columns say another, and nothing
  else settles it, the sheet is **quarantined** and not published.
- A month needs at least 30 states to be published. Values outside ₦20–₦20,000
  are dropped.
- `national_average` is the figure NBS reports. `mean_of_states` is recomputed
  from the state rows as a cross-check.
- Not covered yet: LPG (cooking gas) and kerosene. Their workbooks use different
  units and headers and are skipped, not guessed at.

## Layer 2: News signals

`python -m fuelapi news` polls seven Google News search feeds and nine Nigerian
outlets' RSS feeds. Only **headlines** are read – no article pages are fetched.
A headline is stored (title, outlet, link, date) when it mentions petrol or
diesel together with a naira amount; claims are re-derived from the stored
headlines on every build, so improving the rules improves the whole history.

`fuelapi/news_extract.py` turns a headline into claims:

| Headline | Result |
|---|---|
| NNPC Cuts Petrol Price to ₦1,325 in Abuja, ₦1,355 in Lagos | two pump-price claims, NNPC, falling |
| Dangote Refinery reduces petrol gantry price to N1,215 per litre | one ex-depot (wholesale) claim |
| No more N1,420: NNPC, MRS, AP reduce petrol prices | nothing – N1,420 is the *old* price |
| Petrol could hit N1,411/Litre in November – Analyst | nothing – a prediction |
| Marketers Reject N1,350 Government Price Cap on Petrol | nothing – a policy figure |
| Landing cost of petrol falls to N1,283.81/litre | nothing – a cost, not a selling price |

A claim is published only when it is **high confidence**: it names the seller,
or it names a place and is explicitly per litre / pump / ex-depot. It must also
sit within 0.5×–2× of the latest NBS average (0.4×–1.5× for ex-depot). The same
figure reported within four days becomes one signal with an `outlets` count.

**How good is it?** On 142 real headlines collected in November 2026, it
published 16 claims from 14 headlines and all 16 were right on manual reading;
it withheld six more as lower confidence. That is one small sample: expect
occasional errors, and treat every signal as a lead to check.

**Limits you should know about**

- Coverage is whatever makes headlines: mostly NNPC, Dangote, Lagos and Abuja.
  Most states will have no signal most of the time.
- A headline price may be one station, one city, or a wholesale price.
- Outlets republish each other, so `outlets: 3` is weaker than it looks.
- Recall is deliberately low. Headlines without a figure ("NNPCL reduces petrol
  pump price") produce nothing.
- Google News feeds are intended for feed readers; this polls them four times a
  day. Outlets that refuse automated readers (403) are skipped, not worked around.

## Layer 3: Community reports

People send `/report lagos nnpc 1250` to a Telegram bot. The Worker in `bot/`
checks the price and stores it in D1. It also answers `/price lagos` and serves
its own live summary at `/v1/community.json`.

Rules (`bot/src/core.js`, mirrored in `fuelapi/community.py`; one shared fixture
tests both):

- A price must be inside hard bounds, and within 0.5×–2× of the last NBS state
  average. Once three other people have reported in that city in the past week,
  it must instead be within ±30% of their median. Rejected reports are stored
  in D1 but never published or copied to git.
- Only the last 72 hours count, and only each reporter's newest report per
  station brand.
- A brand's price is `corroborated` when two or more different reporters agree
  within 1.5%. Otherwise it is published as `unverified`.
- 12 reports per reporter per 24 hours.
- Telegram user ids are replaced with a keyed hash; names and usernames are
  never stored. That hash **is** published in `data/community/reports.csv`, so
  one reporter's reports can be linked to each other, though not to a person.

**Known limit:** "different reporters" means different Telegram accounts.
Someone with two accounts can corroborate their own report. The price checks
bound how wrong such a report can be; they do not prevent it.

### Testing

```bash
cd bot
node --test        # Node 22.13+; uses node:sqlite in place of D1
```

### Deploying the bot

Needs a Cloudflare account and a bot token from [@BotFather](https://t.me/BotFather).

```bash
cd bot
npm install
npx wrangler login
npx wrangler d1 create fuel-reports          # paste the printed id into wrangler.toml
# also set BASELINE_URL in wrangler.toml to the static site's URL
npm run db:init
npx wrangler secret put TELEGRAM_WEBHOOK_SECRET   # any long random string (A-Z a-z 0-9 _ -)
npx wrangler secret put REPORTER_SALT             # any long random string; never change it
npx wrangler secret put EXPORT_TOKEN              # any long random string
npm run deploy
```

Point Telegram at the Worker (replace the three placeholders):

```bash
curl "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
  -d "url=https://<your-worker>.workers.dev/telegram/webhook" \
  -d "secret_token=<TELEGRAM_WEBHOOK_SECRET>" \
  -d 'allowed_updates=["message"]'
```

The Worker replies through the webhook response, so the bot token is never
stored in Cloudflare.

Finally, let the build pull its reports. In the GitHub repository, under
Settings → Secrets and variables → Actions, add:

- variable `COMMUNITY_EXPORT_URL` = `https://<your-worker>.workers.dev`
- secret `COMMUNITY_EXPORT_TOKEN` = the `EXPORT_TOKEN` you set above

Until both exist the community step is skipped and those endpoints are absent.

## Deploying the static API (GitHub Pages)

1. Push this repository to GitHub.
2. Settings → Pages → Source: **GitHub Actions**.
3. `.github/workflows/update.yml` then runs every six hours: tests, NBS ingest,
   news, community sync, build, commit any data change, deploy `public/`.

For Cloudflare Pages instead, set the build output directory to `public`
(no build command). `public/_headers` adds CORS and cache headers there.

## Data licence and attribution

NBS figures are published by the National Bureau of Statistics, Nigeria; credit
NBS when you reuse them. News signals quote headlines with a link to the outlet
that published them. This project is not affiliated with NBS or any outlet.
