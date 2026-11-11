"""End-to-end: fixtures -> manifest -> public/v1 JSON."""
import hashlib
import io
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from fuelapi import build, sources

FIX = Path(__file__).parent / "fixtures"

# (key, fixture, published filename)
BOOKS = [
    ("microdata-157-1424", "fuel_2026_05.xlsx", "Fuel_MAY_2026.xlsx"),
    ("legacy-pms-oct-2024-report", "pms_2024_10.xlsx", "PMS_OCT_2024_REPORT.xlsx"),
    ("legacy-pms-dec-2023", "pms_2023_12.xlsx", "PMS_DEC_2023.xlsx"),
    ("legacy-pms-april-2021", "pms_2021_04.xlsx", "pms_april_2021.xlsx"),
    ("legacy-ago-apr-2024", "ago_2024_04.xlsx", "AGO_APR_2024.xlsx"),
]


@pytest.fixture()
def site(tmp_path):
    data, out = tmp_path / "data", tmp_path / "public"
    manifest = {}
    for i, (key, fixture, name) in enumerate(BOOKS):
        rel = Path("raw") / "nbs" / f"{key}__{name}"
        (data / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(FIX / fixture, data / rel)
        blob = (data / rel).read_bytes()
        manifest[key] = {
            "key": key, "origin_url": f"https://example.invalid/{name}", "via": "wayback",
            "wayback_timestamp": None, "filename": name, "title": None, "status": "ok",
            "files": [{"path": rel.as_posix(), "name": name,
                       "sha256": hashlib.sha256(blob).hexdigest(), "bytes": len(blob)}],
            "fetched_at": f"2026-11-0{i + 1}T00:00:00Z",
        }
    sources.save_manifest(data / "sources.json", manifest)
    report = build.build(data, out)
    return {"data": data, "out": out, "report": report}


def load(site, rel):
    return json.loads((site["out"] / "v1" / rel).read_text(encoding="utf-8"))


def test_national_summary(site):
    pms = load(site, "national.json")["fuels"]["pms"]
    assert pms["period"] == "2026-05"
    assert pms["period_end"] == "2026-05-31"
    assert pms["national_average"] == 1596.25
    assert pms["states_reported"] == 37
    assert pms["mom_change_pct"] == 4.13  # NBS: +4.13% month on month
    assert pms["yoy_change_pct"] == 55.31  # NBS: +55.31% year on year
    assert [s["state"] for s in pms["highest"]] == ["Edo", "Bauchi", "Benue"]
    assert [s["state"] for s in pms["lowest"]] == ["Adamawa", "Katsina", "Sokoto"]
    assert len(pms["zones"]) == 6


def test_state_file_and_abuja_alias(site):
    lagos = load(site, "prices/lagos.json")
    latest = lagos["fuels"]["pms"]["latest"]
    assert latest["period"] == "2026-05"
    assert latest["price"] == 1561.22
    assert latest["source"]["name"].startswith("National Bureau of Statistics")
    assert latest["states_ranked"] == 37
    history = {h["period"]: h["price"] for h in lagos["fuels"]["pms"]["history"]}
    assert history["2024-10"] == 1080.95
    assert history["2016-01"] > 0
    assert load(site, "prices/abuja.json") == load(site, "prices/fct.json")
    assert load(site, "prices/fct.json")["state"] == "FCT"


def test_quarantined_sheet_is_not_published(site):
    issues = load(site, "meta/issues.json")
    assert issues["counts"]["quarantined"] == 1
    # The only diesel data comes from the October 2024 petrol workbook's diesel sheet.
    ago = load(site, "fuels/ago/history.json")
    assert ago["periods"] == ["2023-10", "2024-09", "2024-10"]
    assert "2024-03" not in ago["periods"]


def test_history_arrays_line_up(site):
    hist = load(site, "fuels/pms/history.json")
    n = len(hist["periods"])
    assert hist["periods"] == sorted(hist["periods"])
    assert len(hist["national_average"]) == n
    assert len(hist["states"]) == 37
    assert all(len(v) == n for v in hist["states"].values())
    csv_lines = (site["out"] / "v1/fuels/pms/history.csv").read_text().splitlines()
    assert csv_lines[0] == "period,state,slug,zone,price_ngn_per_litre"
    assert len(csv_lines) == 1 + 37 * n


def test_states_index_and_latest(site):
    idx = load(site, "states.json")
    assert idx["count"] == 37
    cov = load(site, "index.json")["coverage"]
    assert cov["ago"]["missing_periods"][0] == "2023-11"  # fixtures hold 2023-10, 2024-09, 2024-10
    assert cov["pms"]["latest_period"] == "2026-05"
    latest = load(site, "fuels/pms/latest.json")
    assert sorted(s["rank_cheapest"] for s in latest["states"]) == list(range(1, 38))


def test_build_is_deterministic(site):
    def snapshot():
        return {p.relative_to(site["out"]).as_posix(): p.read_bytes()
                for p in sorted(site["out"].rglob("*")) if p.is_file()}
    first = snapshot()
    build.build(site["data"], site["out"])
    assert snapshot() == first


def test_own_report_wins_over_later_comparison_column(site):
    # 2023-10 appears in the Oct 2024 workbook only as its year-ago column, and
    # 2024-10 only in its own report; both must resolve without error.
    rows = (site["data"] / "observations.csv").read_text().splitlines()
    assert rows[0] == "fuel,period,state,price,report_period,source"
    own = [r for r in rows if r.startswith("pms,2024-10,lagos,")]
    assert own == ["pms,2024-10,lagos,1080.95,2024-10,legacy-pms-oct-2024-report"]


def test_extract_workbooks_from_zip_pdf_and_junk():
    xlsx = (FIX / "fuel_2026_05.xlsx").read_bytes()
    assert sources.extract_workbooks(xlsx, "Fuel MAY 2026.xlsx") == [("Fuel_MAY_2026.xlsx", xlsx)]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("PMS Report MAY 2026.pdf", b"%PDF-1.7 not really")
        z.writestr("../../evil/Fuel_MAY_2026.xlsx", xlsx)  # path must be flattened
        z.writestr("~$lock.xlsx", b"junk")
        z.writestr("notes.xlsx", b"not a workbook")
    assert sources.extract_workbooks(buf.getvalue(), None) == [("Fuel_MAY_2026.xlsx", xlsx)]

    assert sources.extract_workbooks(b"%PDF-1.7 ...", "report.pdf") == []
    with pytest.raises(ValueError):
        sources.extract_workbooks(b"<!DOCTYPE html><html>error</html>", "x.zip")


def test_parse_microdata_listing():
    html = """
    <span class="resource-info" id="1410"> PMS Report April 2026 </span>
    <a target="_blank" href="https://microdata.nigerianstat.gov.ng/index.php/catalog/157/download/1410"
       title="PMS_APRIL_2026.zip" data-filename="PMS_APRIL_2026.zip" data-extension="zip"
       class="download btn btn-outline-primary btn-sm btn-block">Download [ZIP]</a>
    <a href="https://microdata.nigerianstat.gov.ng/index.php/catalog/157/download/1260"
       data-filename="PMS_Report_JUNE_2025.pdf" data-extension="pdf" class="download btn">PDF</a>
    <a href="https://microdata.nigerianstat.gov.ng/index.php/catalog/157/download/1261"
       data-filename="PMS_Report_June_2025.xlsx" data-extension="xlsx" class="download btn">XLSX</a>
    <a href="https://microdata.nigerianstat.gov.ng/index.php/catalog/999/download/5"
       data-filename="other.xlsx" data-extension="xlsx" class="download btn">other study</a>
    """
    found = sources.parse_microdata_listing(html, 157)
    assert [c.key for c in found] == ["microdata-157-1261", "microdata-157-1410"]
    assert found[1].filename == "PMS_APRIL_2026.zip"
    assert found[1].title == "PMS Report April 2026"
    assert found[1].via == "nbs"


def test_news_and_community_are_published_beside_nbs_never_blended(site):
    data, out = site["data"], site["out"]
    # Without news or community data the endpoints simply do not exist.
    assert not (out / "v1/news.json").exists() and not (out / "v1/community.json").exists()
    assert "news_signals" not in load(site, "prices/lagos.json")
    nbs_before = load(site, "prices/lagos.json")["fuels"]
    national_before = (out / "v1/national.json").read_bytes()

    (data / "news").mkdir()
    (data / "news/items.json").write_text(json.dumps([
        {"id": "a", "outlet": "The Trumpet", "link": "https://example.ng/a",
         "published_at": "2026-11-09T08:00:00Z",
         "title": "NNPC Cuts Petrol Price to ₦1,325 in Abuja, ₦1,355 in Lagos as Pump Prices Vary"},
    ]), encoding="utf-8")
    (data / "news/state.json").write_text('{"last_fetch_date": "2026-11-10"}', encoding="utf-8")
    (data / "community").mkdir()
    (data / "community/reports.csv").write_text(
        "id,created_at,area,state,fuel,brand,brand_label,brand_category,price,reporter,checked_against\n"
        "1,2026-11-10T08:00:00.000Z,lagos,lagos,pms,nnpc,NNPC,nnpc,1355.00,aaaaaaaa01,official_average\n"
        "2,2026-11-10T09:00:00.000Z,lagos,lagos,pms,nnpc,NNPC,nnpc,1360.00,aaaaaaaa02,recent_reports\n",
        encoding="utf-8")
    (data / "community/state.json").write_text(
        '{"last_id": 2, "synced_at": "2026-11-10T12:00:00Z"}', encoding="utf-8")

    report = build.build(data, out)
    assert report["news_signals"] == 2 and report["community_reports"] == 2

    lagos = load(site, "prices/lagos.json")
    assert lagos["fuels"] == nbs_before  # the official block is untouched
    assert (out / "v1/national.json").read_bytes() == national_before
    (signal,) = lagos["news_signals"]["signals"]
    assert (signal["price"], signal["marketer"], signal["price_type"]) == (1355.0, "nnpc", "pump")
    assert signal["nbs_reference"] == {"price": 1561.22, "period": "2026-05"}
    assert lagos["community"]["areas"][0]["fuels"]["pms"]["status"] == "corroborated"
    # A state with neither still says so explicitly rather than omitting the keys.
    kano = load(site, "prices/kano.json")
    assert kano["news_signals"]["signals"] == [] and kano["community"]["areas"] == []

    assert load(site, "news.json")["counts"]["claims_published"] == 2
    assert load(site, "community.json")["areas"][0]["fuels"]["pms"]["median"] == 1360.0
    assert load(site, "community/history.json")["days"][0]["date"] == "2026-11-10"
    endpoints = load(site, "index.json")["endpoints"]
    assert endpoints["news_signals"] == "/v1/news.json" and endpoints["community"] == "/v1/community.json"

    snapshot = {p: p.read_bytes() for p in sorted(out.rglob("*")) if p.is_file()}
    build.build(data, out)
    assert {p: p.read_bytes() for p in sorted(out.rglob("*")) if p.is_file()} == snapshot
