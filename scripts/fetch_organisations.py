#!/usr/bin/env python3
"""Fetch and display all organisations/publishers from data.gov.uk (CKAN API).

Prints a summary to stdout, writes the full data to
downloads/organisations.json (the gitignored API cache, alongside the
dataset files).

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


def get_organisations(client: httpx.Client) -> list[dict]:
    url = f"{BASE_URL}/organization_list"
    rate_limit = create_rate_limiter(MAX_RPS)

    # First, get all org names (fast, single request — no all_fields, and
    # not rate-limited).
    names_res = client.get(url)
    names_body = names_res.json()
    if not names_body.get("success"):
        raise RuntimeError("Failed to fetch org names")
    names = names_body["result"]  # e.g. 1480 strings

    # Now fetch details in pages of 25 (the max the server allows with all_fields).
    all_orgs: list[dict] = []
    for offset in range(0, len(names), PAGE_SIZE):
        rate_limit()

        res = client.get(
            url,
            params={"all_fields": "true", "limit": PAGE_SIZE, "offset": offset},
        )
        if not res.is_success:
            raise RuntimeError(f"HTTP {res.status_code}: {res.reason_phrase}")

        body = res.json()
        if not body.get("success"):
            raise RuntimeError("CKAN API returned success: false")

        all_orgs.extend(body["result"])

        pct = round(len(all_orgs) / len(names) * 100)
        print(f"  {len(all_orgs)}/{len(names)} ({pct}%)", file=sys.stderr)

    return all_orgs


def write_json(orgs: list[dict], path: str) -> None:
    """Write orgs to path as indent-2 JSON."""
    with Path(path).open("w", encoding="utf-8") as f:
        json.dump(orgs, f, indent=2, ensure_ascii=False)


def main() -> None:
    print("Fetching organisations from data.gov.uk...\n")
    try:
        with httpx.Client(follow_redirects=True, timeout=30) as client:
            orgs = get_organisations(client)

        out_path = DOWNLOADS_DIR / "organisations.json"
        DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
        write_json(orgs, out_path)
        print(f"Wrote {len(orgs)} organisations to {out_path}\n")

        for org in orgs:
            name = org.get("display_name") or org["name"]
            desc = (org.get("description") or "(no description)")[:120].replace(
                "\n",
                " ",
            )
            count = org.get("package_count")
            if count is None:
                count = "?"  # 0 is a real count, not a missing one
            print(f"  {name}")
            print(f"    Datasets: {count}")
            print(f"    {desc}")
            print()
    except (httpx.HTTPError, RuntimeError, ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
