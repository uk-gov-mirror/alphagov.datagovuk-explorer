#!/usr/bin/env python3
"""Query datasets for a given organisation from data.gov.uk (CKAN API).

Read-only: one package_search call against the live API, with the org
fuzzy-matched against the local organisations.json. Results go to stdout;
errors to stderr.

Usage: python scripts/query_datasets.py <org-name-or-id> [options]
       python scripts/query_datasets.py --list
       python scripts/query_datasets.py --help

Options:
  --list                 List all org names from local organisations.json
  --sort <field:dir>     Sort order (default: metadata_modified:desc)
  --rows <n>             Number of datasets to return (default: 10)

Sort fields:
  score              Relevance (needs a search query)
  metadata_modified  Last modified date
  metadata_created   First published date
  title_string       Alphabetical by title
  name               Alphabetical by slug
  views_total        Most viewed
  views_recent       Trending (recent views)

Examples:
  python scripts/query_datasets.py "environment-agency"
  python scripts/query_datasets.py "ons" --sort views_total:desc --rows 5
  python scripts/query_datasets.py "cabinet-office" --sort metadata_created:asc

Rate limit: 4 requests per second (scripts/rate_limit.py).
"""

import json
import sys
from pathlib import Path

import httpx
import typer

from scripts.rate_limit import create_rate_limiter

app = typer.Typer(add_completion=False)

BASE_URL = "https://www.data.gov.uk/api/3/action"
MAX_RPS = 4

# Deliberately CWD-relative (not __file__-relative): lets the CLI tests
# chdir into a tmp downloads/.
ORGS_JSON = Path("downloads") / "organisations.json"

VALID_SORT_FIELDS = [
    "score",
    "metadata_modified",
    "metadata_created",
    "title_string",
    "name",
    "views_total",
    "views_recent",
]


class AmbiguousOrgError(RuntimeError):
    """More than one org matched the fuzzy name lookup.

    The matches are printed to stderr and the CLI exits 1.
    """

    def __init__(self, name_or_id: str, matches: list[dict]):
        super().__init__(f'Multiple matches for "{name_or_id}"')
        self.name_or_id = name_or_id
        self.matches = matches


class InvalidSortError(RuntimeError):
    """Bogus sort field or direction (printed to stderr, exits 1)."""


# ---------------------------------------------------------------------------
# Local organisations.json lookup
# ---------------------------------------------------------------------------
def load_orgs(path: str | Path = ORGS_JSON) -> list[dict] | None:
    """Read organisations.json; None when missing/unreadable/not a list.

    A missing/unreadable file, or a successful parse of a non-array, all
    fall through to "no local file — use raw input".
    """

    try:
        with Path(path).open(encoding="utf-8") as f:
            orgs = json.load(f)
    except (OSError, ValueError):
        return None
    return orgs if isinstance(orgs, list) else None


def find_org(name_or_id: str, orgs: list[dict]) -> dict | None:
    """Fuzzy org lookup.

    Exact match on name or id first, then a case-insensitive partial match
    on display_name/name. One match -> that org, >1 -> AmbiguousOrgError
    (the CLI lists the matches and exits 1), none -> None (caller falls
    back to the raw input).
    """

    for o in orgs:
        if o.get("name") == name_or_id or o.get("id") == name_or_id:
            return o

    lower = name_or_id.lower()
    matches = [
        o for o in orgs if lower in (o.get("display_name") or "").lower() or lower in (o.get("name") or "").lower()
    ]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise AmbiguousOrgError(name_or_id, matches)
    return None


# ---------------------------------------------------------------------------
# Sort validation
# ---------------------------------------------------------------------------
def parse_sort(sort_arg: str | None) -> tuple[str, str]:
    """Validate --sort, return (field, direction).

    Default metadata_modified:desc; append :desc when the direction is
    missing; reject unknown fields/directions with the valid-field list.
    """

    sort = sort_arg or "metadata_modified:desc"
    if ":" not in sort:
        sort += ":desc"  # default direction when missing
    parts = sort.split(":")
    field, direction = parts[0], parts[1]

    if field not in VALID_SORT_FIELDS:
        raise InvalidSortError(
            f'Invalid sort field: "{field}"\nValid fields: {", ".join(VALID_SORT_FIELDS)}',
        )
    if direction not in ("asc", "desc"):
        raise InvalidSortError(
            f'Invalid sort direction: "{direction}". Use asc or desc.',
        )
    return field, direction


# ---------------------------------------------------------------------------
# API call
# ---------------------------------------------------------------------------
def get_datasets(
    client: httpx.Client,
    org_name: str,
    *,
    limit: int = 10,
    sort: str = "metadata_modified:desc",
) -> dict:
    """One package_search call for an org; returns body.result.

    Rate-limited (4/s) — one limiter per call, blocking on a slot first.
    """

    rate_limit = create_rate_limiter(MAX_RPS)
    rate_limit()

    params = {
        "q": "",
        "fq": f"organization:{org_name}",
        "rows": str(limit),
        # replace only the first ':' (the direction separator)
        "sort": sort.replace(":", " ", 1),
    }

    res = client.get(f"{BASE_URL}/package_search", params=params)
    if not res.is_success:
        raise RuntimeError(f"HTTP {res.status_code}: {res.reason_phrase}")

    body = res.json()
    if not body.get("success"):
        raise RuntimeError("CKAN API returned success: false")

    return body["result"]


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------
def format_org_line(o: dict) -> str:
    """--list line: `name  \t(N datasets)  \tdisplay_name`."""

    count = o.get("package_count")
    if count is None:
        count = "?"  # 0 is a real count, not a missing one
    return f"{o.get('name')}  \t({count} datasets)  \t{o.get('display_name') or o.get('name')}"


def format_dataset(ds: dict) -> list[str]:
    """The per-dataset stdout block."""

    title = ds.get("title") or ds.get("name")
    notes = ds.get("notes")
    desc = notes[:150].replace("\n", " ") if notes else "(no description)"
    resources = ds.get("resources") or []
    formats = list(dict.fromkeys(r.get("format") for r in resources if r.get("format")))
    modified = ds.get("metadata_modified")
    updated = modified[:10] if modified is not None else "?"
    return [
        f"  {title}",
        f"    ID:     {ds.get('name')}",
        f"    Updated: {updated}",
        f"    Formats: {', '.join(formats) if formats else 'none'}",
        f"    {desc}",
        "",
    ]


def print_orgs() -> None:
    """--list mode. Exits 1 when the file is missing."""

    orgs = load_orgs()
    if orgs is None:
        print(
            "No organisations.json found. Run fetch-organisations.py first.",
            file=sys.stderr,
        )
        raise typer.Exit(1)
    for o in orgs:
        print(format_org_line(o))


def fetch_and_print(org_name: str, rows: int, sort: str) -> None:
    """Fetch and print the listing."""

    with httpx.Client(follow_redirects=True, timeout=30) as client:
        result = get_datasets(client, org_name, limit=rows, sort=sort)

    if result["count"] == 0:
        print("No datasets found.")
        return

    print(
        f"Found {result['count']} datasets total. Showing {len(result['results'])}:\n",
    )
    for ds in result["results"]:
        for line in format_dataset(ds):
            print(line)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
@app.command()
def main(
    org_name_or_id: str | None = typer.Argument(
        None,
        metavar="org-name-or-id",
        help="organisation name or id (fuzzy-matched against organisations.json)",
    ),
    *,
    list_orgs: bool = typer.Option(
        False,  # noqa: FBT003 — typer.Option's default is the first positional
        "--list",
        help="List all org names from local organisations.json",
    ),
    sort: str | None = typer.Option(
        None,
        "--sort",
        help="Sort order (default: metadata_modified:desc)",
    ),
    rows: int = typer.Option(
        10,
        "--rows",
        min=1,
        help="Number of datasets to return (default: 10)",
    ),
) -> None:
    """Fetch datasets for a given organisation from data.gov.uk (CKAN package_search)."""

    if list_orgs:
        print_orgs()
        return

    if not org_name_or_id:
        print(
            "Usage: python scripts/query_datasets.py <org-name-or-id> [options]",
            file=sys.stderr,
        )
        print("       python scripts/query_datasets.py --help", file=sys.stderr)
        raise typer.Exit(1)

    try:
        sort_field, sort_dir = parse_sort(sort)
    except InvalidSortError as e:
        print(str(e), file=sys.stderr)
        raise typer.Exit(1) from None

    orgs = load_orgs()
    try:
        org = find_org(org_name_or_id, orgs) if orgs is not None else None
    except AmbiguousOrgError as e:
        print(f'Multiple matches for "{e.name_or_id}":', file=sys.stderr)
        for m in e.matches[:10]:
            print(
                f"  {m.get('display_name') or m.get('name')}  ({m.get('name')})",
                file=sys.stderr,
            )
        raise typer.Exit(1) from None

    org_name = org["name"] if org else org_name_or_id

    if org:
        print(f"Organisation: {org.get('display_name') or org['name']} ({org['name']})")
        if org.get("package_count"):
            print(f"Total datasets: {org['package_count']}")
        print()

    sort_display = f"{sort_field}:{sort_dir}"
    print(f"Fetching {rows} datasets (sort: {sort_display})...\n")

    try:
        fetch_and_print(org_name, rows, sort_display)
    except (httpx.HTTPError, RuntimeError, ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(1) from None


if __name__ == "__main__":
    app()
