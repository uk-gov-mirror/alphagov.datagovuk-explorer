"""Unit tests for scripts/query_datasets.py (offline — no network).

Covers the deterministic parts per the plan:
- find_org: exact name/id, case-insensitive partial, ambiguous, no match
- load_orgs: missing file / invalid JSON / non-list JSON -> None
- parse_sort: default, missing direction, bogus field/direction errors
- format_org_line: the `name \t (N datasets) \t display_name` line
- format_dataset: the per-dataset stdout block
- get_datasets: URL/params + HTTP/success:false error paths (mock transport)
- CLI error paths: no input, bogus sort, ambiguous fuzzy match, --list

The happy-path CLI against the live API is verified separately by a manual
run against the real API.
Run with: uv run pytest tests/test_query_datasets.py
"""

import json
import os
import tempfile
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import scripts.query_datasets as qd

SAMPLE_ORGS = [
    {
        "name": "ons",
        "id": "a1",
        "display_name": "Office for National Statistics",
        "package_count": 2000,
    },
    {"name": "ons-2", "id": "a2", "display_name": "ONS Beta", "package_count": 3},
    {"name": "defra", "id": "a3", "display_name": "DEFRA", "package_count": 100},
]


@contextmanager
def chdir(path):
    old = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def test_find_org():
    # exact match on name
    assert qd.find_org("ons", SAMPLE_ORGS)["name"] == "ons"
    # exact match on id
    assert qd.find_org("a3", SAMPLE_ORGS)["name"] == "defra"
    # case-insensitive partial on display_name
    assert qd.find_org("national statistics", SAMPLE_ORGS)["name"] == "ons"
    # case-insensitive partial on name
    assert qd.find_org("ONS-2", SAMPLE_ORGS)["name"] == "ons-2"
    # no match -> None (caller falls back to the raw input)
    assert qd.find_org("zzz", SAMPLE_ORGS) is None
    # ambiguous: "o" partial-matches both ons and ons-2
    with pytest.raises(qd.AmbiguousOrgError) as exc:
        qd.find_org("o", SAMPLE_ORGS)
    assert exc.value.name_or_id == "o"
    assert [m["name"] for m in exc.value.matches] == ["ons", "ons-2"]
    print("ok: find_org exact / partial / ambiguous / none")


def test_load_orgs():
    with tempfile.TemporaryDirectory() as d:
        # missing file -> None
        assert qd.load_orgs(Path(d) / "nope.json") is None
        # valid list
        p = Path(d) / "orgs.json"
        p.write_text('[{"name": "a"}]', encoding="utf-8")
        assert qd.load_orgs(p) == [{"name": "a"}]
        # parsed JSON that is not a list -> None
        p2 = Path(d) / "obj.json"
        p2.write_text('{"not": "a list"}', encoding="utf-8")
        assert qd.load_orgs(p2) is None
        # invalid JSON -> None
        p3 = Path(d) / "bad.json"
        p3.write_text("{oops", encoding="utf-8")
        assert qd.load_orgs(p3) is None
    print("ok: load_orgs missing / non-list / invalid JSON")


def test_parse_sort():
    assert qd.parse_sort(None) == ("metadata_modified", "desc")
    assert qd.parse_sort("") == ("metadata_modified", "desc")  # empty -> default
    assert qd.parse_sort("views_total") == ("views_total", "desc")  # :desc appended
    assert qd.parse_sort("title_string:asc") == ("title_string", "asc")
    # bogus field — message lists the valid fields
    with pytest.raises(qd.InvalidSortError, match='Invalid sort field: "bogus"') as exc:
        qd.parse_sort("bogus:desc")
    assert "score" in str(exc.value)
    assert "views_recent" in str(exc.value)
    # bogus direction
    with pytest.raises(
        qd.InvalidSortError,
        match=r'Invalid sort direction: "sideways"\. Use asc or desc\.',
    ):
        qd.parse_sort("name:sideways")
    # empty direction (trailing colon)
    with pytest.raises(qd.InvalidSortError):
        qd.parse_sort("name:")
    print("ok: parse_sort default / append desc / field+dir rejection")


def test_format_org_line():
    # 0 is a real count; display_name fallback to name
    assert (
        qd.format_org_line({"name": "ons", "package_count": 0, "display_name": "ONS"}) == "ons  \t(0 datasets)  \tONS"
    )
    # missing count -> '?'
    assert qd.format_org_line({"name": "defra", "display_name": "Defra"}) == "defra  \t(? datasets)  \tDefra"
    # missing display_name -> name; trailing spaces preserved (real data)
    assert qd.format_org_line({"name": "x", "package_count": 5}) == "x  \t(5 datasets)  \tx"
    print("ok: format_org_line")


def test_format_dataset():
    long_notes = "first line\n" + "x" * 200  # >150 chars, newline in the middle
    ds = {
        "name": "my-dataset",
        "title": "My Dataset",
        "notes": long_notes,
        "metadata_modified": "2026-01-02T03:04:05Z",
        "resources": [
            {"format": "CSV"},
            {"format": "CSV"},  # deduped
            {"format": ""},  # falsy -> dropped
            {"format": None},  # falsy -> dropped
            {"format": "GeoJSON"},
        ],
    }
    lines = qd.format_dataset(ds)
    assert lines[0] == "  My Dataset"
    assert lines[1] == "    ID:     my-dataset"
    assert lines[2] == "    Updated: 2026-01-02"
    assert lines[3] == "    Formats: CSV, GeoJSON"  # order kept, dupes removed
    # notes: first 150 chars, newlines -> spaces
    assert lines[4] == "    " + ("first line " + "x" * 139)  # 150 chars total
    assert lines[5] == ""

    # title missing -> falls back to name
    assert qd.format_dataset({"name": "slug-only"})[0] == "  slug-only"
    # notes falsy -> "(no description)"
    assert qd.format_dataset({"name": "n", "notes": ""})[4] == "    (no description)"
    # no resources -> "none"
    assert qd.format_dataset({"name": "n"})[3] == "    Formats: none"
    # metadata_modified missing -> '?'; empty string -> ""
    assert qd.format_dataset({"name": "n"})[2] == "    Updated: ?"
    assert qd.format_dataset({"name": "n", "metadata_modified": ""})[2] == "    Updated: "
    print("ok: format_dataset")


def test_get_datasets():
    captured = {}

    def ok_handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["params"] = dict(request.url.params)
        return httpx.Response(
            200,
            json={"success": True, "result": {"count": 3, "results": []}},
        )

    def run(handler):
        with httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        ) as client:
            return qd.get_datasets(client, "ons", limit=5, sort="views_total:desc")

    result = run(ok_handler)
    assert result["count"] == 3
    # URL + params mirror the query-string shape (sort ':' -> ' ')
    assert captured["url"].startswith(qd.BASE_URL + "/package_search")
    assert captured["params"] == {
        "q": "",
        "fq": "organization:ons",
        "rows": "5",
        "sort": "views_total desc",
    }
    # HTTP error -> RuntimeError with status
    with pytest.raises(RuntimeError, match="HTTP 500"):
        run(lambda r: httpx.Response(500, text="boom"))
    # success:false -> RuntimeError
    with pytest.raises(RuntimeError, match="success: false"):
        run(lambda r: httpx.Response(200, json={"success": False}))
    print("ok: get_datasets params + error paths")


def test_cli():
    runner = CliRunner()

    # --list with a local organisations.json
    with tempfile.TemporaryDirectory() as d:
        orgs = [
            {"name": "ons", "display_name": "ONS", "package_count": 2000},
            {"name": "empty", "display_name": "Empty Org"},
        ]
        Path(d, "organisations.json").write_text(json.dumps(orgs), encoding="utf-8")
        with chdir(d):
            res = runner.invoke(qd.app, ["--list"])
        assert res.exit_code == 0, res.output
        assert "ons  \t(2000 datasets)  \tONS" in res.stdout
        assert "empty  \t(? datasets)  \tEmpty Org" in res.stdout

    # --list without the file -> error + exit 1
    with tempfile.TemporaryDirectory() as d:
        with chdir(d):
            res = runner.invoke(qd.app, ["--list"])
        assert res.exit_code == 1, res.output
        assert "No organisations.json found." in res.stderr

    # no org argument -> usage + exit 1
    res = runner.invoke(qd.app, [])
    assert res.exit_code == 1, res.output
    assert "Usage:" in res.stderr

    # bogus sort field -> rejected before any lookup/fetch
    res = runner.invoke(qd.app, ["ons", "--sort", "bogus:desc"])
    assert res.exit_code == 1, res.output
    assert 'Invalid sort field: "bogus"' in res.stderr
    assert "Valid fields: score, metadata_modified" in res.stderr

    # ambiguous fuzzy match -> lists matches + exit 1
    with tempfile.TemporaryDirectory() as d:
        orgs = [
            {"name": "test-a", "display_name": "Alpha"},
            {"name": "test-b", "display_name": "Beta"},
        ]
        Path(d, "organisations.json").write_text(json.dumps(orgs), encoding="utf-8")
        with chdir(d):
            res = runner.invoke(qd.app, ["test"])
        assert res.exit_code == 1, res.output
        assert 'Multiple matches for "test":' in res.stderr
        assert "Alpha  (test-a)" in res.stderr
        assert "Beta  (test-b)" in res.stderr

    # --rows must be >= 1; click usage error, non-zero exit
    res = runner.invoke(qd.app, ["ons", "--rows", "0"])
    assert res.exit_code != 0, res.output
    print("ok: CLI --list / no-input / bogus sort / ambiguous / --rows")
