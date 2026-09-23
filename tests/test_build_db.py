"""Unit tests for scripts/build_db.py (offline — no database).

Covers the deterministic pure functions — the algorithmic risk
lives here: temporal_val/year/periods, extract_host, normalise_format,
field_value_str, and the views-CSV loader. Edge cases: malformed URLs,
www. hosts, out-of-range ports, IANA media-type URLs, OGC prefixes,
messy temporal values.

The DB write path is verified separately by a scratch-DB table diff
against a full build of the same data.
Run with: uv run pytest tests/test_build_db.py
"""

import json
import os

import pytest
import typer

# The module-level guard fires on import if DATABASE_URL is unset — tests
# never connect, so give it a dummy URL.
os.environ.setdefault("DATABASE_URL", "postgresql://localhost:5432/test-db")

import scripts.build_db as bd


def test_temporal_val():
    assert bd.temporal_val(None) is None
    assert bd.temporal_val("") is None
    assert bd.temporal_val("2012-06-09") == "2012-06-09"
    assert bd.temporal_val(["1960", "2000"]) == "1960, 2000"
    assert bd.temporal_val([]) is None
    assert bd.temporal_val("point") == "point"
    assert bd.temporal_val(v=True) == "true"  # True coerces to 'true'


def test_temporal_year():
    assert bd.temporal_year("2010-04-01") == "2010"
    assert bd.temporal_year("31/07/2015") == "2015"
    assert bd.temporal_year("present") is None
    assert bd.temporal_year("-") is None
    assert bd.temporal_year("19") is None  # needs 4 digits
    assert bd.temporal_year("Months") is None
    assert bd.temporal_year("1499") is None  # below range
    assert bd.temporal_year("2100") is None  # above range
    assert bd.temporal_year("") is None
    assert bd.temporal_year(None) is None
    # first 4-digit year wins
    assert bd.temporal_year("1960 to 1992") == "1960"
    # re.ASCII: fullwidth digits don't match the ASCII \d class
    assert bd.temporal_year("２０１０") is None


def test_temporal_periods():
    # positional pairing of multiple periods
    assert bd.temporal_periods("1960, 2000", "1992, 2016") == [[1960, 1992], [2000, 2016]]
    # reversed pair is swapped
    assert bd.temporal_periods("2016", "2000") == [[2000, 2016]]
    # one-sided periods
    assert bd.temporal_periods("1960", None) == [[1960, None]]
    assert bd.temporal_periods(None, "1992") == [[None, 1992]]
    # nothing on either side -> null
    assert bd.temporal_periods(None, None) is None
    assert bd.temporal_periods("", "") is None
    # junk values yield no years -> null
    assert bd.temporal_periods("present", "ongoing") is None
    # uneven pairing: extra years on one side still pair up positionally
    assert bd.temporal_periods("1960, 2000, 2010", "1992") == [
        [1960, 1992],
        [2000, None],
        [2010, None],
    ]


def test_text_periods():
    # explicit ranges — hyphen, spaces, "to", en/em-dash
    assert bd._text_periods("Something 1838 - 1862") == [[1838, 1862]]
    assert bd._text_periods("2009 to 2010") == [[2009, 2010]]
    assert bd._text_periods("1838–1862") == [[1838, 1862]]  # en dash
    assert bd._text_periods("1838—1862") == [[1838, 1862]]  # em dash
    # 2-digit range tail expanded via its century: 2019-20 -> 2019-2020
    assert bd._text_periods("2019-20 data") == [[2019, 2020]]
    assert bd._text_periods("1990-95") == [[1990, 1995]]
    # reversed range is swapped
    assert bd._text_periods("2016-2000") == [[2000, 2016]]
    # standalone years -> closed single-year periods
    assert bd._text_periods("Road safety 2020 report") == [[2020, 2020]]
    assert bd._text_periods("Data 1960 and 1970") == [[1960, 1960], [1970, 1970]]
    # range years don't double-count as standalone years
    assert bd._text_periods("1960-1970 census") == [[1960, 1970]]
    # dedupe, order preserved
    assert bd._text_periods("2020 and 2020 again") == [[2020, 2020]]
    # junk / blank / None -> empty
    assert bd._text_periods("quarterly returns") == []
    assert bd._text_periods("data 20") == []  # 2-digit year is not a year
    assert bd._text_periods("1400 data 2100") == []  # out of range
    assert bd._text_periods("") == []
    assert bd._text_periods(None) == []
    # capped at 10 periods
    assert len(bd._text_periods(" ".join(str(y) for y in range(1990, 2003)))) == 10


def test_suggested_periods():
    # title first (high confidence)
    ds = {"title": "Road casualties 2020", "resources": [{"name": "file.csv"}]}
    assert bd._suggested_periods(ds) == ([[2020, 2020]], "title")
    # no title year -> resource-name fallback, union across all resources
    ds = {
        "title": "Quarterly returns",
        "resources": [
            {"name": "2021 - 2023_0300_S3.pdf"},
            {"name": "notes 2019 to 2020.xlsx"},
        ],
    }
    assert bd._suggested_periods(ds) == ([[2021, 2023], [2019, 2020]], "resource")
    # nothing anywhere -> (None, None)
    assert bd._suggested_periods({"title": "No years", "resources": [{"name": "file.pdf"}]}) == (None, None)
    assert bd._suggested_periods({"title": "No years"}) == (None, None)


def test_dataset_period_rows():
    # declared periods win — suggested never alongside
    ds = {
        "id": "d1",
        "title": "Road casualties 2020",
        "temporal_coverage-from": "1960, 2000",
        "temporal_coverage-to": "1992, 2016",
        "resources": [],
    }
    assert bd._dataset_period_rows(ds) == [
        ("d1", 0, 1960, 1992, "declared"),
        ("d1", 1, 2000, 2016, "declared"),
    ]
    # declared junk -> suggested fallback
    ds2 = {
        "id": "d2",
        "title": "Census 2021",
        "temporal_coverage-from": "present",
        "temporal_coverage-to": "ongoing",
        "resources": [],
    }
    assert bd._dataset_period_rows(ds2) == [("d2", 0, 2021, 2021, "title")]
    # nothing -> no rows
    assert bd._dataset_period_rows({"id": "d3", "title": "No years", "resources": [{"name": "file.pdf"}]}) == []


def test_extract_host():
    # plain host, lowercased
    assert bd.extract_host("http://Example.COM/path") == "example.com"
    # leading www. stripped
    assert bd.extract_host("http://www.example.com/x") == "example.com"
    # port stripped (valid port)
    assert bd.extract_host("http://example.com:8080/path") == "example.com"
    # userinfo stripped
    assert bd.extract_host("http://user:pass@example.com:90/x") == "example.com"
    # trailing dot kept
    assert bd.extract_host("http://example.com.") == "example.com."
    # unencoded space -> URL() throws -> loose regex -> bad chars -> null
    assert bd.extract_host("http://Table 1 stuff") is None
    # angle brackets -> null
    assert bd.extract_host("http://<div class=x>") is None
    # non-numeric port -> rejected by the port probe -> regex captures
    # host:port -> ':' is a bad char -> null
    assert bd.extract_host("http://example.com:abc") is None
    # out-of-range port -> the port probe rejects it (urlsplit would
    # otherwise accept the hostname — the probe closes the gap)
    assert bd.extract_host("http://example.com:99999") is None
    # scheme-less URL -> the loose regex needs scheme:// -> null
    assert bd.extract_host("www.example.com") is None
    assert bd.extract_host("datasets/foo.csv") is None
    # malformed percent in path is tolerated; hostname is fine
    assert bd.extract_host("http://example.com/%zz") == "example.com"
    # blank / non-string
    assert bd.extract_host("") is None
    assert bd.extract_host(None) is None
    assert bd.extract_host(42) is None
    # IPv6 hosts: '[' / ':' are bad chars either way, so they reject to null
    assert bd.extract_host("http://[::1]:8080/") is None
    # file scheme hostname is accepted (WHATWG-style URL parsing)
    assert bd.extract_host("file://web2/some/path") == "web2"
    # ...but Windows local paths have an empty authority in WHATWG: the
    # drive letter is part of the path, not the host -> null
    assert bd.extract_host(r"file://\\J:\National\Designations") is None
    assert bd.extract_host("file:///C:/Users/cashmorea/Desktop/x.csv") is None
    assert bd.extract_host("file:/x") is None
    assert bd.extract_host("file:host/path") is None
    # WHATWG leniency for special schemes: backslashes are slashes, so the
    # authority starts at the first host char after http://
    assert bd.extract_host(r"http://\\Xswhc.nhs.uk\\groups\\file.csv") == "xswhc.nhs.uk"
    assert bd.extract_host(r"http:\\host\\path") == "host"
    # single slash after the scheme (http:/host) — urlsplit would see no
    # netloc; WHATWG enters the authority anyway
    assert bd.extract_host("http:/www.northlincolnshireccg.nhs.uk/data/x.csv") == ("northlincolnshireccg.nhs.uk")
    assert bd.extract_host("https:/data./rochdale.gov.uk/files/x.csv") == "data."
    # zero slashes after the scheme (http:host)
    assert bd.extract_host("http:www.buckshealthcare.nhs.uk/About/foi.htm") == ("buckshealthcare.nhs.uk")
    # triple slash (https:///host) collapses to the authority too
    assert bd.extract_host("https:///maps.dartmoor.gov.uk/geoserver/x") == ("maps.dartmoor.gov.uk")
    # unicode hosts are punycoded via IDNA (the WHATWG URL spec emits xn--)
    assert bd.extract_host("http://Spend-over-£25k-report-May-19.pdf") == ("xn--spend-over-25k-report-may-19-l3a.pdf")


def test_normalise_format():
    assert bd.normalise_format(None) is None
    assert bd.normalise_format("") is None
    assert bd.normalise_format("   ") is None
    # trim + collapse + uppercase
    assert bd.normalise_format("  csv ") == "CSV"
    # MIME type by name
    assert bd.normalise_format("application/zip") == "ZIP"
    assert bd.normalise_format("text/csv") == "CSV"
    assert bd.normalise_format("application/ld+json") == "JSON-LD"
    # IANA media-type URL -> MIME -> name
    assert bd.normalise_format("https://www.iana.org/assignments/media-types/text/csv") == "CSV"
    assert (
        bd.normalise_format(
            "http://www.iana.org/assignments/media-types/application/geopackage+sqlite3",
        )
        == "GPKG"
    )
    # dot strip (so "application/vnd.ms-excel" folds to its bare form)
    assert bd.normalise_format("application/vnd.ms-excel") == "APPLICATION/VNDMS-EXCEL"
    # OGC prefix dropped for the W* trio
    assert bd.normalise_format("OGC WMS") == "WMS"
    assert bd.normalise_format("OGC WFS") == "WFS"
    assert bd.normalise_format("OGC WMTS") == "WMTS"
    # ...but not for non-trio values (mechanical strip only applies to the trio)
    assert bd.normalise_format("OGC WCS") == "OGC WCS"
    # typos -> aliases
    assert bd.normalise_format("CVS") == "CSV"
    assert bd.normalise_format("XLSX ") == "XLSX"
    assert bd.normalise_format("xlxs") == "XLSX"
    # ArcGIS Hub bucket
    assert bd.normalise_format("ArcGIS GeoServices REST API") == "ARCGIS REST"
    assert bd.normalise_format("FEATURE SERVER") == "ARCGIS REST"
    # portal-page bucket
    assert bd.normalise_format("WEB PAGE") == "WEB PAGE"
    assert bd.normalise_format("URL") == "WEB PAGE"
    # JSON-stat endpoints
    assert bd.normalise_format("json1.0") == "JSON-STAT"
    assert bd.normalise_format("json2.0") == "JSON-STAT"
    # multi-format label left alone (after mechanical cleaning)
    assert bd.normalise_format("CSV / ZIP") == "CSV / ZIP"
    # rare formats keep their cleaned label
    assert bd.normalise_format("CITYGML") == "CITYGML"
    # JSON value (non-string) -> null like JS typeof check
    assert bd.normalise_format(123) is None


def test_field_value_str():
    assert bd.field_value_str(None) == "(empty)"
    assert bd.field_value_str("") == "(empty)"
    assert bd.field_value_str("hello") == "hello"
    assert bd.field_value_str("x" * 501) == "x" * 500 + "..."
    assert bd.field_value_str(v=True) == "true"
    assert bd.field_value_str(1.0) == "1"
    assert bd.field_value_str(3) == "3"
    assert bd.field_value_str([]) == "(empty)"
    assert bd.field_value_str([1, 2]) == "[1,2]"  # compact separators
    assert bd.field_value_str({}) == "(empty)"
    assert bd.field_value_str({"a": 1}) == '{"a":1}'
    # long arrays/objects truncated at 500 (no '...' suffix — only strings
    # get the ellipsis); compact separators like JSON.stringify
    compact = json.dumps(list(range(100)), separators=(",", ":"))
    assert bd.field_value_str(list(range(100))) == compact[:500]
    truncated = bd.field_value_str(list(range(1000)))
    assert len(truncated) == 500
    assert not truncated.endswith("...")


def test_load_views_csv(tmp_path, monkeypatch):
    # missing file -> empty dict
    monkeypatch.setattr(bd, "VIEWS_FILE", tmp_path / "nope.csv")
    assert bd.load_views_csv() == {}

    # happy path: extracts UUIDs from dataset paths, sums clicks
    csv_file = tmp_path / "views.csv"
    csv_file.write_text(
        "path,clicks\n"
        "/dataset/aaaaaaaa-1111-2222-3333-444444444444/slug,10\n"
        "/dataset/aaaaaaaa-1111-2222-3333-444444444444/slug,5\n"
        "/dataset/bbbbbbbb-1111-2222-3333-444444444444/other,3\n"
        "/,100\n"  # homepage — skipped
        "/search?q=foo,7\n"  # search — skipped
        "/dataset/bbbbbbbb-1111-2222-3333-444444444444/other,0\n",  # zero — skipped
        encoding="utf-8",
    )
    monkeypatch.setattr(bd, "VIEWS_FILE", csv_file)
    result = bd.load_views_csv()
    assert result == {
        "aaaaaaaa-1111-2222-3333-444444444444": 15,
        "bbbbbbbb-1111-2222-3333-444444444444": 3,
    }


def test_load_harvest_sources(tmp_path, monkeypatch):
    # happy path: reads and parses the file
    sources = [{"id": "src-1", "title": "One", "url": "https://x/1.xml"}]
    f = tmp_path / "harvest_sources.json"
    f.write_text(json.dumps(sources), encoding="utf-8")
    monkeypatch.setattr(bd, "HARVEST_SOURCES_FILE", f)
    assert bd._load_harvest_sources() == sources


def test_load_harvest_sources_missing_file(tmp_path, monkeypatch, capsys):
    # missing file -> friendly error + exit 1 (points at get-harvest-sources)
    monkeypatch.setattr(
        bd,
        "HARVEST_SOURCES_FILE",
        tmp_path / "harvest_sources.json",
    )
    with pytest.raises(typer.Exit) as exc:
        bd._load_harvest_sources()
    assert exc.value.exit_code == 1
    err = capsys.readouterr().err
    assert "get-harvest-sources" in err


def test_load_harvest_sources_bad_json(tmp_path, monkeypatch):
    # unparseable file -> same friendly error path
    f = tmp_path / "harvest_sources.json"
    f.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(bd, "HARVEST_SOURCES_FILE", f)
    with pytest.raises(typer.Exit) as exc:
        bd._load_harvest_sources()
    assert exc.value.exit_code == 1
