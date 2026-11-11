"""Turn everything under data/ into the static JSON API under public/v1.

Three kinds of data, published side by side and never blended:

* NBS workbooks (data/raw, data/sources.json) -> the official monthly averages;
* news headlines (data/news)                  -> /v1/news.json signals;
* community reports (data/community)          -> /v1/community.json.

The build is a pure function of data/: no network, no clock. Running it twice
on the same inputs produces byte-identical output, so git only records a change
when the data changed.
"""
from __future__ import annotations

import calendar
import csv
import json
import shutil
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

from . import community, nbs_parse, news, sources, states

FUELS = {
    "pms": {"label": "Premium Motor Spirit (petrol)", "unit": "NGN/litre", "catalog": 157},
    "ago": {"label": "Automotive Gas Oil (diesel)", "unit": "NGN/litre", "catalog": 158},
}
PUBLISHER = "National Bureau of Statistics (NBS), Nigeria"


def source_for(fuel: str) -> dict:
    return {
        "name": PUBLISHER,
        "series": f"{FUELS[fuel]['label']} Price Watch (monthly state average retail prices)",
        "url": f"https://microdata.nigerianstat.gov.ng/index.php/catalog/{FUELS[fuel]['catalog']}",
    }
FRESHNESS_NOTE = (
    "These are NBS monthly state averages, usually published three to six weeks "
    "after the month ends. They are a baseline, not live pump prices: compare "
    "`period` with today's date before relying on a figure."
)
REVISION_THRESHOLD = 0.01  # report disagreements between workbooks above 1%


@dataclass(frozen=True)
class _Cand:
    price: float
    report_period: str
    key: str


def _r2(x: float) -> float:
    return round(float(x) + 0.0, 2)


def _pct(new: float | None, old: float | None) -> float | None:
    if new is None or old is None or old == 0:
        return None
    return _r2((new - old) / old * 100)


def _shift(period: str, months: int) -> str:
    y, m = int(period[:4]), int(period[5:7])
    idx = y * 12 + (m - 1) + months
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}"


def _gaps(periods: list[str]) -> list[str]:
    """Months between the first and last period that have no data."""
    have, out, p = set(periods), [], periods[0]
    while p < periods[-1]:
        p = _shift(p, 1)
        if p not in have:
            out.append(p)
    return out


def _period_end(period: str) -> str:
    y, m = int(period[:4]), int(period[5:7])
    return f"{period}-{calendar.monthrange(y, m)[1]:02d}"


def _pick(cands: list[_Cand], period: str) -> _Cand:
    """Prefer the value from the month's own report; otherwise the newest report."""
    own = [c for c in cands if c.report_period == period]
    return max(own or cands, key=lambda c: (c.report_period, c.key))


def collect(data_dir: Path) -> dict:
    """Parse every downloaded workbook and resolve overlaps between them."""
    manifest = sources.load_manifest(data_dir / "sources.json")
    state_cands: dict[tuple[str, str, str], list[_Cand]] = defaultdict(list)
    nat_cands: dict[tuple[str, str], list[_Cand]] = defaultdict(list)
    issues: list[dict] = []
    used: dict[str, dict] = {}

    for key in sorted(manifest):
        entry = manifest[key]
        if entry.get("status") != "ok":
            continue
        for f in entry["files"]:
            path = data_dir / f["path"]
            if not path.exists():
                issues.append({"kind": "missing_file", "source": key, "file": f["path"]})
                continue
            try:
                context = tuple(n for n in (entry.get("filename"), entry.get("title")) if n)
                wb = nbs_parse.parse_workbook(path.read_bytes(), f["name"], context_names=context)
            except Exception as exc:  # noqa: BLE001 - one bad workbook must not stop the build
                issues.append({"kind": "unreadable", "source": key, "file": f["name"],
                               "detail": f"{type(exc).__name__}: {exc}"})
                continue
            for s in wb.sheets:
                where = {"source": key, "file": f["name"], "sheet": s.sheet}
                if s.skipped:
                    issues.append({"kind": "sheet_skipped", **where, "detail": s.skipped})
                    continue
                if s.name_mismatch:
                    issues.append({
                        "kind": "quarantined", **where,
                        "detail": "; ".join(s.warnings)
                        + " - not published, because the month cannot be trusted",
                    })
                    continue
                for w in s.warnings:
                    issues.append({"kind": "warning", **where, "detail": w})
                for o in s.observations:
                    state_cands[(o.fuel, o.state, o.period)].append(
                        _Cand(o.price, s.report_period, key))
                for period, price in s.national.items():
                    nat_cands[(s.fuel, period)].append(_Cand(price, s.report_period, key))
                used[key] = entry

    series: dict[str, dict[str, dict[str, float]]] = defaultdict(lambda: defaultdict(dict))
    provenance: dict[tuple[str, str, str], _Cand] = {}
    revisions: dict[tuple[str, str], dict] = {}
    for (fuel, slug, period), cands in sorted(state_cands.items()):
        chosen = _pick(cands, period)
        series[fuel][slug][period] = chosen.price
        provenance[(fuel, slug, period)] = chosen
        worst = max(abs(c.price - chosen.price) / chosen.price for c in cands)
        if worst > REVISION_THRESHOLD:
            rev = revisions.setdefault((fuel, period), {
                "kind": "revision", "fuel": fuel, "period": period,
                "states_affected": 0, "max_difference_pct": 0.0, "sources": set(),
            })
            rev["states_affected"] += 1
            rev["max_difference_pct"] = max(rev["max_difference_pct"], _r2(worst * 100))
            rev["sources"].update(c.key for c in cands)
    for rev in revisions.values():
        rev["sources"] = sorted(rev["sources"])
        rev["detail"] = ("workbooks disagree for this month; the value from the "
                         "month's own report (else the newest report) was used")
        issues.append(rev)

    national = {
        fuel_period: _pick(cands, fuel_period[1]).price
        for fuel_period, cands in nat_cands.items()
    }
    return {
        "series": series, "national": national, "provenance": provenance,
        "issues": issues, "used": used, "manifest": manifest,
    }


def _write(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def build(data_dir: Path, out_dir: Path) -> dict:
    c = collect(data_dir)
    series, national, issues = c["series"], c["national"], c["issues"]
    v1 = out_dir / "v1"
    if v1.exists():
        shutil.rmtree(v1)
    v1.mkdir(parents=True)

    updated = max((e["fetched_at"] for e in c["used"].values()), default=None)
    fuels_present = [f for f in FUELS if series.get(f)]
    written = 0

    def stats_for(fuel: str, period: str) -> dict:
        """Everything derivable about one fuel in one month."""
        prices = {slug: by[period] for slug, by in series[fuel].items() if period in by}
        mean = sum(prices.values()) / len(prices)
        return {"prices": prices, "mean": mean, "reported": national.get((fuel, period))}

    periods_by_fuel: dict[str, list[str]] = {}
    stats: dict[tuple[str, str], dict] = {}
    for fuel in fuels_present:
        periods = sorted({p for by in series[fuel].values() for p in by})
        periods_by_fuel[fuel] = periods
        for p in periods:
            stats[(fuel, p)] = stats_for(fuel, p)

    def headline(fuel: str, period: str) -> float:
        s = stats[(fuel, period)]
        return s["reported"] if s["reported"] is not None else s["mean"]

    def headline_or_none(fuel: str, period: str) -> float | None:
        return headline(fuel, period) if (fuel, period) in stats else None

    def fuel_meta(fuel: str, period: str) -> dict:
        return {
            "fuel": fuel,
            "label": FUELS[fuel]["label"],
            "unit": FUELS[fuel]["unit"],
            "period": period,
            "period_end": _period_end(period),
            "granularity": "monthly state average",
            "source": source_for(fuel),
        }

    # ---- national.json -------------------------------------------------
    national_out = {"data_updated_at": updated, "currency": "NGN",
                    "freshness_note": FRESHNESS_NOTE, "fuels": {}}
    for fuel in fuels_present:
        period = periods_by_fuel[fuel][-1]
        s = stats[(fuel, period)]
        ranked = sorted(s["prices"].items(), key=lambda kv: (kv[1], kv[0]))

        def row(item):
            st = states.BY_SLUG[item[0]]
            return {"state": st.name, "slug": st.slug, "price": _r2(item[1])}

        zones = defaultdict(list)
        for slug, price in s["prices"].items():
            zones[states.BY_SLUG[slug].zone].append(price)
        national_out["fuels"][fuel] = {
            **fuel_meta(fuel, period),
            "national_average": _r2(headline(fuel, period)),
            "national_average_basis": (
                "as reported by NBS" if s["reported"] is not None
                else "unweighted mean of state averages (NBS figure not found)"),
            "mean_of_states": _r2(s["mean"]),
            "states_reported": len(s["prices"]),
            "mom_change_pct": _pct(headline(fuel, period), headline_or_none(fuel, _shift(period, -1))),
            "yoy_change_pct": _pct(headline(fuel, period), headline_or_none(fuel, _shift(period, -12))),
            "highest": [row(i) for i in reversed(ranked[-3:])],
            "lowest": [row(i) for i in ranked[:3]],
            "zones": [
                {"zone": z, "slug": states.ZONE_SLUGS[z], "average": _r2(sum(v) / len(v)),
                 "states": len(v)}
                for z, v in sorted(zones.items())
            ],
        }
    _write(v1 / "national.json", national_out)
    written += 1

    # ---- fuels/{fuel}/latest.json, history.json, history.csv ------------
    for fuel in fuels_present:
        periods = periods_by_fuel[fuel]
        period = periods[-1]
        s = stats[(fuel, period)]
        cheapest_first = sorted(s["prices"].items(), key=lambda kv: (kv[1], kv[0]))
        rank = {slug: i + 1 for i, (slug, _) in enumerate(cheapest_first)}
        _write(v1 / "fuels" / fuel / "latest.json", {
            **fuel_meta(fuel, period),
            "data_updated_at": updated,
            "currency": "NGN",
            "national_average": _r2(headline(fuel, period)),
            "states": [
                {"state": st.name, "slug": st.slug, "zone": st.zone,
                 "price": _r2(s["prices"][st.slug]), "rank_cheapest": rank[st.slug]}
                for st in states.STATES if st.slug in s["prices"]
            ],
        })
        _write(v1 / "fuels" / fuel / "history.json", {
            "fuel": fuel, "label": FUELS[fuel]["label"], "unit": FUELS[fuel]["unit"],
            "currency": "NGN", "source": source_for(fuel), "data_updated_at": updated,
            "note": "Arrays line up with `periods`; null means NBS published no figure.",
            "periods": periods,
            "national_average": [_r2(headline(fuel, p)) for p in periods],
            "states": {
                st.slug: [
                    _r2(series[fuel][st.slug][p]) if p in series[fuel].get(st.slug, {}) else None
                    for p in periods
                ]
                for st in states.STATES
            },
        })
        csv_path = v1 / "fuels" / fuel / "history.csv"
        with csv_path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.writer(fh, lineterminator="\n")
            w.writerow(["period", "state", "slug", "zone", "price_ngn_per_litre"])
            for p in periods:
                for st in states.STATES:
                    price = series[fuel].get(st.slug, {}).get(p)
                    if price is not None:
                        w.writerow([p, st.name, st.slug, st.zone, f"{price:.2f}"])
        written += 3

    # ---- news signals and community reports ------------------------------
    def baseline(fuel: str, slug: str | None):
        """Latest NBS figure for a fuel: (price, period) for a state, or nationally."""
        if fuel not in periods_by_fuel:
            return None
        if slug is None:
            period = periods_by_fuel[fuel][-1]
            return headline(fuel, period), period
        by = series[fuel].get(slug)
        if not by:
            return None
        period = max(by)
        return by[period], period

    news_out = news.derive(data_dir, baseline)
    community_out = community.derive(data_dir)
    if news_out:
        _write(v1 / "news.json", news_out["doc"])
        written += 1
    if community_out:
        _write(v1 / "community.json", community_out["doc"])
        _write(v1 / "community" / "history.json", community_out["history"])
        written += 2

    # ---- prices/{state}.json + states.json ------------------------------
    index_rows = []
    for st in states.STATES:
        fuels_out = {}
        latest_summary = {}
        for fuel in fuels_present:
            by = series[fuel].get(st.slug, {})
            if not by:
                continue
            own_periods = sorted(by)
            period = own_periods[-1]
            s = stats[(fuel, period)]
            cheapest_first = sorted(s["prices"].items(), key=lambda kv: (kv[1], kv[0]))
            rank = [slug for slug, _ in cheapest_first].index(st.slug) + 1
            nat = headline(fuel, period)
            fuels_out[fuel] = {
                "label": FUELS[fuel]["label"],
                "unit": FUELS[fuel]["unit"],
                "latest": {
                    "period": period,
                    "period_end": _period_end(period),
                    "price": _r2(by[period]),
                    "granularity": "monthly state average",
                    "source": source_for(fuel),
                    "is_latest_national_period": period == periods_by_fuel[fuel][-1],
                    "national_average": _r2(nat),
                    "difference_vs_national_pct": _pct(by[period], nat),
                    "rank_cheapest": rank,
                    "states_ranked": len(s["prices"]),
                    "mom_change_pct": _pct(by[period], by.get(_shift(period, -1))),
                    "yoy_change_pct": _pct(by[period], by.get(_shift(period, -12))),
                },
                "history": [{"period": p, "price": _r2(by[p])} for p in own_periods],
            }
            latest_summary[fuel] = {"period": period, "price": _r2(by[period])}
        doc = {
            "state": st.name, "slug": st.slug, "zone": st.zone, "currency": "NGN",
            "data_updated_at": updated, "freshness_note": FRESHNESS_NOTE,
            "fuels": fuels_out,
        }
        # Fresher but weaker evidence, kept apart from the official figures above.
        if news_out:
            doc["news_signals"] = {
                "as_of": news_out["as_of"], "note": news.NOTE,
                "signals": news_out["by_state"].get(st.slug, []),
            }
        if community_out:
            doc["community"] = community_out["by_state"].get(st.slug) or {
                "as_of": community_out["as_of"], "window_hours": community.WINDOW_HOURS, "areas": [],
            }
        _write(v1 / "prices" / f"{st.slug}.json", doc)
        written += 1
        if st.slug == "fct":  # people look for Abuja
            _write(v1 / "prices" / "abuja.json", doc)
            written += 1
        index_rows.append({
            "state": st.name, "slug": st.slug, "zone": st.zone,
            "aliases": list(st.aliases), "url": f"/v1/prices/{st.slug}.json",
            "latest": latest_summary,
        })
    _write(v1 / "states.json", {"data_updated_at": updated, "currency": "NGN",
                                "count": len(index_rows), "states": index_rows})
    written += 1

    # ---- meta + index ---------------------------------------------------
    _write(v1 / "meta" / "sources.json", {
        "note": "Workbooks the current figures were read from.",
        "sources": [
            {"key": k, "origin_url": e["origin_url"], "retrieved_via": e["via"],
             "wayback_timestamp": e.get("wayback_timestamp"),
             "fetched_at": e["fetched_at"],
             "files": [{"name": f["name"], "sha256": f["sha256"]} for f in e["files"]]}
            for k, e in sorted(c["used"].items())
        ],
    })
    order = {"quarantined": 0, "unreadable": 1, "missing_file": 2, "revision": 3,
             "warning": 4, "sheet_skipped": 5}
    issues_sorted = sorted(issues, key=lambda i: (order.get(i["kind"], 9), json.dumps(i, sort_keys=True)))
    _write(v1 / "meta" / "issues.json", {
        "note": "Things the build noticed in the source workbooks. Quarantined "
                "sheets are not published.",
        "counts": {k: sum(1 for i in issues if i["kind"] == k)
                   for k in sorted({i["kind"] for i in issues})},
        "issues": issues_sorted,
    })
    _write(v1 / "index.json", {
        "name": "Nigeria Fuel Prices API",
        "version": "v1",
        "data_updated_at": updated,
        "currency": "NGN",
        "freshness_note": FRESHNESS_NOTE,
        "publisher": PUBLISHER,
        "coverage": {
            fuel: {"label": FUELS[fuel]["label"], "unit": FUELS[fuel]["unit"],
                   "source": source_for(fuel),
                   "first_period": periods_by_fuel[fuel][0],
                   "latest_period": periods_by_fuel[fuel][-1],
                   "months": len(periods_by_fuel[fuel]),
                   "missing_periods": _gaps(periods_by_fuel[fuel])}
            for fuel in fuels_present
        },
        "endpoints": {
            "national": "/v1/national.json",
            "states": "/v1/states.json",
            "state": "/v1/prices/{state-slug}.json",
            "fuel_latest": "/v1/fuels/{fuel}/latest.json",
            "fuel_history": "/v1/fuels/{fuel}/history.json",
            "fuel_history_csv": "/v1/fuels/{fuel}/history.csv",
            "sources": "/v1/meta/sources.json",
            "issues": "/v1/meta/issues.json",
            **({"news_signals": "/v1/news.json"} if news_out else {}),
            **({"community": "/v1/community.json",
                "community_history": "/v1/community/history.json"} if community_out else {}),
        },
    })
    written += 3

    # ---- data/observations.csv: the resolved dataset, for diffing in git --
    obs_path = data_dir / "observations.csv"
    with obs_path.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["fuel", "period", "state", "price", "report_period", "source"])
        for (fuel, slug, period), cand in sorted(
                c["provenance"].items(), key=lambda kv: (kv[0][0], kv[0][2], kv[0][1])):
            w.writerow([fuel, period, slug, f"{cand.price:.2f}", cand.report_period, cand.key])

    return {
        "fuels": {
            fuel: {"first_period": periods_by_fuel[fuel][0],
                   "latest_period": periods_by_fuel[fuel][-1],
                   "months": len(periods_by_fuel[fuel]),
                   "national_average_latest": _r2(headline(fuel, periods_by_fuel[fuel][-1]))}
            for fuel in fuels_present
        },
        "workbooks_used": len(c["used"]),
        "news_signals": len(news_out["doc"]["signals"]) if news_out else None,
        "community_reports": community_out["doc"]["reports_all_time"] if community_out else None,
        "files_written": written,
        "issues": {k: sum(1 for i in issues if i["kind"] == k)
                   for k in sorted({i["kind"] for i in issues})},
    }
