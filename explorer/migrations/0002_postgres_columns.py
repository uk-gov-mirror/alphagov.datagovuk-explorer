"""Postgres-only columns: datasets.fts (tsvector) and
dataset_embeddings.embedding (vector(768)).

Django has no native field type for either; the vector extension must exist
before the embedding column is added, so the extension and columns live in
this migration rather than 0001. fts is populated by the build pipeline
(build_db management command) and queried by relatedByFts.
"""

from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("explorer", "0001_initial"),
    ]

    operations = [
        migrations.RunSQL(
            sql="CREATE EXTENSION IF NOT EXISTS vector",
            reverse_sql=migrations.RunSQL.noop,
        ),
        migrations.RunSQL(
            sql="ALTER TABLE datasets ADD COLUMN fts tsvector",
            reverse_sql="ALTER TABLE datasets DROP COLUMN fts",
        ),
        migrations.RunSQL(
            sql="ALTER TABLE dataset_embeddings ADD COLUMN embedding vector(768)",
            reverse_sql="ALTER TABLE dataset_embeddings DROP COLUMN embedding",
        ),
    ]
