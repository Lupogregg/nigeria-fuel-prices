"""Parser tests against real NBS workbooks (tests/fixtures) and synthetic edge cases."""
import datetime as dt
from pathlib import Path

import pytest

from fuelapi import nbs_parse, states

FIX = Path(__file__).parent / "fixtures"
TODAY = dt.date(2026, 11, 10)


def parse(fixture: str, published_name: str):
    return nbs_parse.parse_workbook((FIX / fixture).read_bytes(), published_name, TODAY)


def mean(sheet, period):
    vals = [o.price for o in sheet.observations if o.period == period]
    return sum(vals) / len(vals)


def test_states_lookup_handles_nbs_spellings():
    assert states.lookup("Abuja").slug == "fct"
    assert states.lookup("Nassarawa").slug == "nasarawa"
    assert states.lookup(" Akwa-Ibom ").slug == "akwa-ibom"
    assert states.lookup("Cross River State").slug == "cross-river"
    assert states.lookup("NORTH CENTRAL") is None
    assert states.lookup("AVERAGE") is None
    assert states.lookup(None) is None
    assert len({s.slug for s in states.STATES}) == 37


def test_may_2026_matches_published_headline():
    (sheet,) = parse("fuel_2026_05.xlsx", "Fuel_MAY_2026.xlsx").sheets
    assert sheet.fuel == "pms"
    assert sheet.report_period == "2026-05"
    assert sheet.periods == ["2025-05", "2026-04", "2026-05"]
    assert len(sheet.observations) == 3 * 37
    # NBS press figures: N1,596.25 (May 2026), N1,532.93 (April), N1,027.76 (May 2025)
    assert sheet.national["2026-05"] == pytest.approx(1596.25, abs=0.005)
    assert sheet.national["2026-04"] == pytest.approx(1532.93, abs=0.005)
    assert sheet.national["2025-05"] == pytest.approx(1027.76, abs=0.005)
    assert mean(sheet, "2026-05") == pytest.approx(sheet.national["2026-05"], abs=0.01)
    assert not sheet.name_mismatch and not sheet.warnings


def test_workbook_with_title_rows_and_a_diesel_sheet():
    wb = parse("pms_2024_10.xlsx", "PMS_OCT_2024_REPORT.xlsx")
    by_fuel = {s.fuel: s for s in wb.sheets}
    assert set(by_fuel) == {"pms", "ago"}
    pms, ago = by_fuel["pms"], by_fuel["ago"]
    assert pms.national["2024-10"] == pytest.approx(1184.83, abs=0.005)
    lagos = {o.period: o.price for o in pms.observations if o.state == "lagos"}
    assert lagos["2024-10"] == pytest.approx(1080.95, abs=0.005)
    # Diesel sheet interleaves zone subtotal rows; they must not become states.
    assert len([o for o in ago.observations if o.period == "2024-10"]) == 37
    assert ago.national["2024-10"] == pytest.approx(1441.28, abs=0.005)
    assert mean(ago, "2024-10") == pytest.approx(1441.28, abs=0.01)


def test_old_wide_time_series_ignores_ranking_tables_below():
    (sheet,) = parse("pms_2021_04.xlsx", "pms april 2021.xlsx").sheets
    assert sheet.fuel == "pms"  # sheet is called "Sheet1": fuel comes from the filename
    assert sheet.report_period == "2021-04"
    assert len(sheet.periods) > 40
    assert sheet.periods[0] == "2016-01" and sheet.periods[-1] == "2021-04"
    for p in sheet.periods:
        assert len([o for o in sheet.observations if o.period == p]) == 37
    assert not any("duplicate" in w for w in sheet.warnings)


def test_header_on_first_row():
    (sheet,) = parse("pms_2023_12.xlsx", "PMS_DEC_2023.xlsx").sheets
    assert sheet.periods == ["2022-12", "2023-11", "2023-12"]
    assert sheet.national["2023-12"] == pytest.approx(671.86, abs=0.005)


def test_stale_header_is_flagged_for_quarantine():
    # Published as the April 2024 diesel workbook, but its columns stop at March.
    (sheet,) = parse("ago_2024_04.xlsx", "AGO_APR_2024.xlsx").sheets
    assert sheet.fuel == "ago"
    assert sheet.name_mismatch is True


def test_cooking_gas_reads_the_12_5kg_table_not_the_5kg_one():
    (sheet,) = parse("gas_2024_03.xlsx", "GAS_PRICE_WATCH_MARCH_2024.xlsx").sheets
    assert sheet.fuel == "lpg"
    assert sheet.periods == ["2023-03", "2024-02", "2024-03"]
    assert len(sheet.observations) == 3 * 37
    # NBS press figure for refilling a 12.5 kg cylinder, March 2024: N15,929.04
    assert sheet.national["2024-03"] == pytest.approx(15929.04, abs=0.005)
    assert mean(sheet, "2024-03") == pytest.approx(sheet.national["2024-03"], rel=0.01)


def test_cooking_gas_12kg_label_typo_is_read_as_12_5kg():
    (sheet,) = parse("lpg_2026_01.xlsx", "GAS_PRICE_WATCH_JANUARY_2026_table.xlsx").sheets
    assert sheet.skipped is None
    assert sheet.report_period == "2026-01"
    assert all(o.price > 10000 for o in sheet.observations)  # not the 5 kg table


def test_kerosene_reads_the_litre_table_not_the_gallon_one():
    (sheet,) = parse("dpk_2026_05.xlsx", "HOUSEHOLD KEROSENE MAY 2026.xlsx").sheets
    assert sheet.fuel == "dpk"
    assert sheet.report_period == "2026-05"
    assert len(sheet.observations) == 3 * 37
    assert sheet.national["2026-05"] == pytest.approx(2971.94, abs=0.005)
    assert all(o.price < 5000 for o in sheet.observations)  # gallons are ~4x


def test_two_unit_sheet_without_unit_labels_is_skipped_not_guessed():
    rows = [("", "Jan-26", "Feb-26", "", "", "Jan-26", "Feb-26")]
    rows += [(st.name, 1000.0, 1100.0, "", st.name, 4000.0, 4400.0) for st in states.STATES]
    s = nbs_parse.parse_sheet("Sheet1", rows, "KEROSENE FEB 2026.xlsx", TODAY)
    assert s.observations == []
    assert "litre" in s.skipped


@pytest.mark.parametrize("text,expected", [
    ("PMS_OCT_2024_REPORT.xlsx", "2024-10"),
    ("Fuel_October 2024", "2024-10"),
    ("pms april 2021.xlsx", "2021-04"),
    ("PMS Fuel sept 2017.xlsx", "2017-09"),
    ("Fuel_MAY_2026.xlsx", "2026-05"),
    ("PMS_Report_AUG_SEPT_2025.zip", None),  # two months: ambiguous
    ("DIESEL JUNE 2015 - JULY 2022.xlsx", None),
    ("Sheet1", None),
    ("PMS Report March 2025.xlsx", "2025-03"),  # "Report" must not read as a month
])
def test_period_from_text(text, expected):
    assert nbs_parse.period_from_text(text) == expected


@pytest.mark.parametrize("text,expected", [
    ("Fuel_October 2024", "pms"),
    ("PREMIUM MOTOR SPIRIT (PETROL) PRICE WATCH", "pms"),
    ("DIESEL OCTOBER 2024", "ago"),
    ("AUTOMOTIVE GAS OIL (DIESEL)", "ago"),  # must not be read as cooking gas
    ("GAS_PRICE_WATCH_MARCH_2024", "lpg"),
    ("NHK APR 2024", "dpk"),
    ("Sheet1", None),
])
def test_detect_fuel(text, expected):
    assert nbs_parse.detect_fuel(text) == expected


def _rows(n_states=37, bad=None):
    header = ("State", dt.datetime(2026, 5, 1), dt.datetime(2026, 6, 1), None, "Zone")
    rows = [("PREMIUM MOTOR SPIRIT",), header]
    for i, st in enumerate(states.STATES[:n_states]):
        rows.append((st.name, 1000.0 + i, 1100.0 + i, None, None))
    if bad:
        rows[2] = (rows[2][0], *bad, None, None)
    rows.append(("AVERAGE", 1018.0, 1118.0, None, None))
    rows.append(("Year on Year", None, 55.3, None, None))
    return rows


def test_month_with_too_few_states_is_dropped():
    sheet = nbs_parse.parse_sheet("Fuel", _rows(n_states=12), "x.xlsx", TODAY)
    assert sheet.observations == []
    assert sheet.skipped


def test_out_of_range_values_are_dropped_with_a_warning():
    sheet = nbs_parse.parse_sheet("Fuel", _rows(bad=(1000.0, 4.13)), "x.xlsx", TODAY)
    june = [o for o in sheet.observations if o.period == "2026-06"]
    assert len(june) == 36
    assert any("outside" in w for w in sheet.warnings)


def test_text_prices_with_commas_are_read():
    sheet = nbs_parse.parse_sheet("Fuel", _rows(bad=("1,050.00", "N1,150.50")), "x.xlsx", TODAY)
    abia = {o.period: o.price for o in sheet.observations if o.state == "abia"}
    assert abia == {"2026-05": 1050.0, "2026-06": 1150.5}
