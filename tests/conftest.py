"""pytest config for the pipeline tests (tests/test_*.py for scripts/).

These are plain pytest: they never import Django and set a dummy
DATABASE_URL at import time to satisfy module-level guards they never
connect to. The .env load below runs first (conftest imports before any
test module), so Django settings — which pytest-django loads during the
run — never inherit the dummy URL from a pipeline module.

The Django app tests live in explorer/tests/ with their own conftest.
"""

import os

from dotenv import load_dotenv

# Load the real DATABASE_URL from .env *before* any test module imports.
# The pipeline test modules setdefault("DATABASE_URL", dummy) at import
# time; if that ran first, Django's settings (loaded later by
# pytest-django) would inherit the dummy URL. load_dotenv is a no-op for
# vars the shell already set — the same precedence settings.py has.
load_dotenv()

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
