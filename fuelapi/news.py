"""News signals: fetch headlines, keep the ones that state a fuel price, and
turn them into corroborated "signals" for the static API.

What is stored (data/news/items.json) is only the headline, outlet, link and
date of items that mention petrol or diesel together with a naira amount.
Claims are re-derived from the stored headlines at every build, so improving
the extractor improves the whole history.

News signals are published on their own (/v1/news.json) and are never mixed
into the NBS averages.
"""
from __future__ import annotations

import datetime as dt
import email.utils
import hashlib
import json
import logging
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from . import news_extract, sources, states

log = logging.getLogger("fuelapi.news")

# Google News search feeds give the best coverage of price stories across
# outlets. Each query looks back a week by default, so a missed run loses nothing.
GOOGLE_QUERIES = [
    'petrol "pump price" Nigeria',
    "NNPC petrol price litre",
    "Dangote petrol price litre",
    "diesel price litre Nigeria",
    'petrol price litre Lagos OR Abuja OR Kano OR "Port Harcourt"',
    "petrol sells litre filling stations Nigeria",
    '"ex-depot" petrol price',
]
# Outlets' own feeds. Outlets that answer automated readers with 403 (The
# Guardian, TheCable) are left out rather than worked around.
OUTLET_FEEDS = [
    ("Nairametrics", "https://nairametrics.com/feed/"),
    ("Punch", "https://punchng.com/feed/"),
    ("Premium Times", "https://www.premiumtimesng.com/feed"),
    ("Vanguard", "https://www.vanguardngr.com/feed/"),
    ("BusinessDay", "https://businessday.ng/feed/"),
    ("Daily Trust", "https://dailytrust.com/feed/"),
    ("Channels TV", "https://www.channelstv.com/feed/"),
    ("Daily Post", "https://dailypost.ng/feed/"),
    ("Leadership", "https://leadership.ng/feed/"),
]

MAX_FEED_BYTES = 3 * 1024 * 1024
MAX_TITLE_CHARS = 300
POLITE_DELAY_SECONDS = 2.0
WINDOW_DAYS = 45  # how long a signal stays in the published feed
CLUSTER_DAYS = 4  # same claim within this many days is one signal
PUMP_BAND = (0.5, 2.0)  # against the latest NBS state (or national) average
EX_DEPOT_BAND = (0.4, 1.5)  # ex-depot sits below pump; against the national average

NOTE = (
    "Prices stated in news headlines, extracted by rule. They are leads, not "
    "measurements: a headline may quote one station, one city or a wholesale price. "
    "Several outlets carrying the same figure often means one story was republished. "
    "Never blended with the NBS averages."
)


def google_news_url(query: str, days: int = 7) -> str:
    return "https://news.google.com/rss/search?" + urlencode(
        {"q": f"{query} when:{int(days)}d", "hl": "en-NG", "gl": "NG", "ceid": "NG:en"})


def _iso(d: dt.datetime) -> str:
    return d.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_feed(xml_bytes: bytes, default_outlet: str | None, now: dt.datetime) -> list[dict]:
    """Return [{title, outlet, link, published_at}] from an RSS document."""
    root = ET.fromstring(xml_bytes)
    out = []
    for item in root.iter("item"):
        title = " ".join((item.findtext("title") or "").split())
        link = (item.findtext("link") or "").strip()
        source = " ".join((item.findtext("source") or "").split())
        outlet = source or default_outlet
        if source and title.endswith(" - " + source):  # Google News appends the outlet
            title = title[: -len(source) - 3].rstrip()
        if not title or not outlet or len(title) > MAX_TITLE_CHARS:
            continue
        parts = urlsplit(link)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            continue
        try:
            published = email.utils.parsedate_to_datetime(item.findtext("pubDate") or "")
        except (TypeError, ValueError):
            continue
        if published.tzinfo is None:
            published = published.replace(tzinfo=dt.timezone.utc)
        if published > now + dt.timedelta(days=1) or published < now - dt.timedelta(days=90):
            continue
        out.append({"title": title, "outlet": outlet[:80], "link": link[:600],
                    "published_at": _iso(published)})
    return out


def _item_id(item: dict) -> str:
    # The same story reaches us through several queries; outlet + headline is
    # the identity, not the link (Google's redirect links are not stable).
    key = item["outlet"].lower() + "|" + " ".join(item["title"].lower().split())
    return hashlib.sha1(key.encode("utf-8")).hexdigest()[:16]


def load_items(data_dir: Path) -> list[dict]:
    path = data_dir / "news" / "items.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else []


def _save_items(data_dir: Path, items: list[dict]) -> None:
    path = data_dir / "news" / "items.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    items = sorted(items, key=lambda i: (i["published_at"], i["id"]))
    path.write_text(json.dumps(items, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")


def fetch(data_dir: Path, *, session=None, now: dt.datetime | None = None,
          delay: float = POLITE_DELAY_SECONDS, days: int = 7) -> dict:
    """Poll every feed once and store new price headlines.

    `days` is how far back the search feeds look; use a larger value once to
    seed the store, since outlets' own feeds only hold their latest stories.
    """
    now = now or dt.datetime.now(dt.timezone.utc)
    session = session or sources.new_session()
    feeds = [(None, google_news_url(q, days)) for q in GOOGLE_QUERIES] + list(OUTLET_FEEDS)
    known = {i["id"]: i for i in load_items(data_dir)}
    summary = {"feeds_ok": 0, "feeds_failed": [], "headlines_seen": 0, "new_items": 0}

    for n, (outlet, url) in enumerate(feeds):
        if n and delay:
            time.sleep(delay)
        label = outlet or "Google News"
        try:
            r = session.get(url, timeout=(10, 30))
            r.raise_for_status()
            if len(r.content) > MAX_FEED_BYTES:
                raise ValueError("feed is implausibly large")
            entries = parse_feed(r.content, outlet, now)
        except Exception as exc:  # noqa: BLE001 - one dead feed must not stop the rest
            log.warning("feed failed (%s): %s", label, exc)
            summary["feeds_failed"].append(f"{label}: {type(exc).__name__}")
            continue
        summary["feeds_ok"] += 1
        summary["headlines_seen"] += len(entries)
        for entry in entries:
            if not news_extract.extract(entry["title"]).relevant:
                continue
            entry["id"] = _item_id(entry)
            if entry["id"] not in known:
                known[entry["id"]] = entry
                summary["new_items"] += 1

    if summary["feeds_ok"]:
        _save_items(data_dir, list(known.values()))
        state_path = data_dir / "news" / "state.json"
        state_path.write_text(
            json.dumps({"last_fetch_date": now.strftime("%Y-%m-%d")}, indent=2) + "\n",
            encoding="utf-8")
    return summary


# ------------------------------------------------------------------ derive

def _plausible(claim, baseline) -> tuple[bool, dict | None]:
    """Check a claim against the latest NBS figure. `baseline(fuel, state)` -> (price, period)."""
    if claim.price_type == "ex_depot":
        ref, band = baseline(claim.fuel, None), EX_DEPOT_BAND
    else:
        ref, band = baseline(claim.fuel, claim.state) or baseline(claim.fuel, None), PUMP_BAND
    if ref is None:
        return False, None
    price, period = ref
    ok = price * band[0] <= claim.price <= price * band[1]
    return ok, {"price": round(price, 2), "period": period}


def derive(data_dir: Path, baseline) -> dict | None:
    """Build the published document from stored headlines. None if news was never fetched."""
    state_path = data_dir / "news" / "state.json"
    if not state_path.exists():
        return None
    as_of = json.loads(state_path.read_text(encoding="utf-8"))["last_fetch_date"]
    end = dt.datetime.fromisoformat(as_of + "T23:59:59+00:00")
    start = end - dt.timedelta(days=WINDOW_DAYS)

    counts = {"headlines_stored": 0, "headlines_in_window": 0, "claims_published": 0,
              "claims_withheld_low_confidence": 0, "claims_rejected_implausible": 0}
    rows = []  # (claim, item, nbs reference)
    for item in load_items(data_dir):
        counts["headlines_stored"] += 1
        when = dt.datetime.fromisoformat(item["published_at"].replace("Z", "+00:00"))
        if not (start <= when <= end):
            continue
        counts["headlines_in_window"] += 1
        for claim in news_extract.extract(item["title"]).claims:
            if claim.confidence != "high":
                counts["claims_withheld_low_confidence"] += 1
                continue
            ok, ref = _plausible(claim, baseline)
            if not ok:
                counts["claims_rejected_implausible"] += 1
                continue
            counts["claims_published"] += 1
            rows.append((claim, item, ref))

    # Cluster identical claims that appear within a few days of each other.
    rows.sort(key=lambda r: (r[1]["published_at"], r[1]["id"]))
    clusters: list[dict] = []
    open_by_key: dict[tuple, dict] = {}
    for claim, item, ref in rows:
        key = (claim.fuel, claim.price_type, claim.state, claim.marketer, claim.price)
        when = dt.datetime.fromisoformat(item["published_at"].replace("Z", "+00:00"))
        cluster = open_by_key.get(key)
        if cluster is None or when - cluster["_first"] > dt.timedelta(days=CLUSTER_DAYS):
            cluster = {"_first": when, "claim": claim, "ref": ref, "items": [], "claims": []}
            open_by_key[key] = cluster
            clusters.append(cluster)
        if all(i["id"] != item["id"] for i in cluster["items"]):
            cluster["items"].append(item)
        cluster["claims"].append(claim)

    signals = []
    for c in clusters:
        claim, items = c["claim"], c["items"]
        directions = [x.direction for x in c["claims"] if x.direction]
        direction = max(sorted(set(directions)), key=directions.count) if directions else None
        olds = sorted({x.old_price for x in c["claims"] if x.old_price})
        outlets = sorted({i["outlet"] for i in items})
        st = states.BY_SLUG.get(claim.state) if claim.state else None
        signals.append({
            "fuel": claim.fuel,
            "price": claim.price,
            "unit": "NGN/litre",
            "price_type": claim.price_type,
            "price_type_inferred": all(x.price_type_inferred for x in c["claims"]),
            "state": st.name if st else None,
            "state_slug": claim.state,
            "place": claim.place,
            "marketer": claim.marketer,
            "marketer_label": news_extract.MARKETER_LABELS.get(claim.marketer),
            "direction": direction,
            "previous_price": olds[0] if len(olds) == 1 else None,
            "first_reported_at": items[0]["published_at"],
            "last_reported_at": items[-1]["published_at"],
            "outlets": len(outlets),
            "nbs_reference": c["ref"],
            "headlines": [
                {"outlet": i["outlet"], "title": i["title"], "link": i["link"],
                 "published_at": i["published_at"]}
                for i in items[:5]
            ],
        })
    signals.sort(key=lambda s: (s["last_reported_at"], s["price"]), reverse=True)

    by_state: dict[str, list[dict]] = {}
    for s in signals:
        if s["state_slug"]:
            by_state.setdefault(s["state_slug"], []).append(s)
    doc = {
        "as_of": as_of,
        "window_days": WINDOW_DAYS,
        "note": NOTE,
        "method": "rule-based extraction from headlines; only high-confidence claims are published",
        "counts": counts,
        "signals": signals,
    }
    return {"doc": doc, "by_state": by_state, "as_of": as_of}
