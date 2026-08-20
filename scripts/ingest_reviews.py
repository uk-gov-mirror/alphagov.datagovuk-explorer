#!/usr/bin/env python3
"""Load data/dataset-reviews-suggestions.jsonl into the `reviews` table.

The pipeline (review_suggest.py) keeps appending to the JSONL as the
audit artifact; this script (re)populates the DB table the web app reads.

Idempotent: TRUNCATEs `reviews` then reloads — run it after any
review_suggest run to refresh the site. Dedup rules:
- records are walked in file order and later lines win, so only the
  latest record per dataset_id survives;
- failed (ok:false) records are kept in the table with their flag; the
  views filter ok = true at query time.

Usage: python -m scripts.ingest_reviews [--file data/dataset-reviews-suggestions.jsonl]
       DATABASE_URL=postgresql://localhost:5432/other python -m scripts.ingest_reviews
"""

import json
import sys
from pathlib import Path

from scripts.db import connect, database_url

DEFAULT_FILE = Path(__file__).resolve().parent.parent / "data" / "dataset-reviews-suggestions.jsonl"


def load_records(path: Path) -> list[dict]:
    """All JSONL records in file order; corrupt lines skipped."""
    records: list[dict] = []
    if not path.exists():
        return records
    for line in path.read_text(encoding="utf-8").split("\n"):
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue  # ignore corrupt lines
    return records


def latest_per_dataset(records: list[dict]) -> list[dict]:
    """Later lines win — one record per dataset_id."""
    by_id: dict[str, dict] = {}
    for r in records:
        by_id[r["dataset_id"]] = r
    return list(by_id.values())


def _scores(r: dict) -> dict:
    """The three sub-scores, or {} when scores is missing/malformed."""
    scores = r.get("scores")
    return scores if isinstance(scores, dict) else {}


def _subscore(r: dict, key: str):
    sub = _scores(r).get(key)
    if not isinstance(sub, dict):
        return None
    score = sub.get("score")
    return score if isinstance(score, int) else None


def _int(v):
    return v if isinstance(v, int) else None


def ingest(db, records: list[dict]) -> int:
    """Truncate + insert the deduped records; returns the row count."""

    def _run(tx) -> None:
        tx.exec("TRUNCATE reviews RESTART IDENTITY")
        stmt = tx.prepare(
            """INSERT INTO reviews
               (id, dataset_id, ok, overall, findability, metadata, resources,
                theme, tags, title, "desc", theme_confidence, created_at, json)
               VALUES (DEFAULT, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        )
        for r in records:
            stmt.run(
                r["dataset_id"],
                bool(r.get("ok")),
                _int(r.get("overall")),
                _subscore(r, "findability"),
                _subscore(r, "metadata"),
                _subscore(r, "resources"),
                r.get("theme"),
                json.dumps(r["tags"], ensure_ascii=False) if r.get("tags") else None,
                r.get("suggested_title"),
                r.get("suggested_description"),
                r.get("theme_confidence"),
                r.get("reviewed_at"),
                json.dumps(r, ensure_ascii=False),
            )

    db.transaction(_run)
    return len(records)


def main(file: str = str(DEFAULT_FILE)) -> None:
    path = Path(file)
    records = load_records(path)
    if not records:
        print(f"No records in {path} — nothing to do.", file=sys.stderr)
        sys.exit(1)

    unique = latest_per_dataset(records)
    print(
        f"Loaded {len(records)} record(s) from {path.name}; "
        f"{len(records) - len(unique)} duplicate(s) dropped, "
        f"{len(unique)} latest-per-dataset kept.",
    )

    db = connect(database_url())
    try:
        n = ingest(db, unique)
    finally:
        db.close()
    print(f"Inserted {n} row(s) into reviews.")


if __name__ == "__main__":
    try:
        main(*sys.argv[1:])
    except (RuntimeError, ValueError, OSError, KeyError) as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
