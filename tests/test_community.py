"""Community reports: sync from the bot's export endpoint, and summary rules
that must match the Worker's (bot/src/core.js) exactly."""
import datetime as dt
import json
from pathlib import Path

import pytest

from fuelapi import community

CASES = json.loads(
    (Path(__file__).parent.parent / "bot" / "test" / "fixtures" / "summary_cases.json")
    .read_text(encoding="utf-8"))
NOW = dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.timezone.utc)


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
def test_summary_matches_the_worker(case):
    # Same input, same output as core.js `summarize` (which generated `expected`).
    reports = [{**r, "reporter": r["reporter_id"]} for r in case["reports"]]
    assert community.summarize(reports) == case["expected"]


def export_row(i, **over):
    row = {
        "id": i, "reporter_id": "ab" * 16, "area": "lagos", "state": "lagos", "brand": "nnpc",
        "brand_label": "NNPC", "brand_category": "nnpc", "fuel": "pms", "price": 1250,
        "status": "accepted", "reject_reason": None, "checked_against": "official_average",
        "baseline_price": 1561.22, "baseline_period": "2026-05",
        "created_at": "2026-11-10T09:00:00.000Z",
    }
    row.update(over)
    return row


class ExportSession:
    """Mimics the Worker's GET /export/reports paging."""
    def __init__(self, rows, token="secret", page=2):
        self.rows, self.token, self.page, self.calls = rows, token, page, []

    def get(self, url, params=None, headers=None, timeout=None):
        self.calls.append((url, dict(params)))
        session = self

        class R:
            status_code = 200 if headers.get("Authorization") == f"Bearer {session.token}" else 401

            def raise_for_status(self):
                if self.status_code != 200:
                    raise RuntimeError("HTTP 401")

            def json(self):
                todo = [r for r in session.rows if r["id"] > params["after_id"]][: session.page]
                more = len(todo) == session.page
                return {"reports": todo, "next_after_id": todo[-1]["id"] if more else None}

        return R()


def test_sync_appends_accepted_reports_and_resumes(tmp_path):
    rows = [
        export_row(1),
        export_row(2, status="rejected", reject_reason="out_of_bounds", price=100),
        export_row(3, reporter_id="cd" * 16, price=1255, created_at="2026-11-10T10:00:00.000Z"),
        export_row(4, area="port-harcourt", state="rivers", brand="other-danmarna-oil",
                   brand_label="Danmarna Oil", brand_category="independent", price=1450),
        export_row(5, fuel="ago", price=1700),
    ]
    session = ExportSession(rows)
    s = community.sync(tmp_path, "https://bot.example.workers.dev/", "secret", session=session, now=NOW)
    assert s == {"fetched": 5, "accepted": 4, "skipped_not_accepted": 1, "skipped_invalid": 0}
    assert session.calls[0] == ("https://bot.example.workers.dev/export/reports", {"after_id": 0, "limit": 1000})
    assert [c[1]["after_id"] for c in session.calls] == [0, 2, 4]  # paged by id

    stored = community.load_reports(tmp_path)
    assert [r["id"] for r in stored] == [1, 3, 4, 5]
    assert stored[0]["reporter"] == "ab" * 16 and stored[0]["price"] == 1250.0
    state = json.loads((tmp_path / "community" / "state.json").read_text())
    assert state == {"last_id": 5, "synced_at": "2026-11-10T12:00:00Z"}  # rejected ids advance the cursor too

    # Next run: only what is new.
    rows.append(export_row(6, price=1260, created_at="2026-11-10T11:00:00.000Z"))
    s2 = community.sync(tmp_path, "https://bot.example.workers.dev", "secret", session=ExportSession(rows), now=NOW)
    assert s2["fetched"] == 1 and s2["accepted"] == 1
    assert [r["id"] for r in community.load_reports(tmp_path)] == [1, 3, 4, 5, 6]


def test_sync_refuses_bad_token_and_non_https(tmp_path):
    with pytest.raises(RuntimeError):
        community.sync(tmp_path, "https://bot.example.workers.dev", "wrong",
                       session=ExportSession([export_row(1)]), now=NOW)
    assert not (tmp_path / "community" / "reports.csv").exists()
    with pytest.raises(ValueError):
        community.sync(tmp_path, "http://bot.example.workers.dev", "secret",
                       session=ExportSession([]), now=NOW)


@pytest.mark.parametrize("bad", [
    {"price": 50}, {"price": "abc"}, {"price": 99999}, {"fuel": "lpg"}, {"state": "atlantis"},
    {"area": "Lagos; DROP"}, {"brand": "../etc"}, {"reporter_id": "not-hex"},
    {"created_at": "yesterday"}, {"id": 0}, {"id": "x"},
])
def test_clean_row_rejects_malformed_rows(bad):
    assert community.clean_row(export_row(1, **bad)) is None


def test_clean_row_neutralises_labels():
    row = community.clean_row(export_row(1, brand_label='=HYPERLINK("http://x")', brand_category="weird"))
    assert row["brand_label"] == "HYPERLINK(httpx)"  # no leading "=", no quotes or slashes
    assert row["brand_category"] == "independent"
    assert community.clean_row(export_row(1, brand_label="---"))["brand_label"] == "nnpc"


def test_derive_window_history_and_state_view(tmp_path):
    rows = [
        export_row(1, created_at="2026-11-06T09:00:00.000Z", price=1300),  # older than 72h
        export_row(2, created_at="2026-11-09T09:00:00.000Z", price=1250),
        export_row(3, created_at="2026-11-10T09:00:00.000Z", price=1255, reporter_id="cd" * 16),
        export_row(4, created_at="2026-11-10T09:30:00.000Z", area="abuja", state="fct",
                   brand="totalenergies", brand_label="TotalEnergies", brand_category="major", price=1310),
    ]
    community.sync(tmp_path, "https://bot.example.workers.dev", "secret", session=ExportSession(rows), now=NOW)
    out = community.derive(tmp_path)
    doc = out["doc"]
    assert doc["as_of"] == "2026-11-10T12:00:00Z" and doc["window_hours"] == 72
    assert doc["reports_all_time"] == 4
    lagos = next(a for a in doc["areas"] if a["area"] == "lagos")
    assert lagos["state"] == "lagos" and lagos["baseline_url"] == "/v1/prices/lagos.json"
    pms = lagos["fuels"]["pms"]
    assert pms["status"] == "corroborated" and pms["reports"] == 2  # the 6 Nov report has expired
    assert pms["brands"][0]["price"] == 1255.0

    assert out["by_state"]["fct"]["areas"][0]["label"] == "Abuja"
    days = {(d["date"], d["area"]): d for d in out["history"]["days"]}
    assert days[("2026-11-06", "lagos")]["median"] == 1300.0  # history keeps what the window dropped
    assert days[("2026-11-10", "lagos")]["reporters"] == 1
    assert len(days) == 4


def test_derive_is_none_until_the_bot_has_been_synced(tmp_path):
    assert community.derive(tmp_path) is None
