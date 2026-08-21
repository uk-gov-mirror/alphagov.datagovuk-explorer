#!/usr/bin/env python3
"""Fetch all harvest sources from data.gov.uk (CKAN API).

The unfiltered harvest_source_list endpoint caps at 100 sources (the site
has ~474), but it accepts an organization_id filter, so this script walks
every organisation and fetches its harvest sources per-org, then writes
the deduped union to downloads/harvest_sources.json (the gitignored API
cache, alongside the dataset files).

Each record is tagged with the organization_id it was fetched under,
because the API's own publisher_id/publisher_title fields are often empty.

Rate limit: 4 requests per second (scripts/rate_limit.py).
"""

import json
import sys
from pathlib import Path

import httpx

from scripts.rate_limit import create_rate_limiter

BASE_URL = "https://www.data.gov.uk/api/3/action"
MAX_RPS = 4
PAGE_SIZE = 25

DOWNLOADS_DIR = Path(__file__).resolve().parent.parent / "downloads"


def get_organisation_ids(client: httpx.Client, rate_limit) -> list[str]:
    """Fetch every organisation's id, paging through organization_list
    with all_fields in chunks of PAGE_SIZE (the server's max)."""

    # First, the cheap call: names only, no all_fields.
    names_res = client.get(f"{BASE_URL}/organization_list")
    names_body = names_res.json()
    if not names_body.get("success"):
        raise RuntimeError("Failed to fetch org names")
    names = names_body["result"]

    # Now page through all_fields to get the ids (rate-limited).
    org_ids: list[str] = []
    for offset in range(0, len(names), PAGE_SIZE):
        rate_limit()

        res = client.get(
            f"{BASE_URL}/organization_list",
            params={"all_fields": "true", "limit": PAGE_SIZE, "offset": offset},
        )
        if not res.is_success:
            raise RuntimeError(f"HTTP {res.status_code}: {res.reason_phrase}")
        body = res.json()
        if not body.get("success"):
            raise RuntimeError("CKAN API returned success: false")

        org_ids.extend(org["id"] for org in body["result"])

        pct = round(len(org_ids) / len(names) * 100)
        print(f"  orgs {len(org_ids)}/{len(names)} ({pct}%)", file=sys.stderr)

    return org_ids


def get_harvest_sources(
    client: httpx.Client, rate_limit, org_ids: list[str],
) -> list[dict]:
    """Fetch harvest sources per organisation and return the deduped union.

    harvest_source_list?organization_id=<id> is filtered server-side, so
    we make one call per org. Sources are tagged with the org id they came
    from, then deduped by source id.
    """

    sources: dict[str, dict] = {}
    for i, org_id in enumerate(org_ids, 1):
        rate_limit()

        res = client.get(
            f"{BASE_URL}/harvest_source_list",
            params={"organization_id": org_id},
        )
        if not res.is_success:
            raise RuntimeError(
                f"HTTP {res.status_code} fetching org {org_id}: {res.reason_phrase}",
            )
        body = res.json()
        if not body.get("success"):
            raise RuntimeError(f"CKAN API returned success: false for org {org_id}")

        for item in body.get("result", []):
            source = dict(item)
            source["organization_id"] = org_id
            sources[source["id"]] = source

        if i % 100 == 0 or i == len(org_ids):
            print(
                f"  harvest sources {len(sources)} (orgs {i}/{len(org_ids)})",
                file=sys.stderr,
            )

    return list(sources.values())


def write_json(sources: list[dict], path: str) -> None:
    """Write sources to path as indent-2 JSON."""
    with Path(path).open("w", encoding="utf-8") as f:
        json.dump(sources, f, indent=2, ensure_ascii=False)


def main() -> None:
    print("Fetching harvest sources from data.gov.uk...\n")
    try:
        with httpx.Client(follow_redirects=True, timeout=30) as client:
            rate_limit = create_rate_limiter(MAX_RPS)
            org_ids = get_organisation_ids(client, rate_limit)
            print(f"Fetched {len(org_ids)} organisations", file=sys.stderr)

            sources = get_harvest_sources(client, rate_limit, org_ids)

        out_path = DOWNLOADS_DIR / "harvest_sources.json"
        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        write_json(sources, out_path)
        print(f"Wrote {len(sources)} harvest sources to {out_path}\n")

        for source in sources:
            org = source.get("organization_id") or "?"
            print(f"  {source['title']} [{source.get('type')}]")
            print(f"    url: {source.get('url')}")
            print(f"    org: {org}")
            print()
    except (httpx.HTTPError, RuntimeError, ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
