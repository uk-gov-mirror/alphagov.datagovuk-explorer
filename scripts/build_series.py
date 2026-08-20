#!/usr/bin/env python3
"""Build series data from dataset titles.

Scans the datasets table for title patterns that suggest series membership:
  1. Exact duplicate titles (same title, different datasets)
  2. Date-suffix clusters (common root with varying dates at the end)

Stores results in two tables:
  series          — one row per detected series
  series_datasets — junction linking series to their datasets

Run after build_db.py (needs the datasets table to exist).

Usage: python scripts/build_series.py
       DATABASE_URL=postgresql://localhost:5432/other python scripts/build_series.py
"""

import re
import sys

from scripts.db import connect, database_url

DATABASE_URL = database_url()

# ---- Date stripping patterns ----
# Applied in order; first match wins. Each strips a date-like suffix from the
# end of the title, returning (root_title, date_string). re.ASCII keeps \d and
# \b ASCII-only.
DATE_PATTERNS = [
    # 2020/21, 2020-21, 2020-2021
    re.compile(r"\s+\d{4}\s*[/-]\s*\d{2,4}\s*$", re.ASCII),
    # Q1 2020, Q4 2020/21 etc. (quarter prefix)
    re.compile(r"\s+Q[1-4]\s+\d{4}(\s*[/-]\s*\d{2,4})?\s*$", re.ASCII),
    # January 2020, December 2024
    re.compile(
        r"\s+(January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{4}\s*$",
        re.ASCII,
    ),
    # 2020 (plain year; must be at end and reasonable-looking)
    re.compile(r"\s+\(?\b(19|20)\d{2}\b\)?\s*$", re.ASCII),
    # (2020) — year in parens at end
    re.compile(r"\s*\(\d{4}\)\s*$", re.ASCII),
]

# Date-suffix roots shorter than this are too vague to be series titles.
MIN_ROOT_LENGTH = 5

# A series needs at least this many datasets.
MIN_SERIES_SIZE = 2


def strip_date(title: str) -> dict | None:
    """Strip a date-like suffix from the end of title.

    First pattern to match wins; the root is the title with the match cut
    off and trimmed. Returns {"root": ..., "date": ...} when the root is
    non-empty, None when no pattern matches or the root is empty.
    """

    for pattern in DATE_PATTERNS:
        m = pattern.search(title)
        if m:
            root = title[: m.start()].strip()
            if root:
                return {"root": root, "date": m.group(0).strip()}
    return None


def build_all_series(rows: list[dict]) -> tuple[list[dict], int, int]:
    """Detect series from dataset rows.

    rows: [{id, title, org_slug, org_display_name}, ...] — the datasets
    table rows with a non-empty title (the caller's WHERE clause).

    Returns (all_series, exact_series, date_series):
      all_series   — [{root_title, type, datasets}, ...]; Phase 1 exact
                     duplicate groups first, then Phase 2 date-suffix
                     clusters, both in insertion order (this fixes the
                     SERIAL ids assigned at insert time).
      exact_series — count of Phase 1 groups (2+ datasets)
      date_series  — count of Phase 2 clusters (2+ items)

    dataset_count / org_count are computed at insert time (see _write_series).
    """

    # ---- Phase 1: Exact duplicate titles ----
    exact_groups: dict[str, list[dict]] = {}
    for r in rows:
        t = r["title"].strip()
        exact_groups.setdefault(t, []).append(r)

    # ---- Phase 2: Date-suffix clusters ----
    root_groups: dict[str, dict] = {}
    for r in rows:
        result = strip_date(r["title"].strip())
        if result and len(result["root"]) >= MIN_ROOT_LENGTH:
            key = result["root"].lower()
            if key not in root_groups:
                root_groups[key] = {"root": result["root"], "items": []}
            root_groups[key]["items"].append({**dict(r), "date": result["date"]})

    # ---- Assemble ----
    all_series: list[dict] = []

    # Phase 1 results: same title, 2+ datasets. All one org -> timeseries
    # (a year-by-year re-publication); multiple orgs -> template (a shared
    # template adopted by many publishers).
    exact_series = 0
    for title, datasets in exact_groups.items():
        if len(datasets) < MIN_SERIES_SIZE:
            continue
        orgs = {d["org_slug"] for d in datasets}
        stype = "template" if len(orgs) > 1 else "timeseries"
        all_series.append({"root_title": title, "type": stype, "datasets": datasets})
        exact_series += 1

    # Phase 2 results: date-suffix clusters become timeseries. Some roots
    # overlap with Phase 1 exact-match groups (e.g. "Planning Applications"
    # exists both as an exact-match template across 14 councils AND as a
    # Wigan year-by-year timeseries). That's fine — they become separate
    # series entries with different types.
    date_series = 0
    for group in root_groups.values():
        if len(group["items"]) < MIN_SERIES_SIZE:
            continue
        all_series.append(
            {
                "root_title": group["root"],
                "type": "timeseries",
                "datasets": group["items"],
            },
        )
        date_series += 1

    return all_series, exact_series, date_series


# The series tables are migration-owned (0001's Series/SeriesDataset
# models); the build truncates + repopulates, never creates.
# RESTART IDENTITY keeps series.id deterministic (1..n in insertion order).
# The junction indexes idx_series_datasets_* are migration-owned (0003) and
# survive because the tables are no longer dropped.
TRUNCATE_SQL = "TRUNCATE TABLE series_datasets, series RESTART IDENTITY CASCADE"


def _write_series(tx, all_series: list[dict]) -> None:
    """Insert all series + junction rows inside the transaction."""

    insert_series = tx.prepare(
        "INSERT INTO series (root_title, type, dataset_count, org_count) VALUES (?, ?, ?, ?) RETURNING id",
    )
    insert_sd = tx.prepare(
        "INSERT INTO series_datasets (series_id, dataset_id, dataset_title, date_suffix,"
        " org_slug, org_display_name) VALUES (?, ?, ?, ?, ?, ?)",
    )
    for s in all_series:
        orgs = {d["org_slug"] for d in s["datasets"]}
        result = insert_series.get(
            s["root_title"],
            s["type"],
            len(s["datasets"]),
            len(orgs),
        )
        series_id = result["id"]
        for d in s["datasets"]:
            insert_sd.run(
                series_id,
                d["id"],
                d["title"],
                d.get("date"),  # Phase 1 rows have no date -> NULL
                d["org_slug"],
                d["org_display_name"],
            )


def main() -> None:
    db = connect(DATABASE_URL)
    try:
        print("Reading datasets...")
        rows = db.prepare(
            "SELECT id, title, org_slug, org_display_name FROM datasets WHERE title IS NOT NULL AND title != ''",
        ).all()
        print(f"  {len(rows)} datasets with titles")

        all_series, exact_series, date_series = build_all_series(
            [dict(r) for r in rows],
        )

        print("\nPhase 1: exact duplicate titles...")
        print(f"  {exact_series} exact-duplicate series found")

        print("\nPhase 2: date-suffix clusters...")
        print(f"  {date_series} date-suffix series found (after dedup)")

        # ---- Write to database ----
        print("\nWriting series tables...")
        db.exec(TRUNCATE_SQL)
        db.transaction(lambda tx: _write_series(tx, all_series))

        # Stats
        stats = db.prepare(
            "SELECT type, COUNT(*) AS n, SUM(dataset_count) AS d FROM series GROUP BY type",
        ).all()
        print(f"\nDone. Series table: {len(all_series)} series")
        for s in stats:
            print(f"  {s['type']}: {s['n']} series, {s['d']} datasets")
    finally:
        db.close()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, ValueError, OSError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
