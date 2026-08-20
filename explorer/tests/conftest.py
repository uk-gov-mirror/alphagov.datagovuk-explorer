"""pytest config for the explorer app tests — live DB, no test database.

test_views.py and test_queries.py exercise the Django views + query layer
against the *live*, populated database. pytest-django's defaults don't fit
that: it blocks DB access in unmarked tests and creates an empty test
database. Neither works here — the data is loaded by the standalone
pipeline (scripts/), not by migrations, so a pytest-created database can
never hold the baselines. The blocker is unblocked below and the tests read
the dev DB read-only, skipping cleanly when it's unreachable or empty.

The pipeline tests live in tests/ (with their own conftest); the two
suites are independent.
"""

import os

import pytest
from dotenv import load_dotenv

# Load the real DATABASE_URL from .env *before* pytest-django imports the
# Django settings (DJANGO_SETTINGS_MODULE comes from pyproject.toml).
# load_dotenv is a no-op for vars the shell already set — the same
# precedence settings.py has.
load_dotenv()

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")


@pytest.fixture(scope="session", autouse=True)
def _allow_live_db(django_db_blocker):
    """Let the app tests read the live dev DB (no test database, no block).

    pytest-django's default would create an empty test database and raise
    DatabaseAccessDenied for DB access from unmarked tests. App tests read
    the live DB directly, so this suite does the same.
    """
    django_db_blocker.unblock()


@pytest.fixture(scope="session")
def db_ready():
    """The live DB is reachable and populated — skip app tests otherwise.

    App tests are meaningless without the pipeline's data, and the DB may
    legitimately be down (or a fresh checkout may not have run the build
    yet). Skip loudly instead of failing with a connection error.
    """
    from django.db import connection  # noqa: PLC0415 — lazy: only when a fixture runs against the live DB

    try:
        with connection.cursor() as cur:
            cur.execute("SELECT count(*) FROM datasets")
            datasets = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM organisations")
            orgs = cur.fetchone()[0]
            cur.execute("SELECT count(*) FROM reviews")
            reviews = cur.fetchone()[0]
    except Exception as e:  # noqa: BLE001 — connection refused / missing DB
        pytest.skip(f"live database unavailable: {e}")
    if not datasets or not orgs:
        pytest.skip("live database is empty — run the pipeline first (just build-db)")
    return {"datasets": datasets, "orgs": orgs, "reviews": reviews}


@pytest.fixture
def client(db_ready):
    """Django test client for app tests (live DB, no test database)."""
    from django.test import Client  # noqa: PLC0415 — lazy: only when a fixture runs against the live DB

    return Client()
