"""Embedding statements — pgvector semantic search and the
dataset→embedding-text lookup behind the dataset detail page's
"semantically related" list."""

from .core import Query

# Dataset embedding text (the pgvector KNN query joins through
# embedding_map/rowid to reach dataset ids)
EMBEDDING_TEXT = Query("SELECT vector_text FROM embedding_map WHERE dataset_id = %s")

# Semantic "more like this" via pgvector KNN. The series exclusion (the
# NOT IN block) matches RELATED_BY_FTS: datasets in the same detected
# series as the current one are not "related".
SEMANTIC_RELATED = Query(
    """SELECT d.id, d.title, d.org_slug, d.org_display_name, d.theme_primary,
              emb.embedding <-> %s::vector AS distance
       FROM dataset_embeddings emb
       JOIN embedding_map m ON m.rowid = emb.rowid
       JOIN datasets d ON d.id = m.dataset_id
       WHERE m.dataset_id != %s
         AND d.id NOT IN (
           SELECT sd.dataset_id FROM series_datasets sd
           WHERE sd.series_id IN (
             SELECT sd2.series_id FROM series_datasets sd2 WHERE sd2.dataset_id = %s
           )
         )
       ORDER BY distance, d.id
       LIMIT 20""",
)
