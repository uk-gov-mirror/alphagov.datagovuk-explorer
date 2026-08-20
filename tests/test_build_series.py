"""Unit tests for scripts/build_series.py (offline — no database).

Covers the deterministic algorithmic core per the plan:
- strip_date: all 5 DATE_PATTERNS, first-match-wins ordering, empty-root
  rejection, re.ASCII faithfulness (fullwidth digits don't match
  the ASCII digit class)
- build_all_series: template vs timeseries typing, date-suffix clusters,
  root-length cutoff, Phase 1/Phase 2 overlap, counts, insertion order

The DB write path is verified separately by a scratch-DB table diff
against a full build of the same data.
Run with: uv run pytest tests/test_build_series.py
"""

import os

# The module-level guard fires on import if DATABASE_URL is unset — tests
# never connect, so give it a dummy URL.
os.environ.setdefault("DATABASE_URL", "postgresql://localhost:5432/test-db")

import scripts.build_series as bs


def row(id_, title, org="council-a", org_name="Council A"):
    return {
        "id": id_,
        "title": title,
        "org_slug": org,
        "org_display_name": org_name,
    }


def test_strip_date_patterns():
    # 1: 2020/21, 2020-21, 2020-2021 (with and without spaces)
    assert bs.strip_date("Planning Applications 2020/21") == {
        "root": "Planning Applications",
        "date": "2020/21",
    }
    assert bs.strip_date("Planning Applications 2020-21") == {
        "root": "Planning Applications",
        "date": "2020-21",
    }
    assert bs.strip_date("Planning Applications 2020-2021") == {
        "root": "Planning Applications",
        "date": "2020-2021",
    }
    assert bs.strip_date("Planning Applications 2020 / 21") == {
        "root": "Planning Applications",
        "date": "2020 / 21",
    }
    # 2: Q-prefix quarters (optional /YY suffix)
    assert bs.strip_date("Quarterly Report Q4 2020") == {
        "root": "Quarterly Report",
        "date": "Q4 2020",
    }
    # first-match-wins: pattern 1 eats the trailing " 2020/21" before
    # pattern 2 gets a look — root keeps the Q1 prefix
    assert bs.strip_date("Quarterly Report Q1 2020/21") == {
        "root": "Quarterly Report Q1",
        "date": "2020/21",
    }
    # 3: month names
    assert bs.strip_date("Statistics January 2020") == {
        "root": "Statistics",
        "date": "January 2020",
    }
    assert bs.strip_date("Statistics December 2024") == {
        "root": "Statistics",
        "date": "December 2024",
    }
    # 4: plain year, optional parens; 19xx/20xx only
    assert bs.strip_date("Report 2020") == {"root": "Report", "date": "2020"}
    assert bs.strip_date("Report (2020)") == {"root": "Report", "date": "(2020)"}
    assert bs.strip_date("Report 1999") == {"root": "Report", "date": "1999"}
    assert bs.strip_date("Report 2100") is None  # outside 19xx/20xx
    # 5: year in parens at the very end
    assert bs.strip_date("Something (2020)") == {"root": "Something", "date": "(2020)"}
    # date string is trimmed: trailing spaces after the year are stripped
    assert bs.strip_date("Report 2020  ") == {"root": "Report", "date": "2020"}
    print("ok: strip_date all five patterns + non-matches")


def test_strip_date_rejects():
    # no date-like suffix at all
    assert bs.strip_date("Planning Applications") is None
    # year is not at the end
    assert bs.strip_date("2020 Annual Report") is None
    # three-digit year
    assert bs.strip_date("Report 999") is None
    # empty root -> None
    assert bs.strip_date(" 2020") is None
    assert bs.strip_date("(2020)") is None
    # misspelled month: pattern 3 skips, but pattern 4 still catches the
    # plain " 2020" at the end
    assert bs.strip_date("Report Januarry 2020") == {
        "root": "Report Januarry",
        "date": "2020",
    }
    # no date at the end at all
    assert bs.strip_date("Januarry 2020 Report") is None
    # Q0 / Q5 not valid quarters — and no bare year at the end to catch
    assert bs.strip_date("Report Q5 2020 onwards") is None
    print("ok: strip_date rejections")


def test_strip_date_ascii():
    # re.ASCII: fullwidth digits must NOT match \d (the regex is ASCII-only).
    # ２０２０ is U+FF10..U+FF13 — Python \d without the flag would match it.
    assert bs.strip_date("Report ２０２０") is None
    assert bs.strip_date("Report ２０２０/２１") is None
    # real ASCII digits still match
    assert bs.strip_date("Report 2020") == {"root": "Report", "date": "2020"}
    print("ok: strip_date re.ASCII (fullwidth digits ignored)")


def test_exact_duplicates():
    rows = [
        row("a1", "Planning Applications", "council-a", "Council A"),
        row("a2", "Planning Applications", "council-a", "Council A"),
        row("a3", "Planning Applications", "council-b", "Council B"),
        row("b1", "Unique Title", "council-a", "Council A"),
    ]
    series, exact, date = bs.build_all_series(rows)
    # two exact groups of 2+: "Planning Applications" (2 orgs -> template)
    # and... "Unique Title" has 1 -> skipped
    assert exact == 1
    assert date == 0
    assert len(series) == 1
    s = series[0]
    assert s["root_title"] == "Planning Applications"
    assert s["type"] == "template"  # two orgs
    assert [d["id"] for d in s["datasets"]] == ["a1", "a2", "a3"]
    assert "date" not in s["datasets"][0]  # Phase 1 rows carry no date
    print("ok: exact duplicates -> template across orgs")


def test_single_org_timeseries():
    rows = [
        row("a1", "Planning Applications 2020"),
        row("a2", "Planning Applications 2021"),
        row("a3", "Planning Applications 2022"),
    ]
    series, exact, date = bs.build_all_series(rows)
    assert exact == 0
    assert date == 1
    s = series[0]
    assert s["root_title"] == "Planning Applications"
    assert s["type"] == "timeseries"  # all same org
    dates = [d["date"] for d in s["datasets"]]
    assert dates == ["2020", "2021", "2022"]
    # original (untrimmed) titles preserved in the junction rows
    assert [d["title"] for d in s["datasets"]] == [
        "Planning Applications 2020",
        "Planning Applications 2021",
        "Planning Applications 2022",
    ]
    print("ok: date-suffix cluster -> timeseries with dates")


def test_exact_duplicates_single_org_timeseries():
    # same title, all one org -> timeseries, not template
    rows = [
        row("a1", "Weekly Report", "council-a"),
        row("a2", "Weekly Report", "council-a"),
    ]
    series, exact, _ = bs.build_all_series(rows)
    assert exact == 1
    assert series[0]["type"] == "timeseries"
    print("ok: exact duplicates, one org -> timeseries")


def test_root_length_cutoff():
    # root shorter than 5 chars is skipped in Phase 2
    rows = [
        row("a1", "R 2020"),
        row("a2", "R 2021"),
    ]
    series, exact, date = bs.build_all_series(rows)
    assert exact == 0
    assert date == 0
    assert series == []
    # exactly 5 chars passes
    rows = [
        row("a1", "Stats 2020"),
        row("a2", "Stats 2021"),
    ]
    _, _, date = bs.build_all_series(rows)
    assert date == 1
    print("ok: Phase 2 root-length >= 5 cutoff")


def test_phase_overlap():
    # "Planning Applications" is BOTH an exact
    # template across many councils AND a single council's year-by-year
    # timeseries. Overlap is intentional — separate series entries.
    rows = [
        # exact-duplicate template: same title, 3 councils
        row("t1", "Planning Applications", "council-a", "Council A"),
        row("t2", "Planning Applications", "council-b", "Council B"),
        row("t3", "Planning Applications", "council-c", "Council C"),
        # Wigan year-by-year: date suffix, same org
        row("w1", "Planning Applications 2020", "wigan", "Wigan Council"),
        row("w2", "Planning Applications 2021", "wigan", "Wigan Council"),
        row("w3", "Planning Applications 2022", "wigan", "Wigan Council"),
    ]
    series, exact, date = bs.build_all_series(rows)
    assert exact == 1
    assert date == 1
    assert len(series) == 2
    # Phase 1 first, then Phase 2 (insertion order — fixes SERIAL ids)
    assert series[0]["root_title"] == "Planning Applications"
    assert series[0]["type"] == "template"
    assert series[1]["root_title"] == "Planning Applications"
    assert series[1]["type"] == "timeseries"
    assert [d["id"] for d in series[1]["datasets"]] == ["w1", "w2", "w3"]
    print("ok: Phase 1/Phase 2 overlap -> separate series entries")


def test_order_and_counts():
    rows = [
        row("a1", "Beta Report 2020"),
        row("a2", "Beta Report 2021"),
        row("a3", "Alpha"),  # inserted after Beta rows, but exact group sorts first
        row("a4", "Alpha"),
        row("c1", "Gamma Report 2020"),
        row("c2", "Gamma Report 2021"),
    ]
    series, exact, date = bs.build_all_series(rows)
    assert exact == 1  # Alpha
    assert date == 2  # Beta Report, Gamma Report
    # Phase 1 groups first (exact_groups insertion order), then Phase 2
    # (root_groups insertion order): Alpha, Beta Report, Gamma Report
    assert [s["root_title"] for s in series] == [
        "Alpha",
        "Beta Report",
        "Gamma Report",
    ]
    assert [s["type"] for s in series] == ["timeseries", "timeseries", "timeseries"]
    print("ok: series ordering (Phase 1 then Phase 2) and counts")
