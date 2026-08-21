"""Unit tests for scripts/download_datasets.py (offline — no live API).

Covers the deterministic parts per the plan:
- slugify + filename construction (slugify(title)-id[:8].json)
- iso_now format (ISO 8601 UTC: 3-digit ms + Z)
- load_no_datasets / save_no_datasets round-trip and failure modes
- has_saved_datasets (.json detection, missing dir)
- select_batch: single org (found / not-found + hint), force (contiguous
  slice, clears empty markers, persists), next-batch walk (skips
  noDatasets + saved orgs), offset
- fetch_datasets: pagination (rows=1000 pages, short-page break), URL
  params, HTTP error, success:false (mock transport)
- process_batch: record shape + key order, skip existing, force overwrite,
  zero-dataset org -> no-datasets.json marker, per-org error doesn't stop
  the batch
- CLI error paths: --continuous + --org, missing organisations.json,
  --org not found (+ hint), bogus --per-org

The live-API happy path is verified separately by running the download
against the real API and checking the filenames.

Run with: uv run pytest tests/test_download_datasets.py
"""

import io
import json
import os
import re
import tempfile
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from pathlib import Path

import httpx
import pytest
from typer.testing import CliRunner

import scripts.download_datasets as dd

ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")


@contextmanager
def chdir(path):
    old = Path.cwd()
    os.chdir(path)
    try:
        yield
    finally:
        os.chdir(old)


def make_dataset(i: int) -> dict:
    """A fake CKAN dataset with a title and a 32-hex UUID id."""
    return {
        "title": f"Dataset Number {i}",
        "id": f"{i:08d}-0000-0000-0000-000000000000",
    }


def test_iso_now():
    assert ISO_RE.match(dd.iso_now()), dd.iso_now()
    print("ok: iso_now is ISO 8601 format (ms + Z)")


def test_slugify():
    # lowercase
    assert dd.slugify("My Dataset") == "my-dataset"
    # non-alphanumeric runs -> single dash
    assert dd.slugify("Spend  over £25,000!") == "spend-over-25-000"
    # punctuation-only title -> empty after trimming dashes
    assert dd.slugify("!!! ...") == ""
    # trim leading/trailing dashes
    assert dd.slugify("--leading and trailing--") == "leading-and-trailing"
    # 80-char cap
    assert len(dd.slugify("x" * 200)) == 80
    # 'A'*100 + ' B' -> 100 a's, then the 80-char truncation cuts before the
    # '-b' suffix (verified: 80 a's)
    assert dd.slugify("A" * 100 + " B") == "a" * 80
    # ASCII-only: fullwidth digits are NOT [a-z0-9]
    assert dd.slugify("２０２０ data") == "data"
    print("ok: slugify lowercase / dashes / trim / 80-cap / ASCII-only")


def test_filename():
    ds = {"title": "My Dataset!", "id": "598c37fa-9d20-465a-988c-a6e31974493a"}
    assert dd.slugify(ds["title"]) + "-" + ds["id"][:8] + ".json" == ("my-dataset-598c37fa.json")
    # the id8 suffix disambiguates two titles sharing an 80-char slug prefix
    title = "Long title " + "x" * 70
    a = {"title": title, "id": "11111111-aaaa-aaaa-aaaa-aaaaaaaaaaaa"}
    b = {"title": title, "id": "22222222-bbbb-bbbb-bbbb-bbbbbbbbbbbb"}
    fa = f"{dd.slugify(a['title'])}-{a['id'][:8]}.json"
    fb = f"{dd.slugify(b['title'])}-{b['id'][:8]}.json"
    assert fa != fb
    assert len(fa) == len(fb)
    print("ok: filename = slugify(title)-id[:8].json, id disambiguates")


def test_no_datasets_round_trip():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "no-datasets.json"
        # missing file -> {}
        assert dd.load_no_datasets(str(p)) == {}
        # invalid JSON -> {}
        p.write_text("{oops", encoding="utf-8")
        assert dd.load_no_datasets(str(p)) == {}
        # non-object JSON -> {} (treated as empty)
        p.write_text("[1,2]", encoding="utf-8")
        assert dd.load_no_datasets(str(p)) == {}
        # round-trip with indent-2 formatting
        m = {"org-a": "2026-08-01T11:32:58.493Z", "org-b": "2026-08-01T11:32:59.607Z"}
        dd.save_no_datasets(m, str(p))
        assert dd.load_no_datasets(str(p)) == m
        text = p.read_text(encoding="utf-8")
        assert '"org-a": "2026-08-01T11:32:58.493Z"' in text  # indent 2
    print("ok: no-datasets load/save (missing / invalid / round-trip / indent)")


def test_has_saved_datasets():
    with tempfile.TemporaryDirectory() as d, chdir(d):
        Path("downloads/empty-org").mkdir(parents=True)
        Path("downloads/full-org").mkdir(parents=True)
        Path("downloads/full-org", "a.json").write_text("{}", encoding="utf-8")
        Path("downloads/full-org", "b.json").write_text("{}", encoding="utf-8")
        Path("downloads/full-org", ".DS_Store").write_text("x", encoding="utf-8")
        # missing org dir -> False
        assert dd.has_saved_datasets("missing-org") is False
        # dir with no .json files -> False
        assert dd.has_saved_datasets("empty-org") is False
        # dir with .json files -> True (non-.json files ignored)
        assert dd.has_saved_datasets("full-org") is True
    print("ok: has_saved_datasets (missing / empty / .json present)")


def test_select_batch_single_org():
    orgs = [
        {"name": "ons", "display_name": "ONS"},
        {"name": "defra", "display_name": "DEFRA"},
    ]
    no = {"ons": "2026-08-01T11:32:58.493Z"}  # marked empty earlier

    # single org found -> [org], and its empty marker is cleared
    batch = dd.select_batch(orgs, no, 50, 0, org_slug="defra")
    assert [o["name"] for o in batch] == ["defra"]
    assert "ons" in no  # untouched
    batch = dd.select_batch(orgs, no, 50, 0, org_slug="ons")
    assert [o["name"] for o in batch] == ["ons"]
    assert "ons" not in no  # cleared, even if previously marked empty

    # not found -> OrgNotFoundError with a helpful hint
    with pytest.raises(dd.OrgNotFoundError, match='Organisation not found: "zzz"') as exc:
        dd.select_batch(orgs, no, 50, 0, org_slug="zzz")
    assert "Check organisations.json" in exc.value.hint
    print("ok: select_batch single org (found / clears marker / not-found + hint)")


def test_select_batch_force():
    orgs = [{"name": f"org-{i}", "display_name": f"Org {i}"} for i in range(10)]
    no = {"org-3": "ts", "org-7": "ts"}

    with tempfile.TemporaryDirectory() as d, chdir(d):
        Path("downloads").mkdir(parents=True)  # the no-datasets file needs the dir to exist
        # contiguous slice from offset; markers in the slice cleared + persisted
        batch = dd.select_batch(orgs, no, 4, 2, force=True)
        assert [o["name"] for o in batch] == ["org-2", "org-3", "org-4", "org-5"]
        assert "org-3" not in no  # marker cleared in the passed dict
        saved = json.loads(
            Path("downloads/no-datasets.json").read_text(encoding="utf-8"),
        )
        assert "org-3" not in saved
        assert "org-7" in saved
        # cursor overrides offset (continuous + force)
        batch = dd.select_batch(orgs, {}, 3, 0, force=True, cursor=6)
        assert [o["name"] for o in batch] == ["org-6", "org-7", "org-8"]
        # slice past the end -> shorter batch (no error)
        batch = dd.select_batch(orgs, {}, 5, 8, force=True)
        assert [o["name"] for o in batch] == ["org-8", "org-9"]
    print(
        "ok: select_batch force (slice / markers cleared+persisted / cursor / past-end)",
    )


def test_select_batch_next():
    orgs = [{"name": f"org-{i}"} for i in range(10)]
    no = {"org-1": "ts"}  # previously empty -> skip
    with tempfile.TemporaryDirectory() as d, chdir(d):
        Path("downloads/org-3").mkdir(parents=True)  # has saved data -> skip
        Path("downloads/org-3", "x.json").write_text("{}", encoding="utf-8")
        Path("downloads/org-8").mkdir(parents=True)  # empty dir -> NOT skipped
        batch = dd.select_batch(orgs, no, 3, 0)
        assert [o["name"] for o in batch] == ["org-0", "org-2", "org-4"]
        # offset is IGNORED in next-batch mode — the walk always starts at the
        # top of the list (same batch for offset 0 and 4). offset only matters
        # in --force mode.
        batch = dd.select_batch(orgs, no, 3, 4)
        assert [o["name"] for o in batch] == ["org-0", "org-2", "org-4"]
        # org-count cap
        batch = dd.select_batch(orgs, no, 2, 0)
        assert [o["name"] for o in batch] == ["org-0", "org-2"]
    print("ok: select_batch next-batch (skip noDatasets / saved / offset / cap)")


def test_fetch_datasets():
    total = 2500
    all_results = [make_dataset(i) for i in range(total)]
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(dict(request.url.params))
        start = int(captured[-1]["start"])
        rows = int(captured[-1]["rows"])
        page = all_results[start : start + rows]
        return httpx.Response(
            200,
            json={"success": True, "result": {"results": page, "count": total}},
        )

    def check():
        limiter = dd.create_rate_limiter(4)
        with httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        ) as client:
            # 'all' (inf): 1000 + 1000 + 500 pages, short page breaks the loop
            results = dd.fetch_datasets(limiter, client, "ons", float("inf"), dd.SORT)
        assert len(results) == total
        # pagination params: rows=1000, starts 0/1000/2000, sort ':' -> ' '
        assert [p["rows"] for p in captured] == ["1000", "1000", "1000"]
        assert [p["start"] for p in captured] == ["0", "1000", "2000"]
        assert all(p["sort"] == "metadata_created desc" for p in captured)
        assert all(p["fq"] == "organization:ons" for p in captured)
        assert all(p["q"] == "" for p in captured)

        captured.clear()
        with httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        ) as client:
            # finite limit 1500: page of 1000 then page of 500, loop ends on offset
            results = dd.fetch_datasets(limiter, client, "ons", 1500.0, dd.SORT)
        assert len(results) == 1500
        assert [p["rows"] for p in captured] == ["1000", "500"]

        # HTTP error -> RuntimeError
        with (
            pytest.raises(RuntimeError, match="HTTP 500"),
            httpx.Client(
                transport=httpx.MockTransport(
                    lambda r: httpx.Response(500, text="boom"),
                ),
                follow_redirects=True,
            ) as client,
        ):
            dd.fetch_datasets(limiter, client, "ons", 10.0, dd.SORT)

        # success:false -> RuntimeError
        def bad_success(request):
            return httpx.Response(200, json={"success": False})

        with (
            pytest.raises(RuntimeError, match="success: false"),
            httpx.Client(
                transport=httpx.MockTransport(bad_success),
                follow_redirects=True,
            ) as client,
        ):
            dd.fetch_datasets(limiter, client, "ons", 10.0, dd.SORT)
        print("ok: fetch_datasets pagination / params / HTTP+success errors")

    check()


def test_process_batch():
    datasets = [make_dataset(1), make_dataset(2)]

    def handler(request: httpx.Request) -> httpx.Response:
        fq = dict(request.url.params)["fq"]
        if "with-data" in fq:
            return httpx.Response(
                200,
                json={"success": True, "result": {"results": datasets}},
            )
        return httpx.Response(200, json={"success": True, "result": {"results": []}})

    batch = [
        {"name": "org-with-data", "display_name": "Org With Data"},
        {"name": "empty-org", "display_name": "Empty Org"},
    ]

    def run_batch(*, force: bool, no: dict | None = None):
        limiter = dd.create_rate_limiter(4)
        with httpx.Client(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        ) as client:
            return dd.process_batch(
                batch,
                1000.0,
                limiter,
                client,
                no if no is not None else {},
                force=force,
            )

    with tempfile.TemporaryDirectory() as d, chdir(d):
        # --- first run: saves 2 files + writes no-datasets marker ---
        no = {}
        result = run_batch(force=False, no=no)
        assert result == {"totalDatasets": 2, "skippedOrgs": 0, "skippedDatasets": 0}
        assert "empty-org" in no  # marker recorded in memory
        assert ISO_RE.match(no["empty-org"])

        # files on disk with the exact filenames
        f1 = Path("downloads/org-with-data/dataset-number-1-00000001.json")
        f2 = Path("downloads/org-with-data/dataset-number-2-00000002.json")
        assert f1.exists()
        assert f2.exists()

        # record shape + key order: _fetched_at, _organisation, then ds keys
        rec = json.loads(f1.read_text(encoding="utf-8"))
        assert list(rec)[:2] == ["_fetched_at", "_organisation"]
        assert ISO_RE.match(rec["_fetched_at"])
        assert rec["_organisation"] == {
            "name": "org-with-data",
            "display_name": "Org With Data",
        }
        assert rec["title"] == "Dataset Number 1"
        assert rec["id"] == "00000001-0000-0000-0000-000000000000"
        assert list(rec)[2:] == ["title", "id"]  # ds keys after, order kept
        # written with indent=2
        raw = f1.read_text(encoding="utf-8")
        assert '{\n  "_fetched_at":' in raw

        # no-datasets.json persisted with indent-2 formatting
        saved = json.loads(
            Path("downloads/no-datasets.json").read_text(encoding="utf-8"),
        )
        assert set(saved) == {"empty-org"}

        # --- second run, no force: org skipped (has files), empty re-marked ---
        no2 = {}
        result = run_batch(force=False, no=no2)
        assert result == {"totalDatasets": 0, "skippedOrgs": 1, "skippedDatasets": 0}

        # --- force run: everything refetched and overwritten ---
        no3 = {}
        result = run_batch(force=True, no=no3)
        assert result == {"totalDatasets": 2, "skippedOrgs": 0, "skippedDatasets": 0}
        assert "empty-org" in no3

        # --- display_name missing: omitted from _organisation (JSON
        # serialisation drops a missing key), not written as null ---
        def run_single():
            limiter = dd.create_rate_limiter(4)
            with httpx.Client(
                transport=httpx.MockTransport(handler),
                follow_redirects=True,
            ) as client:
                dd.process_batch(
                    [{"name": "org-with-data"}],
                    10.0,
                    limiter,
                    client,
                    {},
                    force=True,
                )

        run_single()
        rec = json.loads(f1.read_text(encoding="utf-8"))
        assert rec["_organisation"] == {"name": "org-with-data"}
        assert "display_name" not in rec["_organisation"]

    # --- per-org API error: logged to stderr, batch continues ---
    def error_handler(request: httpx.Request) -> httpx.Response:
        fq = dict(request.url.params)["fq"]
        if "bad" in fq:
            return httpx.Response(500, text="boom")
        return httpx.Response(
            200,
            json={"success": True, "result": {"results": datasets}},
        )

    def run_with_error():
        limiter = dd.create_rate_limiter(4)
        with httpx.Client(
            transport=httpx.MockTransport(error_handler),
            follow_redirects=True,
        ) as client:
            return dd.process_batch(
                [{"name": "bad-org"}, {"name": "good-org"}],
                10.0,
                limiter,
                client,
                {},
                force=False,
            )

    with tempfile.TemporaryDirectory() as d, chdir(d):
        err = io.StringIO()
        out = io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            result = run_with_error()
        # bad org errored (stderr), good org still saved (stdout progress)
        assert "✗ error: HTTP 500" in err.getvalue()
        assert "[1/2]" in out.getvalue()
        assert "[2/2]" in out.getvalue()
        assert result["totalDatasets"] == 2
        assert Path("downloads/good-org/dataset-number-1-00000001.json").exists()
    print("ok: process_batch save/skip/force/record-shape/display_name/error-continue")


def test_cli():
    runner = CliRunner()

    # --continuous + --org -> error, exit 1 (before any file/network access)
    res = runner.invoke(dd.app, ["--continuous", "--org", "x"])
    assert res.exit_code == 1, res.output
    assert "--continuous cannot be combined with --org" in res.stderr

    # missing organisations.json -> exit 1
    with tempfile.TemporaryDirectory() as d, chdir(d):
        res = runner.invoke(dd.app, ["--orgs", "5"])
        assert res.exit_code == 1, res.output
        assert "No organisations.json found." in res.stderr

    # --org not found -> exit 1 + hint
    with tempfile.TemporaryDirectory() as d:
        Path(d, "downloads").mkdir(exist_ok=True)
        Path(d, "downloads", "organisations.json").write_text(
            json.dumps([{"name": "ons", "display_name": "ONS"}]),
            encoding="utf-8",
        )
        with chdir(d):
            res = runner.invoke(dd.app, ["--org", "zzz"])
        assert res.exit_code == 1, res.output
        assert 'Organisation not found: "zzz"' in res.stderr
        assert "Check organisations.json" in res.stderr

    # bogus --per-org -> usage error (non-zero exit is all callers rely on)
    with tempfile.TemporaryDirectory() as d:
        Path(d, "downloads").mkdir(exist_ok=True)
        Path(d, "downloads", "organisations.json").write_text(
            json.dumps([{"name": "ons"}]),
            encoding="utf-8",
        )
        with chdir(d):
            res = runner.invoke(dd.app, ["--per-org", "bogus"])
            assert res.exit_code != 0, res.output
            res = runner.invoke(dd.app, ["--per-org", "0"])
            assert res.exit_code != 0, res.output
            # 'all' parses fine — reach the --org lookup (fail-fast, no network)
            res = runner.invoke(dd.app, ["--per-org", "all", "--org", "nope"])
            assert res.exit_code == 1, res.output
            assert 'Organisation not found: "nope"' in res.stderr

    # --orgs 0 / --offset -1 -> usage error (non-zero exit)
    with tempfile.TemporaryDirectory() as d:
        Path(d, "downloads").mkdir(exist_ok=True)
        Path(d, "downloads", "organisations.json").write_text(
            json.dumps([{"name": "ons"}]),
            encoding="utf-8",
        )
        with chdir(d):
            res = runner.invoke(dd.app, ["--orgs", "0"])
            assert res.exit_code != 0, res.output
            res = runner.invoke(dd.app, ["--offset", "-1"])
            assert res.exit_code != 0, res.output
    print("ok: CLI continuous+org / missing orgs file / org not found / per-org / ints")
