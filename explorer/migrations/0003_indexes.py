"""Post-load indexes.

Ownership of the 13 pipeline-created indexes moves from the build scripts
to migrations, completing the "migrations own the schema" decision:

- the 10 idx_* statements build_db.py ran after the bulk load
  (idx_datasets_org, idx_links_host/format/year/dataset/org,
  idx_datasets_org_resource/theme/created/modified)
- idx_datasets_fts (GIN) that build_db.py ran after populating datasets.fts
- idx_series_datasets_series / idx_series_datasets_dataset that
  build_series.py's SERIES_DDL created (the series tables themselves are
  owned by 0001's Series/SeriesDataset models)

CREATE INDEX IF NOT EXISTS keeps migrate a no-op on the baseline DB where
the pipeline already created them (same names + definitions, byte
compatible), and lets a fresh-DB build skip index creation entirely — the
one accepted cost is that a fresh build loads into indexed tables.

Build scripts now TRUNCATE + populate (never create), so the indexes
survive rebuilds.
"""

from django.db import migrations

INDEXES = [
    "CREATE INDEX IF NOT EXISTS idx_datasets_org          ON datasets(org_slug)",
    "CREATE INDEX IF NOT EXISTS idx_links_host            ON links(host)",
    "CREATE INDEX IF NOT EXISTS idx_links_format          ON links(format_norm)",
    "CREATE INDEX IF NOT EXISTS idx_links_year            ON links(year_created)",
    "CREATE INDEX IF NOT EXISTS idx_links_dataset         ON links(dataset_id)",
    "CREATE INDEX IF NOT EXISTS idx_links_org             ON links(org_slug)",
    "CREATE INDEX IF NOT EXISTS idx_datasets_org_resource ON datasets(org_slug, resource_count)",
    "CREATE INDEX IF NOT EXISTS idx_datasets_theme        ON datasets(theme_primary)",
    "CREATE INDEX IF NOT EXISTS idx_datasets_created      ON datasets(metadata_created)",
    "CREATE INDEX IF NOT EXISTS idx_datasets_modified     ON datasets(metadata_modified)",
    "CREATE INDEX IF NOT EXISTS idx_datasets_fts          ON datasets USING GIN(fts)",
    "CREATE INDEX IF NOT EXISTS idx_series_datasets_series  ON series_datasets(series_id)",
    "CREATE INDEX IF NOT EXISTS idx_series_datasets_dataset ON series_datasets(dataset_id)",
]


class Migration(migrations.Migration):
    dependencies = [
        # fts (tsvector) is added by 0002; the series tables come from 0001.
        ("explorer", "0002_postgres_columns"),
    ]

    operations = [
        migrations.RunSQL(sql=INDEXES, reverse_sql=migrations.RunSQL.noop),
    ]
