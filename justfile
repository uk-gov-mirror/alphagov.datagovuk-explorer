# data.gov.uk Explorer — command shortcuts

# Load .env (DATABASE_URL etc.) into the recipe environment so commands like
# pg_dump/pg_restore can use it. Missing .env is fine — vars stay unset.
set dotenv-path := ".env"

# Run the dev server with auto-reload. whitenoise.runserver_nostatic means
# WhiteNoise serves /static/ through the middleware chain (Cache-Control
# max-age=0 in dev, see config/settings.py), so edited CSS/JS aren't cached.
# The BasicAuth gate therefore applies to static locally too.
dev:
    uv run --env-file .env python manage.py runserver 0.0.0.0:3000

# Run the server (production mode — no reload; WhiteNoise serves collectstatic
# output from staticfiles/)
start:
    uv run --env-file .env python manage.py collectstatic --noinput
    uv run --env-file .env gunicorn config.wsgi --bind 0.0.0.0:{{env_var_or_default("PORT", "3000")}}

# Lint: Run pre-commit checks without the commit (ruff, djlint, django-upgrade, ...)
lint *args:
    pre-commit run {{args}}

# Typecheck: Run mypy
# (Not in pre-commit/CI — run manually, like datagovuk.)
typecheck:
    uv run mypy .

# Format + lint fix
format:
    uv run ruff check --fix . && uv run ruff format .

# Run all unit tests
test:
    uv run pytest

# Install dependencies (first run)
setup:
    uv sync --dev

# Download the bge-base-en-v1.5 GGUF model into llm/ (needed for embeddings)
download-llm:
    uv run python -m scripts.download_llm

# Rebuild the PostgreSQL database from downloads/ (pass --skip-embeddings
# for a fully offline build; DATABASE_URL comes from .env)
build-db *args:
    uv run --env-file .env python -m scripts.build_db {{args}}

# Build series data from dataset titles (DATABASE_URL from .env)
build-series:
    uv run --env-file .env python -m scripts.build_series

# Embed dataset embeddings (needs llama-server up on :8080; DATABASE_URL from .env)
embed-only:
    uv run --env-file .env python -m scripts.embed_only

# Dump the local dev database (schema + all pipeline data) to db/backups/ —
# the one-shot path to replace the Railway Postgres contents (see restore-db).
# db/backups/ is gitignored.
dump-db dump_file="db/backups/explorer-`date +%F`.dump":
    @mkdir -p db/backups
    pg_dump "{{env_var_or_default('DATABASE_URL', 'postgresql://localhost:5432/datagovuk_explorer')}}" \
      --no-owner --no-privileges --format=custom --file="{{dump_file}}"
    @echo "Wrote {{dump_file}}"

# Restore a local dump into the Railway Postgres, replacing everything.
# The JS app and this app share the same table names, so the existing
# schema is dropped wholesale first — pg_restore --clean alone can't
# cascade through the FK dependencies (datasets ← links/dataset_json/
# embedding_map), which is why the drop is done up front. That IS the
# replacement. The dump recreates the vector extension afterwards.
#
# Needs a reachable URL for the Railway Postgres (its internal
# postgres.railway.internal host doesn't resolve off-Railway):
#   • tunnel:     railway connect Postgres --tunnel-only -P 5433
#                 (5432 is usually your local Postgres — pick a free port)
#   • public URL: Postgres → Connect → Public connection URL in the dashboard
# Pass it as the second arg, or set RAILWAY_DATABASE_URL.
restore-db dump_file railway_database_url=env_var_or_default('RAILWAY_DATABASE_URL', ''):
    @if [ -z "{{railway_database_url}}" ]; then \
        echo "Pass the Railway Postgres URL, e.g." >&2; \
        echo '  just restore-db explorer.dump postgresql://postgres:PASS@127.0.0.1:5433/railway' >&2; \
        exit 1; \
    fi
    @echo "Dropping existing schema in the target DB, then restoring {{dump_file}}"
    psql "{{railway_database_url}}" -c "DROP SCHEMA public CASCADE; CREATE SCHEMA public;"
    pg_restore --no-owner --no-privileges \
      --dbname="{{railway_database_url}}" "{{dump_file}}"

# Open an encrypted tunnel to the Railway Postgres. Keep this running in a
# terminal while you restore (Ctrl+C to close). 5433 — your local Postgres
# usually owns 5432.
tunnel:
    @echo "Opening tunnel to Railway Postgres on 127.0.0.1:5433 — Ctrl+C to close"
    railway connect Postgres --tunnel-only -P 5433

# Deploy the current directory to the Railway service, replacing the running
# app on the same URL. Run the dump → tunnel → restore-db dance first (the
# DB is the state; this just ships the code). Requires the dir to be linked:
#   railway link --project datagovuk-explorer --service datagovuk-explorer
# Stuck in DEPLOYING with empty logs? Check Railway's status page — they've
# had queueing incidents that clear on their own.
deploy:
    railway up -d -y

# Verify the deployed app is healthy (BASIC_AUTH_USER/PASS from .env).
deploy-check:
    curl -s -o /dev/null -w "health: %{http_code}\n" \
      -u "{{env_var_or_default('BASIC_AUTH_USER', 'datagovuk')}}:{{env_var_or_default('BASIC_AUTH_PASS', '')}}" \
      "https://datagovuk-explorer-production.up.railway.app/health"

# Fetch organisations from CKAN API. Writes downloads/organisations.json,
# which build-db loads.
fetch-organisations:
    uv run python -m scripts.fetch_organisations

# Fetch all harvest sources from CKAN API (walks orgs, per-org filter).
# Writes downloads/harvest_sources.json, which build-db loads.
fetch-harvest-sources:
    uv run python -m scripts.fetch_harvest_sources

# Audit for unused CSS with PurgeCSS (read-only: lists selectors that
# appear in no template/JS, never rewrites files). Requires Node/npx —
# the first run downloads purgecss. The --safelist entries are classes
# built dynamically in templates (score-{{ n }}, suggestion--{{ level }},
# badge-{{ org.state/approval_status }}), which a static scan always flags.
unused-css:
    npx -y purgecss \
      --css 'explorer/static/css/**/*.css' \
      --content 'explorer/templates/**/*.html' 'explorer/static/links.js' \
      --rejected \
      --safelist score-0 score-1 score-2 score-3 score-4 score-5 \
                 suggestion--low suggestion--med suggestion--high \
                 badge-active badge-approved badge-deleted badge-draft \
                 badge-rejected badge-pending \
      | python3 -c 'import json,sys; [print(x["file"].split("/")[-1] + ": " + (", ".join(r.strip() for r in x["rejected"] if r.strip()) or "clean")) for x in json.load(sys.stdin)]'

# Download datasets (pass args through)
download-datasets *args:
    uv run python -m scripts.download_datasets {{args}}

# Query datasets for one org
query-datasets *args:
    uv run python -m scripts.query_datasets {{args}}

# LLM review + suggest (loads .env for LLM/LOCAL_* vars — uv only loads
# .env via --env-file)
review-suggest *args:
    uv run --env-file .env python -m scripts.review_suggest {{args}}

# Load the review JSONL into the reviews table (run after review-suggest)
ingest-reviews:
    uv run --env-file .env python -m scripts.ingest_reviews
