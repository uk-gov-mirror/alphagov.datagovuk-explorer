"""datasets.temporal_periods: TEXT -> jsonb. Every row is valid JSON
already (confirmed against the live data before this migration was
written) — no data cleanup needed. No index added here: the per-year
facet aggregate in explorer/queries/datasets.py unrolls the array with
jsonb_array_elements, which a plain GIN index on the column doesn't
accelerate — leave indexing as a separate decision, not bundled here."""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("explorer", "0005_dataset_json_jsonb"),
    ]

    operations = [
        migrations.RunSQL(
            sql="ALTER TABLE datasets ALTER COLUMN temporal_periods TYPE jsonb USING temporal_periods::jsonb",
            reverse_sql="ALTER TABLE datasets ALTER COLUMN temporal_periods TYPE text USING temporal_periods::text",
        ),
    ]
