#!/usr/bin/env python3
"""Download batches of datasets from data.gov.uk and save each to
downloads/<org-name>/<slug>-<id8>.json.

Downloads batches of datasets from data.gov.uk. By default this walks
organisations.json and picks the first N orgs that haven't been fetched
yet, so you can just run it again to continue where you left off. Orgs that
return zero datasets are recorded in no-datasets.json so they are not
re-queried on the next run.

With --continuous it keeps fetching batch after batch until every
organisation in organisations.json has been processed, so you can start it
once and let it work through the whole list. Data files are written and
no-datasets.json updated as it goes, so interrupting it is safe — just run
it again (with or without --continuous) to resume.

Usage: python scripts/download_datasets.py [options]

Options:
  --orgs <n>        Number of organisations to process per batch (default: 50)
  --per-org <n|all> Datasets per organisation, fetched 1000 at a time;
                    use "all" for everything an org has (default: 1000)
  --org <slug>      Process a single specific organisation only
  --offset <n>      Skip the first n organisations in organisations.json
  --force           Refetch and overwrite orgs/datasets already saved
  --continuous, -c  Keep processing batches until every org has been fetched
  --help, -h        Show this help

Examples:
  python scripts/download_datasets.py                               # next 50 orgs without data
  python scripts/download_datasets.py --continuous                  # fetch everything, batch by batch
  python scripts/download_datasets.py --continuous --force --per-org all
  python scripts/download_datasets.py --orgs 50 --per-org 20
  python scripts/download_datasets.py --org environment-agency
  python scripts/download_datasets.py --force

Rate limit: 4 requests per second (scripts/rate_limit.py).
"""

import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import cast

import click
import httpx
import typer

from scripts.rate_limit import create_rate_limiter

app = typer.Typer(add_completion=False)

BASE_URL = "https://www.data.gov.uk/api/3/action"
MAX_RPS = 4
DEFAULT_ORG_COUNT = 50
DEFAULT_DATASETS_PER_ORG = 1000
MAX_ROWS_PER_CALL = 1000  # hard cap per package_search call
OUTPUT_DIR = "downloads"
NO_DATASETS_FILE = "downloads/no-datasets.json"
SORT = "metadata_created:desc"  # fixed sort for the API call


class OrgNotFoundError(RuntimeError):
    """Single-org mode asked for a slug that isn't in organisations.json.

    Carries a hint to print after the message.
    """

    def __init__(self, slug: str):
        super().__init__(f'Organisation not found: "{slug}"')
        self.hint = "Check organisations.json or run fetch-organisations.py."


# ---------------------------------------------------------------------------
# Timestamps (ISO 8601 UTC with milliseconds and Z, e.g. 2026-08-03T15:04:29.901Z)
# ---------------------------------------------------------------------------
def iso_now() -> str:
    """Current UTC time, ISO 8601 with milliseconds — e.g. 2026-08-03T15:04:29.901Z."""

    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ---------------------------------------------------------------------------
# Progress tracking (no-datasets.json read/write + detection)
# ---------------------------------------------------------------------------
def load_no_datasets(path: str = NO_DATASETS_FILE) -> dict:
    """Org slugs that previously returned zero datasets (never re-query them).

    Any read/parse failure returns {} — a corrupted file shouldn't crash
    the run. A parse that succeeds on a non-object also returns {}.
    """

    try:
        with Path(path).open(encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def save_no_datasets(no_datasets: dict, path: str = NO_DATASETS_FILE) -> None:
    """Write the marker map (indent-2 JSON)."""

    with Path(path).open("w", encoding="utf-8") as f:
        json.dump(no_datasets, f, indent=2, ensure_ascii=False)


def has_saved_datasets(org_name: str) -> bool:
    """Does this org already have saved dataset files in downloads/ ?"""

    d = Path(OUTPUT_DIR) / org_name
    if not d.is_dir():
        return False
    return any(p.suffix == ".json" for p in d.iterdir())


# ---------------------------------------------------------------------------
# Slugify a dataset title into a safe filename
# ---------------------------------------------------------------------------
def slugify(title: str) -> str:
    """Slugify a dataset title for its filename.

    Lowercase, [^a-z0-9]+ -> '-', trim dashes, [:80]. The character class
    is explicit ASCII, so no re.ASCII needed here.
    """

    s = title.lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)  # non-alphanumeric → dash
    s = re.sub(r"^-+|-+$", "", s)  # trim dashes
    return s[:80]  # keep reasonable length


# ---------------------------------------------------------------------------
# Fetch datasets for one org, paginating with rows=1000 per call until the
# requested limit is reached (or all results are exhausted for "all").
# ---------------------------------------------------------------------------
def fetch_datasets(
    rate_limit,
    client: httpx.Client,
    org_name: str,
    limit: float,
    sort: str,
) -> list[dict]:
    """Fetch one org's datasets, page by page, until exhausted or limit hit.
    `limit` is float('inf') for --per-org all.

    Each page blocks on the shared limiter's slot before the API call.
    """

    sort_param = sort.replace(":", " ", 1)  # first ':' only
    results: list[dict] = []
    offset = 0

    while offset < limit:
        rows = int(min(limit - offset, MAX_ROWS_PER_CALL))

        rate_limit()

        params = {
            "q": "",
            "fq": f"organization:{org_name}",
            "rows": str(rows),
            "start": str(offset),
            "sort": sort_param,
        }

        res = client.get(f"{BASE_URL}/package_search", params=params)
        if not res.is_success:
            raise RuntimeError(f"HTTP {res.status_code}: {res.reason_phrase}")

        body = res.json()
        if not body.get("success"):
            raise RuntimeError("CKAN API returned success: false")

        page = body["result"]["results"]
        results.extend(page)
        offset += len(page)

        # Fewer results than requested → no more pages left
        if len(page) < rows:
            break

    return results


# ---------------------------------------------------------------------------
# Decide which orgs the next batch covers
#
# `cursor` is only used in continuous + force mode: it advances through the
# org list so each batch is the next contiguous slice. Single-run and
# non-force continuous modes pass no cursor, so it falls back to `offset`.
# ---------------------------------------------------------------------------
def select_batch(
    orgs: list[dict],
    no_datasets: dict,
    org_count: int,
    offset: int,
    *,
    org_slug: str | None = None,
    force: bool = False,
    cursor: int | None = None,
) -> list[dict]:
    """Choose which orgs to fetch next (single-org / force / next-batch modes)."""

    if org_slug:
        # Single org mode — always re-query, even if previously marked empty
        org = next((o for o in orgs if o["name"] == org_slug), None)
        if org is None:
            raise OrgNotFoundError(org_slug)
        no_datasets.pop(org["name"], None)
        return [org]

    if force:
        # Force mode — take a contiguous slice and clear their empty markers.
        # In continuous mode `cursor` advances each batch so we walk the whole
        # list; in single-run mode it falls back to `offset`.
        start = cursor if cursor is not None else offset
        batch = orgs[start : start + org_count]
        for o in batch:
            no_datasets.pop(o["name"], None)
        save_no_datasets(no_datasets)
        return batch

    # Next batch — walk the list and pick orgs that still need fetching
    batch = []
    for org in orgs:
        if len(batch) >= org_count:
            break
        if no_datasets.get(org["name"]):
            continue
        if has_saved_datasets(org["name"]):
            continue
        batch.append(org)
    return batch


# ---------------------------------------------------------------------------
# Fetch and save datasets for one batch of orgs
# ---------------------------------------------------------------------------
def process_batch(
    batch: list[dict],
    per_org: float,
    rate_limit,
    client: httpx.Client,
    no_datasets: dict,
    *,
    force: bool,
) -> dict:
    """Fetch one batch of orgs. Returns {totalDatasets, skippedOrgs, skippedDatasets}.

    Per-org errors are caught and logged (stderr) — one bad org doesn't
    stop the batch. The catch tuple is the project's specific set, not a
    blind except: a genuine bug (e.g. a dataset record missing `title`)
    still surfaces as a traceback.
    """

    total_datasets = 0
    skipped_orgs = 0
    skipped_datasets = 0

    for i, org in enumerate(batch):
        org_name = org["name"]
        display = org.get("display_name") or org_name
        dir_path = Path(OUTPUT_DIR) / org_name

        print(f"[{i + 1}/{len(batch)}] {display} ({org_name})")

        # Skip orgs we already have data for (unless --force)
        existing = [p.name for p in dir_path.iterdir() if p.suffix == ".json"] if dir_path.is_dir() else []
        if existing and not force:
            print(f"  → already have {len(existing)} datasets, skipping")
            skipped_orgs += 1
            continue

        try:
            datasets = fetch_datasets(rate_limit, client, org_name, per_org, SORT)

            if not datasets:
                print("  → no datasets found")
                no_datasets[org_name] = iso_now()
                save_no_datasets(no_datasets)
                continue

            saved = 0
            for ds in datasets:
                # Include the dataset id so long titles sharing an 80-char slug
                # prefix (e.g. "...January 2024/2025/2026") can't collide and
                # get dropped. id[:8] on a UUID.
                filename = f"{slugify(ds['title'])}-{ds['id'][:8]}.json"
                filepath = dir_path / filename

                # Skip datasets we already have (unless --force)
                if filepath.exists() and not force:
                    skipped_datasets += 1
                    continue

                dir_path.mkdir(parents=True, exist_ok=True)

                # Add org context to the stored data —
                # { _fetched_at, _organisation: { name, display_name }, ... }.
                # JSON.stringify drops an undefined display_name (missing key);
                # mirror that by omitting it rather than writing null.
                org_ctx = {"name": org["name"]}
                if "display_name" in org:
                    org_ctx["display_name"] = org["display_name"]
                record = {
                    "_fetched_at": iso_now(),
                    "_organisation": org_ctx,
                    **ds,
                }

                filepath.write_text(
                    json.dumps(record, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                total_datasets += 1
                saved += 1

            print(f"  → saved {saved} datasets")
        except (httpx.HTTPError, RuntimeError, ValueError, OSError) as e:
            print(f"  ✗ error: {e}", file=sys.stderr)

    return {
        "totalDatasets": total_datasets,
        "skippedOrgs": skipped_orgs,
        "skippedDatasets": skipped_datasets,
    }


# ---------------------------------------------------------------------------
# CLI argument parsing
# ---------------------------------------------------------------------------
def parse_per_org(value: str) -> float:
    """'all' -> Infinity, otherwise an integer >= 1.

    Bad values raise click.BadParameter, which typer turns into a usage
    error (exit 2) — a non-zero exit is all callers rely on.
    """

    if value == "all":
        return float("inf")
    try:
        n = int(value)
    except ValueError:
        raise click.BadParameter('must be an integer >= 1, or "all"') from None
    if n < 1:
        raise click.BadParameter("must be an integer >= 1")
    return float(n)


def per_org_label(per_org: float) -> str:
    """Infinity -> 'all', otherwise the integer."""

    return "all" if per_org == float("inf") else str(int(per_org))


def load_orgs(path: Path = Path("downloads") / "organisations.json") -> list[dict] | None:
    """Read downloads/organisations.json; None when missing/unreadable/not
    a list.

    A parse failure exits 1 with the "No organisations.json found" message
    (a parsed non-list is treated the same way).
    """

    try:
        with Path(path).open(encoding="utf-8") as f:
            orgs = json.load(f)
    except (OSError, ValueError):
        return None
    return orgs if isinstance(orgs, list) else None


# ---------------------------------------------------------------------------
# Main (continuous mode and single-run mode)
# ---------------------------------------------------------------------------
def run(
    orgs: list[dict],
    org_count: int,
    per_org: float,
    org_slug: str | None,
    offset: int,
    *,
    force: bool,
    continuous: bool,
) -> None:
    """Fetch one run's worth of orgs. One shared rate limiter for the whole
    run — each limiter is only effective if it sees the full stream of
    requests.
    """

    rate_limit = create_rate_limiter(MAX_RPS)
    no_datasets = load_no_datasets()
    label = per_org_label(per_org)

    with httpx.Client(follow_redirects=True, timeout=30) as client:
        if continuous:
            batch_no = 0
            total_datasets = 0
            skipped_orgs = 0
            skipped_datasets = 0

            print(
                f"Continuous mode: fetching every organisation in batches of {org_count}, {label} dataset(s) each.",
            )
            print(f"Output: {OUTPUT_DIR}/<org-name>/<dataset-title>.json")

            cursor = offset  # advances through the org list in --force mode
            while True:
                batch = select_batch(
                    orgs,
                    no_datasets,
                    org_count,
                    offset,
                    org_slug=org_slug,
                    force=force,
                    cursor=cursor,
                )
                if not batch:
                    break
                cursor += org_count

                batch_no += 1
                print(f"\n=== Batch {batch_no}: {len(batch)} organisation(s) ===")

                result = process_batch(
                    batch,
                    per_org,
                    rate_limit,
                    client,
                    no_datasets,
                    force=force,
                )

                total_datasets += result["totalDatasets"]
                skipped_orgs += result["skippedOrgs"]
                skipped_datasets += result["skippedDatasets"]
                print(
                    f"--- batch complete: {result['totalDatasets']} dataset(s) saved ---",
                )

            print(
                f"\nAll done. {total_datasets} datasets saved across {batch_no} batch(es).",
            )
            if skipped_orgs or skipped_datasets:
                print(
                    f"Skipped {skipped_orgs} org(s) and {skipped_datasets} dataset(s) already fetched.",
                )
            return

        # Single-run mode (default)
        batch = select_batch(
            orgs,
            no_datasets,
            org_count,
            offset,
            org_slug=org_slug,
            force=force,
        )

        if not batch:
            print("No organisations left to fetch.")
            return

        print(f"Processing {len(batch)} organisation(s), {label} dataset(s) each...\n")
        print(f"Output: {OUTPUT_DIR}/<org-name>/<dataset-title>.json\n")

        result = process_batch(batch, per_org, rate_limit, client, no_datasets, force=force)

        print(f"\nDone. {result['totalDatasets']} datasets saved to {OUTPUT_DIR}/")
        if result["skippedOrgs"] or result["skippedDatasets"]:
            print(
                f"Skipped {result['skippedOrgs']} org(s) and "
                f"{result['skippedDatasets']} dataset(s) already fetched. "
                f"Use --force to refetch.",
            )


@app.command()
def main(
    *,
    org_count: int = typer.Option(
        DEFAULT_ORG_COUNT,
        "--orgs",
        min=1,
        help="Number of organisations to process per batch (default: 50)",
    ),
    per_org: str = typer.Option(
        str(DEFAULT_DATASETS_PER_ORG),
        "--per-org",
        callback=parse_per_org,
        help='Datasets per organisation, fetched 1000 at a time; use "all" for everything an org has (default: 1000)',
    ),
    org_slug: str | None = typer.Option(
        None,
        "--org",
        help="Process a single specific organisation only",
    ),
    offset: int = typer.Option(
        0,
        "--offset",
        min=0,
        help="Skip the first n organisations in organisations.json",
    ),
    force: bool = typer.Option(
        False,  # noqa: FBT003 — typer.Option's default is the first positional
        "--force",
        help="Refetch and overwrite orgs/datasets already saved",
    ),
    continuous: bool = typer.Option(
        False,  # noqa: FBT003 — typer.Option's default is the first positional
        "--continuous",
        "-c",
        help="Keep processing batches until every org has been fetched",
    ),
) -> None:
    """Fetch datasets from data.gov.uk and save each to downloads/<org-name>/<slug>-<id8>.json."""

    if continuous and org_slug:
        print(
            "--continuous cannot be combined with --org (it would never finish).",
            file=sys.stderr,
        )
        raise typer.Exit(1)

    orgs = load_orgs()
    if orgs is None:
        print(
            "No organisations.json found. Run fetch-organisations.py first.",
            file=sys.stderr,
        )
        raise typer.Exit(1)

    try:
        run(
            orgs=orgs,
            org_count=org_count,
            # parse_per_org already converted the CLI string to a float (or
            # inf for "all") — the annotation must stay str so typer hands
            # the raw string to the callback instead of converting it itself.
            per_org=cast("float", per_org),
            org_slug=org_slug,
            offset=offset,
            force=force,
            continuous=continuous,
        )
    except OrgNotFoundError as e:
        print(str(e), file=sys.stderr)
        if e.hint:
            print(e.hint, file=sys.stderr)
        raise typer.Exit(1) from None
    except (httpx.HTTPError, RuntimeError, ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        raise typer.Exit(1) from None


if __name__ == "__main__":
    app()
