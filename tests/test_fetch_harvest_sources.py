"""Unit tests for scripts/fetch_harvest_sources.py (offline — no live API).

Covers the deterministic parts:
- get_organisation_ids: pages organization_list with all_fields in chunks
  of PAGE_SIZE, extracts ids, pagination params
- get_harvest_sources: one call per org with organization_id filter,
  tags each source with the org it came from, dedupes by source id
- error paths: HTTP error, success:false
- write_json round-trip
- main(): end-to-end happy path writes harvest_sources.json

Run with: uv run python -m pytest tests/test_fetch_harvest_sources.py
"""

import json

import httpx
import pytest

import scripts.fetch_harvest_sources as fh

PAGE_SIZE = fh.PAGE_SIZE


def make_org(i: int) -> dict:
    return {"id": f"org-{i:04d}", "name": f"org-{i}"}


def make_source(i: int, org_id: str) -> dict:
    return {
        "id": f"src-{i:04d}",
        "title": f"Harvest Source {i}",
        "url": f"https://example.com/{i}.xml",
        "type": "gemini-single",
        "active": True,
        "publisher_id": "",  # often empty in the real API
    }


def test_get_organisation_ids():
    total = 60  # 3 pages at PAGE_SIZE=25
    orgs = [make_org(i) for i in range(total)]
    captured = []

    def handler(request: httpx.Request) -> httpx.Response:
        if "all_fields" not in request.url.params:
            # the cheap names call: no all_fields, no pagination
            return httpx.Response(
                200, json={"success": True, "result": [o["name"] for o in orgs]},
            )
        captured.append(dict(request.url.params))
        offset = int(captured[-1]["offset"])
        limit = int(captured[-1]["limit"])
        page = orgs[offset : offset + limit]
        return httpx.Response(200, json={"success": True, "result": page})

    with httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=True,
    ) as client:
        ids = fh.get_organisation_ids(client, lambda: None)

    assert ids == [o["id"] for o in orgs]
    # paginated at PAGE_SIZE with offset stepping
    assert [p["limit"] for p in captured] == [str(PAGE_SIZE)] * 3
    assert [p["offset"] for p in captured] == ["0", "25", "50"]
    assert all(p["all_fields"] == "true" for p in captured)


def test_get_harvest_sources_tags_and_dedupes():
    org_ids = ["org-0001", "org-0002", "org-0003"]
    # org-0002 has two sources; src-0001 appears twice (only via org-0002)
    per_org = {
        "org-0001": [make_source(1, "org-0001")],
        "org-0002": [make_source(1, "org-0002"), make_source(2, "org-0002")],
        "org-0003": [],  # no sources
    }
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        org_id = request.url.params["organization_id"]
        requested.append(org_id)
        return httpx.Response(200, json={"success": True, "result": per_org[org_id]})

    with httpx.Client(
        transport=httpx.MockTransport(handler), follow_redirects=True,
    ) as client:
        sources = fh.get_harvest_sources(client, lambda: None, org_ids)

    # one request per org, in order
    assert requested == org_ids
    # deduped: src-0001 returned once, tagged with the org that owns it
    assert len(sources) == 2
    by_id = {s["id"]: s for s in sources}
    assert set(by_id) == {"src-0001", "src-0002"}
    assert by_id["src-0001"]["organization_id"] == "org-0002"
    assert by_id["src-0002"]["organization_id"] == "org-0002"
    # original fields preserved
    assert by_id["src-0001"]["title"] == "Harvest Source 1"


def test_error_paths():
    def http_error(request):
        return httpx.Response(500, text="boom")

    def bad_success(request):
        return httpx.Response(200, json={"success": False})

    # HTTP error on org page
    with (
        pytest.raises(RuntimeError, match="HTTP 500"),
        httpx.Client(
            transport=httpx.MockTransport(http_error), follow_redirects=True,
        ) as client,
    ):
        fh.get_harvest_sources(client, lambda: None, ["org-0001"])

    # success:false on org page
    with (
        pytest.raises(RuntimeError, match="success: false"),
        httpx.Client(
            transport=httpx.MockTransport(bad_success), follow_redirects=True,
        ) as client,
    ):
        fh.get_harvest_sources(client, lambda: None, ["org-0001"])

    # success:false on org names call
    with (
        pytest.raises(RuntimeError, match="Failed to fetch org names"),
        httpx.Client(
            transport=httpx.MockTransport(bad_success), follow_redirects=True,
        ) as client,
    ):
        fh.get_organisation_ids(client, lambda: None)


def test_write_json_roundtrip(tmp_path):
    sources = [make_source(1, "org-0001"), make_source(2, "org-0002")]
    path = tmp_path / "harvest_sources.json"
    fh.write_json(sources, str(path))
    loaded = json.loads(path.read_text())
    assert loaded == sources


def test_main_writes_file(tmp_path, monkeypatch):
    """End-to-end via main(): writes downloads/harvest_sources.json."""
    orgs = [make_org(1), make_org(2)]
    sources = [make_source(1, "org-0001")]

    downloads_dir = tmp_path / "downloads"
    monkeypatch.setattr(fh, "DOWNLOADS_DIR", downloads_dir)

    def handler(request: httpx.Request) -> httpx.Response:
        if "all_fields" in request.url.params:
            return httpx.Response(
                200, json={"success": True, "result": orgs},
            )
        if request.url.path.endswith("/organization_list"):
            return httpx.Response(
                200, json={"success": True, "result": [o["name"] for o in orgs]},
            )
        # harvest_source_list: only org-0001 has a source
        if request.url.params.get("organization_id") == "org-0001":
            return httpx.Response(200, json={"success": True, "result": sources})
        return httpx.Response(200, json={"success": True, "result": []})

    real_client = fh.httpx.Client

    def fake_client(**kw):
        return real_client(
            transport=httpx.MockTransport(handler), follow_redirects=True,
        )

    monkeypatch.setattr(fh.httpx, "Client", fake_client)

    fh.main()  # must not raise

    out = downloads_dir / "harvest_sources.json"
    assert out.exists()
    loaded = json.loads(out.read_text())
    assert len(loaded) == 1
    assert loaded[0]["organization_id"] == "org-0001"
