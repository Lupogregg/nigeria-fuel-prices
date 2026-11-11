"""News extraction tests. The headlines are real ones, collected from Google
News and outlet RSS feeds in October-November 2026."""
import datetime as dt
import json

import pytest

from fuelapi import news, news_extract

NOW = dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.timezone.utc)


def claims(title):
    return [(c.fuel, c.price, c.price_type, c.state, c.marketer, c.confidence)
            for c in news_extract.extract(title).claims]


def published(title):
    return [c for c in claims(title) if c[5] == "high"]


# ------------------------------------------------------------ correct claims

@pytest.mark.parametrize("title,expected", [
    ("NNPC Cuts Petrol Price to ₦1,325 in Abuja, ₦1,355 in Lagos as Pump Prices Vary Across Stations",
     [("pms", 1325.0, "pump", "fct", "nnpc", "high"), ("pms", 1355.0, "pump", "lagos", "nnpc", "high")]),
    ("Petrol climbs to N1,370/litre in Lagos following fresh depot price increases of N75",
     [("pms", 1370.0, "pump", "lagos", None, "high")]),
    ("Dangote Refinery reduces petrol gantry price to N1,215 per litre",
     [("pms", 1215.0, "ex_depot", None, "dangote", "high")]),
    # The refinery sells wholesale; NNPC and MRS in the second clause are not the seller.
    ("Dangote Cuts Petrol Price to N1,215 as NNPC, MRS, Other Filling Stations Prepare New Rates",
     [("pms", 1215.0, "ex_depot", None, "dangote", "high")]),
    ("NNPCL Raises Petrol Price to ₦1,405 Per Litre Amid Fuel Discount Dispute",
     [("pms", 1405.0, "pump", None, "nnpc", "high")]),
    # The marketer is in a different clause from the price, and "by N30" is a change.
    ("N1,405: NNPC Increases Petrol Price by N30, Marketers Release New Rates",
     [("pms", 1405.0, "pump", None, "nnpc", "high")]),
    ("Petrol Ex-depot Price Now N1,380/Litre Despite Federal Government’s Price Peg",
     [("pms", 1380.0, "ex_depot", None, None, "high")]),
    # Second price belongs to "other outlets", not to NNPC.
    ("Plateau motorists pay ₦1,420 for petrol at NNPC stations as other outlets charge ₦1,492",
     [("pms", 1420.0, "pump", "plateau", "nnpc", "high"), ("pms", 1492.0, "pump", "plateau", None, "high")]),
    ("‘Discounted’ fuel price: Nigerians decry NNPCL’s sale of fuel at N1,405",
     [("pms", 1405.0, "pump", None, "nnpc", "high")]),
])
def test_real_headlines_with_a_price(title, expected):
    assert claims(title) == expected


def test_direction_and_previous_price():
    (c,) = news_extract.extract("NNPC slashes petrol pump price from N1,420 to N1,355 in Lagos").claims
    assert (c.price, c.old_price, c.direction, c.state) == (1355.0, 1420.0, "down", "lagos")
    (c,) = news_extract.extract("Dangote Refinery Raises Petrol Price To N1,350 Per Litre").claims
    assert c.direction == "up" and c.per_litre_stated and c.price_type_inferred


def test_diesel_and_mixed_fuel_headlines():
    assert claims("Diesel sells for N1,850 per litre in Kano filling stations") == [
        ("ago", 1850.0, "pump", "kano", None, "high")]
    mixed = news_extract.extract("2027: Tinubu’s panic can’t erase N1,400 petrol, N2,000 diesel prices")
    assert [(c.fuel, c.price) for c in mixed.claims] == [("pms", 1400.0), ("ago", 2000.0)]
    assert all(c.confidence == "low" for c in mixed.claims)  # rhetoric: nothing anchors it


# -------------------------------------------- amounts that are not prices

@pytest.mark.parametrize("title,reason", [
    ("No more N1,420: NNPC, MRS, AP reduce petrol prices, new rates emerge", "a previous price"),
    ("No more N1,365: NNPC reverses increase, announces new petrol pump prices", "a previous price"),
    ("Petrol could hit N1,411/Litre in November as freight, insurance premiums bite – Analyst",
     "a prediction or demand"),
    ("Marketers Reject Government’s Plan to Cap Petrol Price at ₦1,350", "a prediction or demand"),
    ("Nigerian Petroleum Marketers Reject N1,350 Government Price Cap on Petrol", "an announced or capped"),
    ("Fuel price update in Nigeria weeks after Tinubu’s N1,350/litre announcement", "an announced or capped"),
    ("Fuel Price: Marketers react as FG’s N1,350/litre petrol discount yet to reflect", "the size of a change"),
    ("Landing cost of petrol falls to N1,283.81/litre", "a landing or import cost"),
    ("Marketers Unveil Cheaper Petrol Price as Landing Cost Crashes By N227 Below Dangote’s Rate",
     "the size of a change"),
    ("Relief as petrol prices drop by N85, states with biggest cuts emerge", "too small"),
    ("N66 petrol discount: Group praises NNPCL, makes fresh demand", "too small"),
    ("Telcos face 110 percent diesel cost surge, invest N4trn in two years – ALTON", "a sum of money"),
    ("Cooking gas now N1,500 per kg as petrol queues return", "not priced per litre"),
])
def test_real_headlines_without_a_usable_price(title, reason):
    ex = news_extract.extract(title)
    assert ex.claims == []
    assert any(reason in i["reason"] for i in ex.ignored), ex.ignored


@pytest.mark.parametrize("title", [
    "NNPCL reduces petrol pump price after Dangote Refinery's cut",  # no figure
    "FCCPC To Monitor Petrol Pump Price As Oil Dips Below $90",  # dollars
    "OGRA Cuts Petrol Price To Rs362, Diesel To Rs387.81 Per Litre",  # Pakistan
    "Exclusive: Dangote Refinery IPO attracts N5.85 trillion in bids from nearly 3.8m applicants",
    "N210trn not missing from NNPC – Mele Kyari tells Senate",
    "Tinubu: Whoever says fuel subsidy will return is lying",
])
def test_irrelevant_headlines(title):
    ex = news_extract.extract(title)
    assert ex.claims == [] and not ex.relevant


def test_only_high_confidence_claims_would_be_published():
    # Unanchored: no seller, no place.
    assert published("Petrol hits N1,370/litre as depot prices rise") == []
    assert claims("Petrol hits N1,370/litre as depot prices rise")[0][5] == "medium"
    # A marketer used as a yardstick is not the seller.
    assert published("N1,293/litre: List of 10 marketers selling petrol below Dangote’s price") == []
    assert published("N1,350/Litre: Marketers Announce New Position on FG’s Petrol Price Control") == []


def test_place_names_that_are_not_states():
    assert claims("Petrol sells at N1,400 per litre in Niger Delta communities")[0][3] is None
    assert claims("Petrol sells at N1,400 per litre in Niger")[0][3] == "niger"
    assert claims("Petrol sells for N1,450 per litre in Port Harcourt")[0][3] == "rivers"
    assert claims("Petrol sells for N1,450 per litre in Benin Republic border towns")[0][3] is None
    # lower-case "delta"/"rivers"/"plateau" are ordinary words
    assert claims("NNPC sells petrol at N1,400 as rivers flood delta communities")[0][3] is None


# ------------------------------------------------------------- feed parsing

RSS = """<?xml version="1.0" encoding="UTF-8"?>
<rss version="2.0"><channel><title>x</title>
<item><title>NNPC Cuts Petrol Price to ₦1,325 in Abuja, ₦1,355 in Lagos - The Trumpet</title>
  <link>https://news.google.com/rss/articles/abc</link>
  <pubDate>Mon, 09 Nov 2026 08:00:00 GMT</pubDate><source url="https://thetrumpet.ng">The Trumpet</source></item>
<item><title>NNPC cuts petrol price to ₦1,325 in  Abuja, ₦1,355 in Lagos - The Trumpet</title>
  <link>https://news.google.com/rss/articles/def</link>
  <pubDate>Mon, 09 Nov 2026 09:00:00 GMT</pubDate><source url="https://thetrumpet.ng">The Trumpet</source></item>
<item><title>Lagos NNPC petrol now N1,355 - Daily Example</title>
  <link>https://news.google.com/rss/articles/ghi</link>
  <pubDate>Tue, 10 Nov 2026 07:00:00 GMT</pubDate><source url="https://example.ng">Daily Example</source></item>
<item><title>Old petrol story N1,300 in Lagos - Daily Example</title><link>https://example.ng/old</link>
  <pubDate>Mon, 09 Feb 2026 08:00:00 GMT</pubDate><source url="https://example.ng">Daily Example</source></item>
<item><title>Bad link petrol N1,300 in Lagos - Daily Example</title><link>javascript:alert(1)</link>
  <pubDate>Mon, 09 Nov 2026 08:00:00 GMT</pubDate><source url="https://example.ng">Daily Example</source></item>
<item><title>No date petrol N1,300 in Lagos - Daily Example</title><link>https://example.ng/nodate</link>
  <source url="https://example.ng">Daily Example</source></item>
<item><title>Senate passes budget - Daily Example</title><link>https://example.ng/budget</link>
  <pubDate>Mon, 09 Nov 2026 08:00:00 GMT</pubDate><source url="https://example.ng">Daily Example</source></item>
</channel></rss>""".encode("utf-8")


def test_parse_feed_strips_outlet_suffix_and_drops_bad_entries():
    entries = news.parse_feed(RSS, None, NOW)
    titles = [e["title"] for e in entries]
    assert titles[0] == "NNPC Cuts Petrol Price to ₦1,325 in Abuja, ₦1,355 in Lagos"
    assert "Senate passes budget" in titles  # relevance is decided later, not here
    assert not any("Old petrol" in t or "Bad link" in t or "No date" in t for t in titles)
    assert entries[0]["outlet"] == "The Trumpet"
    assert entries[0]["published_at"] == "2026-11-09T08:00:00Z"


class FakeResponse:
    def __init__(self, content=b"", status=200, payload=None):
        self.content, self.status_code, self._payload = content, status, payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


class FeedSession:
    """Serves RSS for Google queries, fails one outlet, empty feeds elsewhere."""
    def __init__(self):
        self.calls = []

    def get(self, url, **kw):
        self.calls.append(url)
        if "news.google.com" in url:
            return FakeResponse(RSS)
        if "punchng.com" in url:
            return FakeResponse(status=403)
        return FakeResponse(b"<rss><channel></channel></rss>")


def baseline(fuel, state):
    table = {("pms", None): (1596.25, "2026-05"), ("pms", "lagos"): (1561.22, "2026-05"),
             ("pms", "fct"): (1602.64, "2026-05")}
    return table.get((fuel, state))


def test_fetch_stores_only_price_headlines_and_derive_clusters_them(tmp_path):
    session = FeedSession()
    summary = news.fetch(tmp_path, session=session, now=NOW, delay=0)
    assert summary["feeds_failed"] == ["Punch: RuntimeError"]  # one dead feed does not stop the rest
    assert summary["feeds_ok"] == len(news.GOOGLE_QUERIES) + len(news.OUTLET_FEEDS) - 1
    assert "when%3A7d" in session.calls[0]

    stored = news.load_items(tmp_path)
    # Same outlet + same headline (case/spacing aside) is one item, however many queries return it.
    assert sorted(i["title"] for i in stored) == [
        "Lagos NNPC petrol now N1,355",
        "NNPC Cuts Petrol Price to ₦1,325 in Abuja, ₦1,355 in Lagos",
    ]
    assert news.fetch(tmp_path, session=FeedSession(), now=NOW, delay=0)["new_items"] == 0

    out = news.derive(tmp_path, baseline)
    doc = out["doc"]
    assert doc["as_of"] == "2026-11-10"
    by_key = {(s["state_slug"], s["price"]): s for s in doc["signals"]}
    lagos = by_key[("lagos", 1355.0)]
    assert lagos["outlets"] == 2  # two different outlets carried the same figure
    assert lagos["marketer"] == "nnpc" and lagos["price_type"] == "pump"
    assert lagos["nbs_reference"] == {"price": 1561.22, "period": "2026-05"}
    assert lagos["first_reported_at"] == "2026-11-09T08:00:00Z"
    assert lagos["last_reported_at"] == "2026-11-10T07:00:00Z"
    assert by_key[("fct", 1325.0)]["outlets"] == 1
    assert [s["state_slug"] for s in out["by_state"]["lagos"]] == ["lagos"]
    assert doc["counts"]["claims_published"] == 3


def test_derive_rejects_implausible_prices_and_expires_old_items(tmp_path):
    (tmp_path / "news").mkdir()
    items = [
        {"id": "a", "outlet": "X", "link": "https://x.ng/1", "published_at": "2026-11-09T08:00:00Z",
         "title": "NNPC sells petrol at N9,500 per litre in Lagos"},  # far outside the NBS band
        {"id": "b", "outlet": "X", "link": "https://x.ng/2", "published_at": "2026-08-01T08:00:00Z",
         "title": "NNPC sells petrol at N1,300 per litre in Lagos"},  # older than the window
        {"id": "c", "outlet": "X", "link": "https://x.ng/3", "published_at": "2026-11-09T08:00:00Z",
         "title": "NNPC sells petrol at N1,300 per litre in Lagos"},
    ]
    (tmp_path / "news" / "items.json").write_text(json.dumps(items), encoding="utf-8")
    (tmp_path / "news" / "state.json").write_text('{"last_fetch_date": "2026-11-10"}', encoding="utf-8")
    doc = news.derive(tmp_path, baseline)["doc"]
    assert [s["price"] for s in doc["signals"]] == [1300.0]
    assert doc["counts"]["claims_rejected_implausible"] == 1
    assert doc["counts"]["headlines_in_window"] == 2


def test_derive_is_none_until_news_has_been_fetched(tmp_path):
    assert news.derive(tmp_path, baseline) is None
