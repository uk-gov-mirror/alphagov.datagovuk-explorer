"""Unit tests for scripts/review_suggest.py (offline — no live LLM, no DB).

Covers the deterministic parts per the plan:
- constants: THEMES (14) / EXTRAS_WHITELIST (17), plus sha256 pins on the
  three verbatim prompt strings (the model contract — the hashes stop
  accidental edits)
- truncate / strip_html / digest_resource / build_digest (whitelist
  filtering, truncation, resource digest + _note, org fallback chain, tags
  object-vs-string, key order)
- build_prompt: system/user roles, themeList interpolation into both rubric
  and schema, digest JSON with indent=1
- extract_json: fence stripping, first-{-to-last-} slicing, error paths
- load_processed_ids / append_record: ok vs attempted sets, corrupt-line
  skip, compact no-space JSONL bytes
- send_request (mock transport): auth header + thinking only when an API
  key is present, request key order, HTTP-error truncation, empty content
- process_one: ok/failed record shapes + key order, invalid theme, non-array
  tags, 429 backoff (sleep patched), immediate retry on non-429
- run_workers: concurrency cap honoured, each row processed exactly once
- CLI error paths: --limit 0, missing env, remote --concurrency 0, local
  health-check down

The live end-to-end (real quality DB + mock LLM server, resume,
--include-reviewed, --org/--dataset/--limit selection) was verified
separately against a real run (record schema and prompt byte-identical).

Run with: uv run pytest tests/test_review_suggest.py
"""

import hashlib
import io
import json
import os
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path

# The module-level guard fires on import if DATABASE_URL is unset — tests
# never connect, so give it a dummy URL.
os.environ.setdefault("DATABASE_URL", "postgresql://localhost:5432/test-db")

import httpx
import pytest
from typer.testing import CliRunner

import scripts.review_suggest as rs


# ---------------------------------------------------------------------------
# Test data helpers
# ---------------------------------------------------------------------------
def fake_row(
    pkg,
    id_="11111111-1111-1111-1111-111111111111",
    org="test-org",
    title="Test Dataset",
):
    """A row shaped like the SELECT result. dataset_json.json is jsonb now,
    so the pipeline's raw psycopg3 connection (JsonbLoader) returns it as an
    already-parsed dict, not a JSON string."""
    return {
        "id": id_,
        "title": title,
        "org_slug": org,
        "org_display_name": "Test Org",
        "json": pkg,
    }


def review_reply(**overrides) -> httpx.Response:
    """A valid model reply (schema-order keys)."""
    review = {
        "overall": 4,
        "scores": {
            "findability": {"score": 4, "explanation": "Clear title and description."},
            "metadata": {"score": 3, "explanation": "Licence present."},
            "resources": {"score": 2, "explanation": "Few formats."},
        },
        "theme": "environment",
        "theme_confidence": "medium",
        "tags": ["geology", "boreholes"],
        "suggested_title": "",
        "suggested_description": "",
    }
    review.update(overrides)
    return httpx.Response(
        200,
        json={"choices": [{"message": {"content": json.dumps(review)}}]},
    )


def chat_handler(responses):
    """Build a MockTransport handler that walks a list of responses/callables."""
    calls = []
    state = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        i = state["n"]
        state["n"] += 1
        fn = responses[i] if i < len(responses) else responses[-1]
        return fn(request) if callable(fn) else fn

    handler.calls = calls
    return handler


def make_client(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


class PatchedSleep:
    """Patch rs.sleep to record delays and return immediately (no real waiting)."""

    def __init__(self):
        self.delays = []

    def __enter__(self):
        self._orig = rs.sleep

        def fake_sleep(ms):
            self.delays.append(ms)

        rs.sleep = fake_sleep
        return self

    def __exit__(self, *exc):
        rs.sleep = self._orig


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
def test_constants():
    assert len(rs.THEMES) == 14
    assert len(rs.EXTRAS_WHITELIST) == 17
    # a couple of whitelist mappings (hyphenated key + plain key)
    assert rs.EXTRAS_WHITELIST["frequency-of-update"] == "update_frequency"
    assert rs.EXTRAS_WHITELIST["update_frequency"] == "update_frequency"
    assert rs.EXTRAS_WHITELIST["harvest_source_title"] == "harvest_source"
    assert rs.EXTRAS_WHITELIST["licence"] == "licence_statement"
    assert rs.RETRIES == 2
    assert rs.REMOTE_CONCURRENCY == 50
    assert rs.MAX_TOKENS == 2048
    assert rs.TEMPERATURE == 0.2
    print("ok: THEMES (14) / EXTRAS_WHITELIST (17) / retry+concurrency constants")


def test_prompt_contract_hashes():
    # The hashes pin the prompt strings so an accidental edit can't
    # silently change the model contract.
    assert hashlib.sha256(rs.SYSTEM_CONTENT.encode()).hexdigest() == (
        "0f165ea394be82a52af1b587037c66dd4bded3e753192a30822d51720742db87"
    )
    assert hashlib.sha256(rs.RUBRIC.encode()).hexdigest() == (
        "7124727cd8b4072177d93ceaed614256f55c1dd0c6154b6180b9ac26454e5b68"
    )
    assert hashlib.sha256(rs.SCHEMA.encode()).hexdigest() == (
        "488a4c35653b644c229b2a47f197cba8278d686d348134c54c0a7302f5628dc2"
    )
    # structure sanity: the placeholder appears in both rubric and schema
    assert rs.RUBRIC.count("${themeList}") == 1
    assert rs.SCHEMA.count("${themeList}") == 1
    print("ok: prompt strings pinned by sha256 (model contract)")


# ---------------------------------------------------------------------------
# Pure string helpers
# ---------------------------------------------------------------------------
def test_truncate():
    assert rs.truncate(None, 5) is None
    assert rs.truncate("short", 10) == "short"
    assert rs.truncate("longer than ten", 5) == "longe…"
    assert rs.truncate("abc", 3) == "abc"  # exactly n -> untouched
    # True coerces to 'true' / False to 'false'
    assert rs.truncate(s=True, n=10) == "true"
    assert rs.truncate(s=False, n=10) == "false"
    assert rs.truncate(123, 10) == "123"
    print("ok: truncate (null / short / long+… / exact / bool / number)")


def test_strip_html():
    assert rs.strip_html("<p>Hello</p>") == "Hello"
    assert rs.strip_html("A &amp; B") == "A & B"
    assert rs.strip_html("A&nbsp;&nbsp;B") == "A B"
    assert rs.strip_html("line1\n\n  line2\tline3") == "line1 line2 line3"
    assert rs.strip_html("<b>x</b> &amp; <i>y</i>") == "x & y"
    assert rs.strip_html(None) == ""
    assert rs.strip_html("") == ""
    print("ok: strip_html (tags / entities / whitespace collapse / None)")


def test_digest_resource():
    r = {
        "format": "CSV",
        "name": "Data",
        "description": "d",
        "url": "http://x",
        "size": 5,
        "created": "2020-01-02T03:04:05Z",
    }
    out = rs.digest_resource(r)
    assert list(out) == ["format", "name", "description", "url", "size", "created"]
    assert out["format"] == "CSV"
    assert out["created"] == "2020-01-02"
    # missing format -> null, empty string too
    assert rs.digest_resource({"url": "u"}) == {"format": None, "url": "u"}
    assert rs.digest_resource({"format": ""}) == {"format": None}
    # name/description truncation, size/created absent -> keys omitted
    out = rs.digest_resource({"format": "x", "name": "n" * 300})
    assert out["name"] == "n" * 200 + "…"
    assert "size" not in out
    assert "created" not in out
    # falsy name/description/url/size/created are skipped
    assert rs.digest_resource({"format": "x", "name": 0, "size": 0}) == {"format": "x"}
    print("ok: digest_resource (key order / null format / truncation / falsy skip)")


# ---------------------------------------------------------------------------
# build_digest
# ---------------------------------------------------------------------------
def test_build_digest_extras_and_resources():
    pkg = {
        "title": "T",
        "extras": [
            {"key": "frequency-of-update", "value": "monthly"},
            {"key": "unknown-key", "value": "ignored"},
            {"key": "publisher", "value": "x" * 2500},  # truncated to 2000
        ],
        "resources": [{"format": "CSV", "name": f"r{i}"} for i in range(10)],
        "num_resources": 10,
    }
    d = rs.build_digest(pkg)
    assert d["extras"] == {
        "update_frequency": "monthly",
        "publisher": "x" * 2000 + "…",
    }
    # resources sliced to 8 + the _note (num_resources wins over the list len)
    assert len(d["resources"]) == 9
    assert d["resources"][-1] == {"_note": "…and 2 more resources"}
    assert d["resources"][0] == {"format": "CSV", "name": "r0"}

    # num_resources missing -> total from the list length (10 -> _note says 2)
    pkg2 = {**pkg, "resources": [{"format": "CSV"} for _ in range(10)]}
    del pkg2["num_resources"]
    d2 = rs.build_digest(pkg2)
    assert d2["resources"][-1] == {"_note": "…and 2 more resources"}

    # 8 or fewer resources -> no _note
    d3 = rs.build_digest(
        {"title": "T", "resources": [{"format": "CSV"} for _ in range(8)]},
    )
    assert len(d3["resources"]) == 8
    assert all("_note" not in r for r in d3["resources"])
    print("ok: build_digest extras whitelist/truncation + resource slice/_note")


def test_build_digest_fields():
    pkg = {
        "title": "  My <b>Dataset</b>  ",
        "_organisation": {"name": "x", "display_name": "Org A"},
        "organization": {"title": "Org B"},
        "theme-primary": "environment",
        "license_title": "OGL",
        "isopen": False,  # must stay false, not null
        "metadata_created": "2019-06-01T10:00:00Z",
        "metadata_modified": "2021-12-31T23:59:59Z",
        "notes": "Some <p>notes</p>   with&nbsp;spaces",
        "tags": ["a", {"name": "b"}, "c"],
        "resources": [],
    }
    d = rs.build_digest(pkg)
    assert d["title"] == "  My <b>Dataset</b>  "  # title passes through untouched
    assert d["organisation"] == "Org A"  # _organisation.display_name wins
    assert d["theme"] == "environment"
    assert d["licence"] == "OGL"
    assert d["open_licence"] is False
    assert d["created"] == "2019-06-01"
    assert d["last_modified"] == "2021-12-31"
    assert d["description"] == "Some notes with spaces"
    assert d["tags"] == ["a", "b", "c"]

    # fallback chain: _organisation without display_name -> organization.title -> None
    d = rs.build_digest(
        {**_small(), "organization": {"title": "Org B"}, "_organisation": {"name": "x"}},
    )
    assert d["organisation"] == "Org B"
    d = rs.build_digest({**_small(), "organization": {"title": "Org B"}})
    assert d["organisation"] == "Org B"
    d = rs.build_digest(_small())
    assert d["organisation"] is None

    # tags slice(0, 10) and string-vs-object mapping
    pkg2 = {**_small(), "tags": [f"t{i}" for i in range(12)]}
    assert len(rs.build_digest(pkg2)["tags"]) == 10

    # digest key order is fixed (matches build_digest's literal order)
    assert list(d.keys()) == [
        "title",
        "organisation",
        "theme",
        "licence",
        "open_licence",
        "created",
        "last_modified",
        "description",
        "tags",
        "resources",
        "extras",
    ]
    print("ok: build_digest fields (org fallback / licence / tags / key order)")


def _small():
    return {"title": "T", "resources": []}


# ---------------------------------------------------------------------------
# build_prompt
# ---------------------------------------------------------------------------
def test_build_prompt():
    digest = {"title": "X", "organisation": None}
    messages = rs.build_prompt(digest)
    assert len(messages) == 2
    assert messages[0] == {"role": "system", "content": rs.SYSTEM_CONTENT}
    assert messages[1]["role"] == "user"

    theme_list = ", ".join(f'"{t}"' for t in rs.THEMES)
    rubric = rs.RUBRIC.replace("${themeList}", theme_list)
    schema = rs.SCHEMA.replace("${themeList}", theme_list)
    digest_json = json.dumps(digest, indent=1, ensure_ascii=False)
    assert digest_json == '{\n "title": "X",\n "organisation": null\n}'
    expected = (
        "Evaluate and classify the following dataset metadata.\n"
        f"{rubric}\n{schema}\n\n"
        "Dataset metadata (JSON):\n"
        f"{digest_json}\n\n"
        "Now return the review JSON."
    )
    assert messages[1]["content"] == expected
    # both occurrences of the theme list resolved (rubric + schema)
    assert f"[{theme_list}]" in messages[1]["content"]
    assert messages[1]["content"].count(f"[{theme_list}]") == 2
    print("ok: build_prompt (roles / themeList interpolation / indent-1 digest JSON)")


# ---------------------------------------------------------------------------
# extract_json
# ---------------------------------------------------------------------------
def test_extract_json():
    assert rs.extract_json('{"a": 1}') == {"a": 1}
    assert rs.extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert rs.extract_json('```JSON\n{"a": 1}```') == {"a": 1}
    assert rs.extract_json('```\n{"a": 1}\n```') == {"a": 1}
    # text around the object is sliced away (first { to last })
    assert rs.extract_json(
        'Sure! Here you go: ```json\n{"a": 1}``` hope that helps',
    ) == {"a": 1}
    # slice handles trailing text after the last }
    assert rs.extract_json('prefix {"a": 1, "b": {"c": 2}} suffix') == {
        "a": 1,
        "b": {"c": 2},
    }
    # no JSON object -> ValueError ('{}]' is NOT an error: the first-{-to-last-}
    # slice cuts the trailing ] off, leaving '{}')
    for bad in ("no json here", "```json\n```", ""):
        with pytest.raises(ValueError, match="no JSON object found in reply"):
            rs.extract_json(bad)
    # invalid JSON inside the slice -> json.JSONDecodeError (a ValueError)
    with pytest.raises(json.JSONDecodeError):
        rs.extract_json('{"a": }')
    print("ok: extract_json (fences / slice / no-JSON / invalid JSON)")


# ---------------------------------------------------------------------------
# JSONL store
# ---------------------------------------------------------------------------
def test_load_processed_ids():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "out.jsonl"
        # missing file -> empty sets
        ok, attempted = rs.load_processed_ids(p)
        assert ok == set()
        assert attempted == set()

        p.write_text(
            '{"dataset_id":"a","ok":true}\n'
            '{"dataset_id":"b","ok":false,"error":"boom"}\n'
            "corrupt line not json\n"
            '{"dataset_id":"c","ok":true}\n'
            "\n"
            '{"no_dataset_id":true}\n',
            encoding="utf-8",
        )
        ok, attempted = rs.load_processed_ids(p)
        assert ok == {"a", "c"}  # only ok:true
        assert attempted == {
            "a",
            "b",
            "c",
            None,
        }  # all parsed records (None = missing id)
    print("ok: load_processed_ids (missing / ok vs attempted / corrupt skip)")


def test_append_record():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "out.jsonl"
        rs.append_record(p, {"dataset_id": "a", "ok": True, "theme": "society"})
        rs.append_record(
            p,
            {"dataset_id": "b", "ok": False, "error": "x", "note": "£—…"},
        )
        lines = p.read_text(encoding="utf-8").split("\n")
        assert lines[0] == '{"dataset_id":"a","ok":true,"theme":"society"}'
        assert lines[1] == '{"dataset_id":"b","ok":false,"error":"x","note":"£—…"}'
        assert lines[2] == ""  # trailing newline
        # no spaces after separators, raw unicode
        assert ":" in lines[0]
        assert ', "' not in lines[0]
        assert "£" in lines[1]
        assert "—" in lines[1]
    print("ok: append_record (compact separators / raw unicode / trailing newline)")


# ---------------------------------------------------------------------------
# send_request
# ---------------------------------------------------------------------------
def test_send_request_remote():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return review_reply()

    with make_client(handler) as client:
        content = rs.send_request(client, "http://llm", "secret", "m1", {"title": "T"})
    assert content == json.dumps(
        {
            "overall": 4,
            "scores": {
                "findability": {
                    "score": 4,
                    "explanation": "Clear title and description.",
                },
                "metadata": {"score": 3, "explanation": "Licence present."},
                "resources": {"score": 2, "explanation": "Few formats."},
            },
            "theme": "environment",
            "theme_confidence": "medium",
            "tags": ["geology", "boreholes"],
            "suggested_title": "",
            "suggested_description": "",
        },
    )

    assert captured["auth"] == "Bearer secret"
    body = captured["body"]
    # key order: model, messages, thinking, max_tokens, temperature
    assert list(body) == ["model", "messages", "thinking", "max_tokens", "temperature"]
    assert body["thinking"] == {"type": "disabled"}
    assert body["max_tokens"] == 2048
    assert body["temperature"] == 0.2
    assert len(body["messages"]) == 2
    print("ok: send_request remote (auth / thinking / key order / params)")


def test_send_request_local():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["auth"] = request.headers.get("Authorization")
        captured["body"] = json.loads(request.content)
        return review_reply()

    with make_client(handler) as client:
        rs.send_request(client, "http://llm", "", "m1", {"title": "T"})
    assert captured["auth"] is None
    assert "thinking" not in captured["body"]
    assert list(captured["body"]) == ["model", "messages", "max_tokens", "temperature"]
    print("ok: send_request local (no auth / no thinking)")


def test_send_request_errors():
    # HTTP error -> ReviewError with status; body truncated to 200 + …
    handler = chat_handler([httpx.Response(429, text="rate limited " + "x" * 300)])
    with make_client(handler) as client:
        with pytest.raises(rs.ReviewError, match="HTTP 429: ") as exc:
            rs.send_request(client, "http://llm", "", "m", {"title": "T"})
        assert exc.value.status == 429
        assert str(exc.value).startswith("HTTP 429: ")
        assert str(exc.value).endswith("…")  # truncated body

    # empty content -> ReviewError message
    handler = chat_handler(
        [httpx.Response(200, json={"choices": [{"message": {"content": ""}}]})],
    )
    with make_client(handler) as client:
        with pytest.raises(rs.ReviewError) as exc:
            rs.send_request(client, "http://llm", "", "m", {"title": "T"})
        assert str(exc.value) == "empty reply content (max_tokens may be too low)"

    # content is trimmed before returning
    handler = chat_handler(
        [
            httpx.Response(
                200,
                json={"choices": [{"message": {"content": '  {"a":1}  '}}]},
            ),
        ],
    )
    with make_client(handler) as client:
        content = rs.send_request(client, "http://llm", "", "m", {"title": "T"})
    assert content == '{"a":1}'
    print("ok: send_request errors (HTTP truncation / empty content / trim)")


# ---------------------------------------------------------------------------
# process_one
# ---------------------------------------------------------------------------
def test_process_one_ok_record():
    row = fake_row({"title": "T", "resources": [{"format": "CSV"}]})
    summary = {"ok": 0, "failed": 0, "overall": []}
    handler = chat_handler([review_reply()])

    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "out.jsonl"

        with make_client(handler) as client:
            rs.process_one(
                rs.ReviewConfig(client, "http://llm", "", "m1", out),
                row,
                0,
                1,
                show_progress=False,
                summary=summary,
            )
        rec = json.loads(out.read_text(encoding="utf-8"))
        # key order: base keys, ok, then the parsed model output
        assert list(rec) == [
            "dataset_id",
            "title",
            "org_slug",
            "org_display_name",
            "model",
            "reviewed_at",
            "classified_at",
            "ok",
            "overall",
            "scores",
            "theme",
            "theme_confidence",
            "tags",
            "suggested_title",
            "suggested_description",
        ]
        assert rec["dataset_id"] == row["id"]
        assert rec["org_slug"] == "test-org"
        assert rec["org_display_name"] == "Test Org"
        assert rec["model"] == "m1"
        assert rec["ok"] is True
        assert rec["overall"] == 4
        assert rec["reviewed_at"].endswith("Z")
        assert rec["classified_at"].endswith("Z")
        assert summary == {"ok": 1, "failed": 0, "overall": [4]}
        assert len(handler.calls) == 1  # one request, success on first try
    print("ok: process_one ok record (schema + key order + summary)")


def test_process_one_failed_and_validation():
    cases = [
        (
            review_reply(theme="not-a-theme"),
            'invalid theme "not-a-theme" — not in vocabulary',
        ),
        (review_reply(tags="nope"), "tags must be an array"),
        (httpx.Response(500, text="boom"), "HTTP 500: boom"),
    ]
    for reply, err in cases:
        row = fake_row({"title": "T"})
        summary = {"ok": 0, "failed": 0, "overall": []}
        handler = chat_handler([reply, reply, reply])  # retries up to 3 times

        with tempfile.TemporaryDirectory() as d:
            out = Path(d) / "out.jsonl"

            with make_client(handler) as client:
                rs.process_one(
                    rs.ReviewConfig(client, "http://llm", "", "m", out),
                    row,
                    0,
                    1,
                    show_progress=False,
                    summary=summary,
                )
            rec = json.loads(out.read_text(encoding="utf-8"))
        assert rec["ok"] is False
        assert rec["error"] == err
        assert summary == {"ok": 0, "failed": 1, "overall": []}
        # non-429 errors retry immediately (the loop runs to RETRIES+1)
        assert len(handler.calls) == 3
    print("ok: process_one failed (bad theme / bad tags / HTTP 500, retried 3x)")


def test_process_one_429_backoff():
    row = fake_row({"title": "T"})
    summary = {"ok": 0, "failed": 0, "overall": []}
    handler = chat_handler(
        [
            httpx.Response(429, text="slow down"),
            httpx.Response(429, text="slow down"),
            review_reply(),  # third attempt succeeds
        ],
    )

    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "out.jsonl"
        with PatchedSleep() as ps, make_client(handler) as client:
            rs.process_one(
                rs.ReviewConfig(client, "http://llm", "", "m", out),
                row,
                0,
                1,
                show_progress=False,
                summary=summary,
            )
        rec = json.loads(out.read_text(encoding="utf-8"))
    assert rec["ok"] is True
    assert rec["overall"] == 4
    assert len(handler.calls) == 3
    # sleep(2000 * (attempt + 1)) on 429, attempts 0 and 1 only
    assert ps.delays == [2000, 4000]

    # 429 on every attempt -> failed record, error is the last message
    row2 = fake_row({"title": "T"})
    summary2 = {"ok": 0, "failed": 0, "overall": []}
    handler2 = chat_handler([httpx.Response(429, text="nope")] * 3)
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "out.jsonl"
        with PatchedSleep(), make_client(handler2) as client:
            rs.process_one(
                rs.ReviewConfig(client, "http://llm", "", "m", out),
                row2,
                0,
                1,
                show_progress=False,
                summary=summary2,
            )
        rec = json.loads(out.read_text(encoding="utf-8"))
    assert rec["ok"] is False
    assert rec["error"].startswith("HTTP 429:")
    print("ok: process_one 429 backoff (2s/4s sleeps, then success / exhausted)")


def test_process_one_progress():
    row = fake_row({"title": "Nice Title"}, title="Nice Title")
    summary = {"ok": 0, "failed": 0, "overall": []}
    handler = chat_handler([review_reply()])
    buf = io.StringIO()
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "out.jsonl"

        with make_client(handler) as client, redirect_stdout(buf):
            rs.process_one(
                rs.ReviewConfig(client, "http://llm", "", "m", out),
                row,
                0,
                1,
                show_progress=True,
                summary=summary,
            )
    line = buf.getvalue().strip()
    assert line.startswith(
        "[1/1] overall 4/5 | theme environment (medium) | test-org/Nice Title | ",
    )
    # lowest score is resources (2) -> its explanation is the suffix
    assert "Few formats." in line
    assert "Clear title and description." not in line
    print("ok: process_one progress line (lowest-score explanation suffix)")


# ---------------------------------------------------------------------------
# run_workers concurrency
# ---------------------------------------------------------------------------
def test_run_workers_concurrency():
    rows = [fake_row({"title": f"T{i}"}, id_=f"{i:08d}-0000-0000-0000-000000000000") for i in range(6)]
    state = {"active": 0, "max": 0, "seen": set()}

    def handler(request: httpx.Request) -> httpx.Response:
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        time.sleep(0.05)
        state["active"] -= 1
        return review_reply()

    summary = {"ok": 0, "failed": 0, "overall": []}
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "out.jsonl"

        with make_client(handler) as client:
            rs.run_workers(
                rs.ReviewConfig(client, "http://llm", "", "m", out),
                rows,
                3,
                show_progress=False,
                summary=summary,
            )
        lines = [json.loads(line) for line in out.read_text(encoding="utf-8").split("\n") if line.strip()]

    assert summary == {"ok": 6, "failed": 0, "overall": [4] * 6}
    assert len(lines) == 6
    # every row processed exactly once
    assert {r["dataset_id"] for r in lines} == {r["id"] for r in rows}
    assert state["max"] == 3  # concurrency cap honoured
    print("ok: run_workers (6 rows, cap 3 -> max in-flight 3, each row once)")


def test_run_workers_caps_to_row_count():
    rows = [fake_row({"title": "T"}, id_=f"{i:08d}-0000-0000-0000-000000000000") for i in range(2)]
    state = {"active": 0, "max": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["active"] += 1
        state["max"] = max(state["max"], state["active"])
        time.sleep(0.01)
        state["active"] -= 1
        return review_reply()

    summary = {"ok": 0, "failed": 0, "overall": []}
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "out.jsonl"

        with make_client(handler) as client:
            rs.run_workers(
                rs.ReviewConfig(client, "http://llm", "", "m", out),
                rows,
                50,
                show_progress=False,
                summary=summary,
            )
    assert state["max"] == 2  # min(concurrency, len(rows))
    print("ok: run_workers caps workers to row count")


# ---------------------------------------------------------------------------
# CLI error paths
# ---------------------------------------------------------------------------
def test_cli():
    runner = CliRunner()
    clear_env = {
        "LLM": "",
        "LLM_BASE_URL": "",
        "LLM_MODEL": "",
        "LOCAL_BASE_URL": "",
        "LOCAL_MODEL": "",
    }

    # --limit 0 -> exit 1
    res = runner.invoke(rs.app, ["--limit", "0"], env=clear_env)
    assert res.exit_code == 1, res.output
    assert "--limit must be >= 1" in res.stderr

    # missing model/base-url env -> exit 1 with the right var names
    res = runner.invoke(rs.app, ["--limit", "1"], env=clear_env)
    assert res.exit_code == 1, res.output
    assert "LOCAL_MODEL and LOCAL_BASE_URL" in res.stderr
    # remote (LLM key present) -> names the remote vars
    res = runner.invoke(rs.app, ["--limit", "1"], env={**clear_env, "LLM": "k"})
    assert res.exit_code == 1, res.output
    assert "LLM_MODEL and LLM_BASE_URL" in res.stderr

    # remote --concurrency 0 -> exit 1
    res = runner.invoke(
        rs.app,
        ["--concurrency", "0", "--limit", "1"],
        env={**clear_env, "LLM": "k", "LLM_BASE_URL": "http://x", "LLM_MODEL": "m"},
    )
    assert res.exit_code == 1, res.output
    assert "--concurrency must be >= 1" in res.stderr

    # local health check against a dead port -> exit 1, both messages
    res = runner.invoke(
        rs.app,
        ["--limit", "1"],
        env={
            **clear_env,
            "LOCAL_BASE_URL": "http://127.0.0.1:59999",
            "LOCAL_MODEL": "m",
        },
    )
    assert res.exit_code == 1, res.output
    assert "Cannot reach the model server at http://127.0.0.1:59999" in res.stderr
    assert "Start the server configured by LOCAL_BASE_URL" in res.stderr
    assert "Error: 1" not in res.stderr  # typer.Exit must not be swallowed
    print("ok: CLI error paths (limit/env/concurrency/health-check)")
