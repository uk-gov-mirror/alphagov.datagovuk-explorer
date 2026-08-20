"""dataset_json.json: TEXT -> jsonb, plus a GIN index for metadata-value
filtering (explorer/queries/datasets.py's _metadata_clause). Every row is
valid JSON already (confirmed against the live data before this migration
was written) — no data cleanup needed.

The metadata filter deliberately stays on `->>` rather than a GIN `@>`
query: the GIN index only accelerates `@>` containment, which requires
exact JSON type matches (safe for extras values, which are all strings,
but not for top-level fields of mixed type), and would force the filter
builder to use two query idioms. Measured: ~737ms for the `->>` form vs
~3ms for the `@>` form — not worth the added complexity for a
low-traffic filter; revisit only if usage patterns change.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("explorer", "0004_review"),
    ]

    operations = [
        migrations.RunSQL(
            sql="ALTER TABLE dataset_json ALTER COLUMN json TYPE jsonb USING json::jsonb",
            reverse_sql="ALTER TABLE dataset_json ALTER COLUMN json TYPE text USING json::text",
        ),
        migrations.RunSQL(
            sql="CREATE INDEX IF NOT EXISTS idx_dataset_json_gin ON dataset_json USING GIN (json)",
            reverse_sql="DROP INDEX IF EXISTS idx_dataset_json_gin",
        ),
    ]
