r"""Sync query layer for the Explorer — all raw SQL through
django.db.connection, psycopg3's native `%s` placeholder style.

Row shape: dicts keyed by column name (key access in the views and
templates). Two things to know about the row shape:

- jsonb columns come back as JSON strings — Django's psycopg backend
  registers str loaders for raw cursors (the views keep their
  json.loads calls).
- A literal `%` in a statement must be written doubled (%%…%%) — see
  core.py's docstring for the placeholder rule.

Organised by data domain, not by consuming view. Import directly from the
module for the table you're querying — import everything from the package
root is intentionally not provided, so a view's dependency list shows its
data domains:

- ``core`` — the Query class and row-fetching plumbing (no statements)
- ``organisations`` — organisations-table statements + yearly_org_counts
- ``datasets`` — datasets-table statements + the /datasets builder
  (datasets_stmts) + yearly_dataset_counts
- ``embeddings`` — pgvector/semantic statements
- ``links`` — links-table statements + the /links builder (links_stmts)
- ``metadata`` — metadata_keys/metadata_values statements
- ``reports`` — the REPORTS definitions and report_stmts builder
- ``dashboard`` — home-dashboard card assembly (totals + one count per
  report)
- ``series`` — series-table statements + the /series builder
  (series_list_stmt) + series_built
- ``reviews`` — the review helpers (latest_reviews, get_review)
"""
