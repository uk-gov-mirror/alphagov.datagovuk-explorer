"""Django models — the tables the migrations own.

Schema notes:
- Timestamps are TEXT in the DB (format_date exists for a reason) — TextField,
  not DateTimeField.
- links.id / series.id are SERIAL -> AutoField.
- embedding_map.rowid / dataset_embeddings.rowid are plain INTEGER PRIMARY KEY
  (embed_batch assigns dense rowids from 1) -> IntegerField(primary_key=True),
  not AutoField.
- datasets.fts (tsvector) and dataset_embeddings.embedding (vector(768)) are
  deliberately NOT here — they're added by a RunSQL migration (0002) that
  creates the vector extension first.
- Indexes are migration-owned (0003: the 10 idx_* + GIN) — FK fields are
  db_index=False so Django doesn't emit its own.
- metadata_values and series_datasets use Django 6 composite primary keys
  (pk = CompositePrimaryKey(...)) — no implicit id column.

The query layer is raw SQL via django.db.connection; these models exist to
own the schema via migrations.
"""

from django.db import models


class Organisation(models.Model):
    slug = models.TextField(primary_key=True)
    name = models.TextField(blank=True, null=True)
    display_name = models.TextField(blank=True, null=True)
    package_count = models.IntegerField(blank=True, null=True)
    type = models.TextField(blank=True, null=True)
    state = models.TextField(blank=True, null=True)
    approval_status = models.TextField(blank=True, null=True)
    created = models.TextField(blank=True, null=True)
    title = models.TextField(blank=True, null=True)
    json = models.TextField(blank=True, null=True)

    class Meta:
        app_label = "explorer"
        db_table = "organisations"

    def __str__(self):
        return self.slug


class Dataset(models.Model):
    id = models.TextField(primary_key=True)
    org_slug = models.TextField()
    org_display_name = models.TextField(blank=True, null=True)
    title = models.TextField(blank=True, null=True)
    name = models.TextField(blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    metadata_created = models.TextField(blank=True, null=True)
    metadata_modified = models.TextField(blank=True, null=True)
    resource_count = models.IntegerField(blank=True, null=True)
    theme_primary = models.TextField(blank=True, null=True)
    temporal_coverage_from = models.TextField(blank=True, null=True)
    temporal_coverage_to = models.TextField(blank=True, null=True)
    temporal_granularity = models.TextField(blank=True, null=True)
    temporal_periods = models.JSONField(blank=True, null=True)
    harvested = models.IntegerField(db_default=0)
    harvest_source_title = models.TextField(blank=True, null=True)
    views = models.IntegerField(db_default=0)
    tags = models.TextField(blank=True, null=True)

    class Meta:
        app_label = "explorer"
        db_table = "datasets"

    def __str__(self):
        return self.title or self.name or self.id


class DatasetJson(models.Model):
    dataset = models.OneToOneField(
        Dataset,
        on_delete=models.CASCADE,
        primary_key=True,
        db_column="id",
        db_index=False,
    )
    json = models.JSONField()

    class Meta:
        app_label = "explorer"
        db_table = "dataset_json"

    def __str__(self):
        return str(self.dataset)


class Link(models.Model):
    id = models.AutoField(primary_key=True)
    resource_id = models.TextField(blank=True, null=True)
    dataset = models.ForeignKey(
        Dataset,
        on_delete=models.CASCADE,
        db_column="dataset_id",
        db_index=False,
    )
    org_slug = models.TextField()
    org_display_name = models.TextField(blank=True, null=True)
    dataset_title = models.TextField(blank=True, null=True)
    name = models.TextField(blank=True, null=True)
    description = models.TextField(blank=True, null=True)
    url = models.TextField(blank=True, null=True)
    host = models.TextField(blank=True, null=True)
    format = models.TextField(blank=True, null=True)
    format_norm = models.TextField(blank=True, null=True)
    year_created = models.TextField(blank=True, null=True)
    created = models.TextField(blank=True, null=True)
    position = models.IntegerField(blank=True, null=True)

    class Meta:
        app_label = "explorer"
        db_table = "links"

    def __str__(self):
        return self.name or self.dataset_title or self.resource_id


class MetadataKey(models.Model):
    key = models.TextField(primary_key=True)
    section = models.TextField()
    count = models.IntegerField()
    non_empty = models.IntegerField()
    distinct_values = models.IntegerField()

    class Meta:
        app_label = "explorer"
        db_table = "metadata_keys"

    def __str__(self):
        return self.key


class MetadataValue(models.Model):
    metadata_key = models.ForeignKey(
        MetadataKey,
        on_delete=models.CASCADE,
        db_column="key",
        db_index=False,
    )
    value = models.TextField()
    count = models.IntegerField()
    pk = models.CompositePrimaryKey("metadata_key", "value")

    class Meta:
        app_label = "explorer"
        db_table = "metadata_values"

    def __str__(self):
        return self.value


class EmbeddingMap(models.Model):
    rowid = models.IntegerField(primary_key=True)
    dataset = models.ForeignKey(
        Dataset,
        on_delete=models.CASCADE,
        db_column="dataset_id",
        db_index=False,
    )
    vector_text = models.TextField()

    class Meta:
        app_label = "explorer"
        db_table = "embedding_map"

    def __str__(self):
        return str(self.dataset)


class DatasetEmbedding(models.Model):
    rowid = models.IntegerField(primary_key=True)

    class Meta:
        app_label = "explorer"
        db_table = "dataset_embeddings"

    def __str__(self):
        return str(self.rowid)


class Series(models.Model):
    id = models.AutoField(primary_key=True)
    root_title = models.TextField()
    type = models.TextField()
    dataset_count = models.IntegerField(db_default=0)
    org_count = models.IntegerField(db_default=0)

    class Meta:
        app_label = "explorer"
        db_table = "series"
        # Django Meta option, mutable by design — RUF012's class-attribute
        # default guard doesn't apply to Meta.
        constraints = [
            models.CheckConstraint(
                condition=models.Q(type__in=("template", "timeseries")),
                name="series_type_check",
            ),
        ]

    def __str__(self):
        return self.root_title


class SeriesDataset(models.Model):
    series = models.ForeignKey(
        Series,
        on_delete=models.CASCADE,
        db_column="series_id",
        db_index=False,
    )
    dataset_id = models.TextField()
    dataset_title = models.TextField()
    date_suffix = models.TextField(blank=True, null=True)
    org_slug = models.TextField()
    org_display_name = models.TextField()
    pk = models.CompositePrimaryKey("series", "dataset_id")

    class Meta:
        app_label = "explorer"
        db_table = "series_datasets"

    def __str__(self):
        return self.dataset_title


class Review(models.Model):
    """One row per LLM review record.

    The typed columns mirror the record's suggestion fields; `json` holds the
    full JSONL record verbatim and is what the views read
    (via explorer/queries), so the dict shape the templates expect is
    preserved byte-for-byte. Timestamps are TEXT like every other table in
    this schema (reviewed_at from the record).
    """

    id = models.AutoField(primary_key=True)
    dataset = models.ForeignKey(
        Dataset,
        on_delete=models.CASCADE,
        db_column="dataset_id",
        db_index=True,
    )
    ok = models.BooleanField(db_default=True)
    overall = models.IntegerField(blank=True, null=True)
    findability = models.IntegerField(blank=True, null=True)
    metadata = models.IntegerField(blank=True, null=True)
    resources = models.IntegerField(blank=True, null=True)
    theme = models.TextField(blank=True, null=True)
    tags = models.TextField(blank=True, null=True)
    title = models.TextField(blank=True, null=True)
    desc = models.TextField(blank=True, null=True)
    theme_confidence = models.TextField(blank=True, null=True)
    created_at = models.TextField(blank=True, null=True)
    json = models.TextField()

    class Meta:
        app_label = "explorer"
        db_table = "reviews"

    def __str__(self):
        return self.title or str(self.dataset)
