#!/usr/bin/env python3
"""Load per-dataset suggestion JSON files into the `suggestions` table.

The pipeline writes one JSON file per dataset under
  downloads/suggestions/<org>/   (theme/tags/title/desc from scripts/llm/suggest.py)

This script (re)populates the DB table the web app reads for /suggestions.

Idempotent: TRUNCATEs `suggestions` then reloads — run it after any
suggest run to refresh the site. Failed (ok:false) records are skipped.

Usage: python -m scripts.llm.ingest_suggestions [--suggestions-dir downloads/suggestions]
       DATABASE_URL=postgresql://localhost:5432/other python -m scripts.llm.ingest_suggestions
"""

import json
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from scripts.db import connect, database_url

DEFAULT_SUGGESTIONS_DIR = Path(__file__).resolve().parent.parent.parent / "downloads" / "suggestions"


def _read_file(f: Path) -> dict | None:
    try:
        return json.loads(f.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def load_records(directory: Path) -> list[dict]:
    """All records from per-dataset JSON files; corrupt files skipped."""
    if not directory.exists():
        return []
    files = sorted(directory.rglob("*.json"))
    records: list[dict] = []
    with ThreadPoolExecutor(max_workers=32) as pool:
        for result in as_completed(pool.submit(_read_file, f) for f in files):
            r = result.result()
            if r is not None:
                records.append(r)
    return records


def ingest(db, records: list[dict]) -> int:
    """Truncate + COPY ok records whose dataset exists locally; returns inserted count.

    Source records are already keyed by CKAN guid, so they are copied straight
    through — no id mapping.
    """
    ids = [r["dataset_id"] for r in records]
    rows = db.prepare("SELECT ckan_id FROM datasets WHERE ckan_id = ANY(?)").all(ids)
    existing = {row["ckan_id"] for row in rows}
    present = [r for r in records if r.get("ok") and r["dataset_id"] in existing]
    skipped = len(records) - len(present)
    if skipped:
        print(f"Skipped {skipped} suggestion(s) — ok:false or dataset not in local DB.")

    # A retitled dataset gets a new filename, leaving the old file behind: keep
    # the most recently classified record per dataset and warn about the rest.
    latest: dict[str, dict] = {}
    for r in present:
        kept = latest.get(r["dataset_id"])
        if kept is None or r.get("classified_at", "") > kept.get("classified_at", ""):
            latest[r["dataset_id"]] = r
    duplicates = len(present) - len(latest)
    if duplicates:
        print(f"Warning: skipped {duplicates} older duplicate suggestion(s) for the same dataset.", file=sys.stderr)
    present = list(latest.values())

    copy_sql = (
        'COPY suggestions (dataset_ckan_id, theme, theme_confidence, tags, title, "desc", created_at, json) FROM STDIN'
    )
    with db.conn.transaction(), db.conn.cursor() as cur:
        cur.execute("TRUNCATE suggestions RESTART IDENTITY")
        with cur.copy(copy_sql) as copy:
            for r in present:
                copy.write_row(
                    (
                        r["dataset_id"],
                        r.get("suggested_theme"),
                        r.get("suggested_theme_confidence"),
                        json.dumps(r["suggested_tags"], ensure_ascii=False) if r.get("suggested_tags") else None,
                        r.get("suggested_title"),
                        r.get("suggested_description"),
                        r.get("classified_at"),
                        json.dumps(r, ensure_ascii=False),
                    ),
                )

    return len(present)


def main(suggestions_dir: str = str(DEFAULT_SUGGESTIONS_DIR)) -> None:
    suggestions_path = Path(suggestions_dir)
    records = load_records(suggestions_path)

    if not records:
        print(f"No files in {suggestions_path} — nothing to do.", file=sys.stderr)
        sys.exit(1)

    print(f"Loaded {len(records)} suggestion(s) from {suggestions_path}.")

    db = connect(database_url())
    try:
        n = ingest(db, records)
    finally:
        db.close()
    print(f"Inserted {n} row(s) into suggestions.")


if __name__ == "__main__":
    try:
        main(*sys.argv[1:])
    except (RuntimeError, ValueError, OSError, KeyError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
