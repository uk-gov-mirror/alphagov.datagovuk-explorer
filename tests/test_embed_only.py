"""Unit tests for scripts/embed_only.py (offline — no llama-server, no DB).

Covers the deterministic algorithmic core per the plan:
- build_texts: BGE prefix, notes[:500] truncation, whitespace collapse,
  None for empty texts, empty-title handling
- assemble_batch: null-skipping + origIndex mapping (the request/response
  alignment logic)
- constants: DIM 768, BATCH 256, model, prefix

The full pipeline (texts -> llama-server -> pgvector write) is verified
separately against a scratch DB by comparing the stored embeddings with
float tolerance.

Run with: uv run pytest tests/test_embed_only.py
"""

import os

# The module-level guard fires on import if DATABASE_URL is unset — tests
# never connect, so give it a dummy URL.
os.environ.setdefault("DATABASE_URL", "postgresql://localhost:5432/test-db")

import scripts.embed_only as eo


def row(id_, title, notes=None):
    return {"id": id_, "title": title, "notes": notes}


PREFIX = "Represent this sentence for searching relevant passages: "


def test_constants():
    assert eo.DIM == 768
    assert eo.BATCH == 256
    assert eo.MODEL == "bge-base-en-v1.5"
    assert eo.BGE_PREFIX == PREFIX
    print("ok: constants match (DIM, BATCH, model, prefix)")


def test_basic_prefix_title_notes():
    rows = [
        row("a1", "Planning Applications 2020", "Applications received and decided."),
    ]
    texts = eo.build_texts(rows)
    assert texts == [
        PREFIX + "Planning Applications 2020 Applications received and decided.",
    ]
    print("ok: prefix + title + notes")


def test_notes_truncated_to_500():
    notes = "x" * 700
    texts = eo.build_texts([row("a1", "Long Notes", notes)])
    assert texts == [PREFIX + f"Long Notes {'x' * 500}"]
    # exactly 500 passes through whole
    assert eo.build_texts([row("a1", "T", "y" * 500)]) == [PREFIX + "T " + "y" * 500]
    print("ok: notes truncated to 500 chars")


def test_null_or_empty_notes():
    # None notes -> only the title
    assert eo.build_texts([row("a1", "Title Only", None)]) == [PREFIX + "Title Only"]
    # empty-string notes behave the same as None
    assert eo.build_texts([row("a1", "Title Only", "")]) == [PREFIX + "Title Only"]
    print("ok: None/empty notes -> title-only text")


def test_whitespace_collapse():
    rows = [row("a1", "  Census   Data \n 2021 ", "  Multiple\tspaces\nin notes.\n")]
    texts = eo.build_texts(rows)
    assert texts == [PREFIX + "Census Data 2021 Multiple spaces in notes."]
    # unicode whitespace (NBSP) collapses too — \s includes it
    rows = [row("a1", "A\u00a0B", "\u00a0notes\u00a0")]
    assert eo.build_texts(rows) == [PREFIX + "A B notes"]
    print("ok: whitespace collapse (incl. NBSP)")


def test_empty_title_is_prefix_only():
    # The constant prefix means the text is never empty: an empty title +
    # empty notes embeds the bare prefix (the null fallback is dead code).
    # the trailing space after the colon is stripped by trim
    bare = PREFIX.rstrip()
    texts = eo.build_texts([row("a1", "", None), row("a2", "", "")])
    assert texts == [bare, bare]
    assert all(t is not None for t in texts)
    print("ok: empty title -> bare prefix, never None")


def test_ordering_preserved():
    rows = [
        row("a1", "First", "notes one"),
        row("a2", "Second", None),
        row("a3", "  Third  ", "notes three"),
    ]
    texts = eo.build_texts(rows)
    assert texts[0] == PREFIX + "First notes one"
    assert texts[1] == PREFIX + "Second"
    assert texts[2] == PREFIX + "Third notes three"
    print("ok: texts keep row order")


def test_assemble_batch_skips_none():
    texts: list[str | None] = ["t0", None, "t2", "t3", None]
    inp, idx = eo.assemble_batch(texts, 0, 5)
    assert inp == ["t0", "t2", "t3"]
    assert idx == [0, 2, 3]
    # partial window
    inp, idx = eo.assemble_batch(texts, 2, 5)
    assert inp == ["t2", "t3"]
    assert idx == [2, 3]
    # window with only nulls
    inp, idx = eo.assemble_batch(texts, 1, 2)
    assert inp == []
    assert idx == []
    # empty window
    inp, idx = eo.assemble_batch(texts, 4, 4)
    assert inp == []
    assert idx == []
    print("ok: assemble_batch null-skipping + origIndex")
