"""Harvest sources table + the datasets join key.

Owns the harvest_sources table that scripts/build_db.py populates from
downloads/harvest_sources.json (the fetch_harvest_sources.py output, the
gitignored API cache alongside the dataset files). Mirrors the
organisations table: a few queryable scalars plus the full record in a
json text column, keyed by the CKAN source id — the same id datasets
carry in their harvest_source_id extra, which is why the second
operation adds that column to datasets: it is the datasets→sources join
key (titles aren't unique across sources, so title joins overcount).

Promoted query fields beyond the API's own columns:
- created: the API's creation timestamp — the /harvesters page sorts on
  it, so it lives as a column (text, like every timestamp in this
  schema) instead of json extraction.
- org_slug: the owning organisation's slug (the organisations table
  primary key), denormalised at build time from the CKAN UUID the API
  returns in organization_id — so /harvesters joins organisations on
  its primary key instead of the org record's json blob. organization_id
  keeps the raw UUID for provenance.

organization_id is the CKAN organisation UUID the source was fetched
under (fetch_harvest_sources.py tags each record with it because the
API's own publisher_id/publisher_title are often empty). It is a plain
text column — the organisations table is keyed by slug, not UUID, so no
FK is possible; org_slug is the join key instead.

Both halves of the harvesters feature shipped together on the unmerged
feature branch, so they're one migration (squashed before merge — no
environment outside the dev machine had applied either).
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ("explorer", "0006_temporal_periods_jsonb"),
    ]

    operations = [
        migrations.CreateModel(
            name="HarvestSource",
            fields=[
                ("id", models.TextField(primary_key=True, serialize=False)),
                ("title", models.TextField(blank=True, null=True)),
                ("url", models.TextField(blank=True, null=True)),
                ("type", models.TextField(blank=True, null=True)),
                ("active", models.BooleanField(blank=True, null=True)),
                ("frequency", models.TextField(blank=True, null=True)),
                ("organization_id", models.TextField(blank=True, null=True)),
                ("org_slug", models.TextField(blank=True, null=True)),
                ("created", models.TextField(blank=True, null=True)),
                ("json", models.TextField(blank=True, null=True)),
            ],
            options={
                "db_table": "harvest_sources",
            },
        ),
        migrations.AddField(
            model_name="dataset",
            name="harvest_source_id",
            field=models.TextField(blank=True, null=True),
        ),
    ]
