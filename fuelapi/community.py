"""Community reports: copy accepted reports out of the Telegram bot's database
into git, and summarise them for the static API.

The Worker (bot/) is the write side. Each build pulls what it has accepted
since the last run through its /export/reports endpoint, appends it to
data/community/reports.csv, and publishes /v1/community.json. The summary rules
here mirror bot/src/core.js `summarize`; both are checked against the same
fixture (bot/test/fixtures/summary_cases.json) so they cannot drift apart.
"""
from __future__ import annotations

import csv
import datetime as dt
import json
import logging
import math
import re
from pathlib import Path

from . import sources, states

log = logging.getLogger("fuelapi.community")

WINDOW_HOURS = 72
AGREE_TOLERANCE = 0.015
FIELDS = ["id", "created_at", "area", "state", "fuel", "brand", "brand_label",
          "brand_category", "price", "reporter", "checked_against"]
NOTE = (
    f"Crowd-sourced pump prices reported through Telegram in the {WINDOW_HOURS} hours before "
    "`as_of`. \"corroborated\" means two or more different reporters agree within 1.5%; "
    "\"unverified\" is a single report. Not official data, and never blended with the NBS averages."
)

_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
_HEX = re.compile(r"^[0-9a-f]{8,64}$")
_TIME = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$")


def clean_row(raw: dict) -> dict | None:
    """Validate one exported report. The Worker is ours, but its rows began as
    text typed by strangers, so nothing is trusted on the way into git."""
    try:
        rid = int(raw["id"])
        price = round(float(raw["price"]), 2)
        area, state, brand = str(raw["area"]), str(raw["state"]), str(raw["brand"])
        fuel, created, reporter = str(raw["fuel"]), str(raw["created_at"]), str(raw["reporter_id"])
    except (KeyError, TypeError, ValueError):
        return None
    if rid <= 0 or not math.isfinite(price) or not (100 <= price <= 10000):
        return None
    if fuel not in ("pms", "ago") or state not in states.BY_SLUG:
        return None
    if not (_SLUG.match(area) and _SLUG.match(brand) and _HEX.match(reporter) and _TIME.match(created)):
        return None
    label = re.sub(r"[^A-Za-z0-9 .()&-]", "", str(raw.get("brand_label") or ""))[:40]
    label = re.sub(r"^[^A-Za-z0-9]+", "", label).strip() or brand  # no leading = + - @ in a CSV cell
    category = str(raw.get("brand_category") or "")
    checked = str(raw.get("checked_against") or "")
    return {
        "id": rid, "created_at": created, "area": area, "state": state, "fuel": fuel,
        "brand": brand, "brand_label": label,
        "brand_category": category if category in ("nnpc", "major", "independent") else "independent",
        "price": price, "reporter": reporter,
        "checked_against": checked if checked in ("recent_reports", "official_average", "bounds_only") else "",
    }


def load_reports(data_dir: Path) -> list[dict]:
    path = data_dir / "community" / "reports.csv"
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    for r in rows:
        r["id"] = int(r["id"])
        r["price"] = float(r["price"])
    return rows


def _load_state(data_dir: Path) -> dict | None:
    path = data_dir / "community" / "state.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else None


def sync(data_dir: Path, base_url: str, token: str, *, session=None,
         now: dt.datetime | None = None) -> dict:
    """Append reports accepted since the last sync. Returns a small summary."""
    if not re.match(r"^https://|^http://(localhost|127\.0\.0\.1)(:\d+)?(/|$)", base_url):
        raise ValueError("COMMUNITY_EXPORT_URL must be an https:// URL")
    session = session or sources.new_session()
    now = now or dt.datetime.now(dt.timezone.utc)
    state = _load_state(data_dir) or {"last_id": 0}
    after_id = int(state["last_id"])
    summary = {"fetched": 0, "accepted": 0, "skipped_not_accepted": 0, "skipped_invalid": 0}
    new_rows = []

    url = base_url.rstrip("/") + "/export/reports"
    for _ in range(200):  # hard stop: 200 pages of 1000
        r = session.get(url, params={"after_id": after_id, "limit": 1000},
                        headers={"Authorization": f"Bearer {token}"}, timeout=(10, 60))
        r.raise_for_status()
        page = r.json()
        for raw in page.get("reports", []):
            summary["fetched"] += 1
            try:
                after_id = max(after_id, int(raw["id"]))
            except (KeyError, TypeError, ValueError):
                summary["skipped_invalid"] += 1
                continue
            if raw.get("status") != "accepted":
                summary["skipped_not_accepted"] += 1
                continue
            row = clean_row(raw)
            if row is None:
                summary["skipped_invalid"] += 1
                continue
            new_rows.append(row)
            summary["accepted"] += 1
        if not page.get("next_after_id"):
            break

    folder = data_dir / "community"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "reports.csv"
    is_new = not path.exists()
    if new_rows or is_new:
        with path.open("a", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=FIELDS, lineterminator="\n")
            if is_new:
                w.writeheader()
            w.writerows(sorted(new_rows, key=lambda r: r["id"]))
    (folder / "state.json").write_text(json.dumps({
        "last_id": after_id,
        "synced_at": now.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }, indent=2) + "\n", encoding="utf-8")
    return summary


# --------------------------------------------------------------- summarise

def _median(values: list[float]) -> float | None:
    if not values:
        return None
    s = sorted(values)
    mid = len(s) // 2
    if len(s) % 2:
        return s[mid]
    return math.floor((s[mid - 1] + s[mid]) / 2 * 100 + 0.5) / 100  # round half up, as JS does


def _latest_per_reporter(reports: list[dict]) -> list[dict]:
    newest: dict[tuple, dict] = {}
    for r in reports:
        key = (r["area"], r["fuel"], r["brand"], r["reporter"])
        seen = newest.get(key)
        if seen is None or (r["created_at"], r["id"]) > (seen["created_at"], seen["id"]):
            newest[key] = r
    return list(newest.values())


def summarize(reports: list[dict]) -> dict:
    """{area: {fuel: {...}}} - the same rules as bot/src/core.js `summarize`."""
    groups: dict[tuple, list[dict]] = {}
    for r in _latest_per_reporter(reports):
        groups.setdefault((r["area"], r["fuel"], r["brand"]), []).append(r)

    out: dict[str, dict] = {}
    for (area, fuel, _brand), rows in groups.items():
        rows.sort(key=lambda r: (r["created_at"], r["id"]), reverse=True)
        head = rows[0]
        agreeing = [r for r in rows if abs(r["price"] - head["price"]) <= head["price"] * AGREE_TOLERANCE]
        entry = out.setdefault(area, {}).setdefault(fuel, {"brands": [], "_rows": []})
        entry["brands"].append({
            "brand": head["brand"],
            "label": head["brand_label"],
            "category": head["brand_category"],
            "price": head["price"],
            "status": "corroborated" if len(agreeing) >= 2 else "unverified",
            "reporters_agreeing": len(agreeing),
            "reporters_total": len(rows),
            "reported_at": head["created_at"],
        })
        entry["_rows"].extend(rows)

    for fuels in out.values():
        for fuel, entry in fuels.items():
            rows = entry.pop("_rows")
            brands = sorted(entry["brands"], key=lambda b: (b["price"], b["brand"]))
            prices = [b["price"] for b in brands]
            times = sorted(r["created_at"] for r in rows)
            fuels[fuel] = {
                "status": "corroborated" if any(b["status"] == "corroborated" for b in brands) else "unverified",
                "unit": "NGN/litre",
                "median": _median(prices),
                "min": min(prices),
                "max": max(prices),
                "reports": len(rows),
                "reporters": len({r["reporter"] for r in rows}),
                "oldest_report_at": times[0],
                "newest_report_at": times[-1],
                "brands": brands,
            }
    return out


def _label(area: str) -> str:
    return " ".join(w.capitalize() for w in area.split("-"))


def derive(data_dir: Path) -> dict | None:
    """Build the published documents. None if the bot has never been synced."""
    state = _load_state(data_dir)
    if state is None or "synced_at" not in state:
        return None
    reports = load_reports(data_dir)
    as_of = state["synced_at"]
    cutoff = (dt.datetime.fromisoformat(as_of.replace("Z", "+00:00"))
              - dt.timedelta(hours=WINDOW_HOURS)).strftime("%Y-%m-%dT%H:%M:%S")
    current = summarize([r for r in reports if cutoff <= r["created_at"][:19] <= as_of[:19]])

    area_state = {r["area"]: r["state"] for r in reports}
    areas = [
        {"area": area, "label": _label(area), "state": area_state[area],
         "baseline_url": f"/v1/prices/{area_state[area]}.json", "fuels": current.get(area, {})}
        for area in sorted(area_state)
    ]

    # One row per area, fuel and day: the median across brands of each
    # reporter's last report that day.
    by_day: dict[tuple, list[dict]] = {}
    for r in reports:
        by_day.setdefault((r["area"], r["fuel"], r["created_at"][:10]), []).append(r)
    history = []
    for (area, fuel, day), rows in sorted(by_day.items()):
        s = summarize(rows)[area][fuel]
        history.append({"date": day, "area": area, "state": area_state[area], "fuel": fuel,
                        "median": s["median"], "min": s["min"], "max": s["max"],
                        "reports": s["reports"], "reporters": s["reporters"]})

    by_state: dict[str, dict] = {}
    for a in areas:
        if a["fuels"]:
            by_state.setdefault(a["state"], {"as_of": as_of, "window_hours": WINDOW_HOURS, "areas": []})
            by_state[a["state"]]["areas"].append(
                {"area": a["area"], "label": a["label"], "fuels": a["fuels"]})
    doc = {
        "as_of": as_of, "window_hours": WINDOW_HOURS, "note": NOTE,
        "reports_all_time": len(reports),
        "areas": areas,
    }
    history_doc = {
        "as_of": as_of,
        "note": "Daily medians of accepted community reports, per pilot area and fuel (UTC days).",
        "days": history,
    }
    return {"doc": doc, "history": history_doc, "by_state": by_state, "as_of": as_of}
