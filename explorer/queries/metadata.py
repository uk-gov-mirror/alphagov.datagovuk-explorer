"""Metadata statements — the field-adoption overview (/metadata) and
the paginated value distribution for one field."""

from .core import Query

# Metadata field keys, ordered by section then adoption
METADATA_KEYS = Query(
    """SELECT key, section, count, non_empty, distinct_values
       FROM metadata_keys
       ORDER BY section, non_empty DESC, count DESC""",
)

# Count of unique values for one metadata field
METADATA_VALUE_COUNT = Query("SELECT COUNT(*) AS n FROM metadata_values WHERE key = %s")

# Top N values for one metadata field. The `, value` tiebreak pins
# case-variant values that share LOWER(value), so a tie straddling a page
# boundary doesn't shuffle between requests.
METADATA_VALUES = Query(
    """SELECT value, count
       FROM metadata_values
       WHERE key = %s
       ORDER BY count DESC, LOWER(value), value
       LIMIT %s OFFSET %s""",
)
