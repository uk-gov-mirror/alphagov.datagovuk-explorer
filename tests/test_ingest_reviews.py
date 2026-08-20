"""Unit tests for scripts/ingest_reviews.py (offline — no DB).

Covers the deterministic parts:
- load_records: missing file, corrupt-line skip, file order
- latest_per_dataset: later lines win (one record per dataset_id)
- _subscore / _int: malformed-scores handling for the typed columns

The live ingest (TRUNCATE + insert into reviews, idempotency, FK against
datasets) is verified by running the script against the real DB.
Run with: uv run pytest tests/test_ingest_reviews.py
"""

import json
import tempfile
from pathlib import Path

import scripts.ingest_reviews as ir


def rec(dataset_id, n):
    """A minimal record — the fields load/dedup care about."""
    return {
        "dataset_id": dataset_id,
        "title": f"Title {n}",
        "ok": True,
        "overall": n % 6,
        "reviewed_at": f"2026-08-01T00:00:0{n}.000Z",
    }


def test_load_records():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d) / "out.jsonl"
        # missing file -> []
        assert ir.load_records(p) == []

        p.write_text(
            json.dumps(rec("a", 1)) + "\n"
            "corrupt line not json\n" + json.dumps(rec("b", 2)) + "\n"
            "\n" + json.dumps(rec("c", 3)) + "\n",
            encoding="utf-8",
        )
        records = ir.load_records(p)
        # corrupt + blank lines skipped, order kept
        assert [r["dataset_id"] for r in records] == ["a", "b", "c"]
    print("ok: load_records (missing / corrupt skip / order)")


def test_latest_per_dataset():
    # Build records with duplicate dataset_ids — later lines must win.
    records = [rec(f"id-{n % 3}", n) for n in range(9)]  # 3 ids x 3 records
    unique = ir.latest_per_dataset(records)
    assert len(unique) == 3
    # one row per id, in first-seen id order
    assert [r["dataset_id"] for r in unique] == ["id-0", "id-1", "id-2"]
    # the latest (highest n) per id survives
    assert {r["dataset_id"]: r["title"] for r in unique} == {
        "id-0": "Title 6",
        "id-1": "Title 7",
        "id-2": "Title 8",
    }
    # later duplicate of the same id replaces the earlier record entirely
    dup = [rec("id-x", 1), rec("id-x", 2)]
    assert ir.latest_per_dataset(dup) == [rec("id-x", 2)]
    print("ok: latest_per_dataset (later lines win)")


def test_typed_column_helpers():
    # _subscore pulls scores.<key>.score; malformed -> None
    r = {
        "scores": {
            "findability": {"score": 4},
            "metadata": "not a dict",
            "resources": {"score": None},
        },
    }
    assert ir._subscore(r, "findability") == 4
    assert ir._subscore(r, "metadata") is None
    assert ir._subscore(r, "resources") is None
    assert ir._subscore({}, "findability") is None

    # _int keeps ints only (bools are ints in Python — records use real ints)
    assert ir._int(3) == 3
    assert ir._int(None) is None
    assert ir._int("3") is None
    print("ok: _subscore / _int (malformed -> None)")
