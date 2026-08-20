r"""Unit + scratch-DB tests for scripts/db.py.

Covers:

- the SQLite-`?` → psycopg-`%s` translation (the exact seam that makes the
  pipeline SQL carry over unchanged — including escaped `\?` literals)
- database_url()'s fail-loud guard
- the connection layer against a throwaway scratch database (created and
  dropped per session): exec / prepare / get / all / run, transaction
  commit + rollback, dict rows, jsonb-as-parsed-object.

The scratch DB is skipped when the postgres user can't create databases
(CREATEDB privilege); the pure tests always run. These tests never import
Django and never touch the dev database.
"""

import os
import uuid

import psycopg
import pytest

from scripts import db


# ---------------------------------------------------------------------------
# Pure: SQL translation
# ---------------------------------------------------------------------------
def test_translate_basic():
    assert db._translate("SELECT ?") == "SELECT %s"
    assert db._translate("WHERE a = ? AND b = ?") == "WHERE a = %s AND b = %s"
    assert db._translate("no placeholders") == "no placeholders"


def test_translate_escaped_question_marks():
    r"""`\?` literals (URL regexes in the report SQL) stay untouched."""
    assert db._translate(r"url ~ '^https\?://'") == r"url ~ '^https\?://'"
    assert db._translate(r"a = ? AND b ~ '\?x'") == r"a = %s AND b ~ '\?x'"


def test_translate_mixed():
    assert db._translate(r"x = ? AND y ~ '\?' AND z = ?") == (r"x = %s AND y ~ '\?' AND z = %s")


def test_database_url_guard(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    with pytest.raises(SystemExit):
        db.database_url()


# ---------------------------------------------------------------------------
# Scratch-DB session fixture
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def scratch_db_url():
    """A throwaway postgres database, or pytest.skip if we can't create one.

    Created from the postgres maintenance DB (the usual local layout);
    dropped with (FORCE) at session end so leftover connections (a crashed
    earlier run) don't block the drop.
    """
    maintenance = os.getenv(
        "TEST_POSTGRES_MAINTENANCE_URL",
        "postgresql://localhost:5432/postgres",
    )
    name = f"explorer_scratch_{uuid.uuid4().hex[:12]}"
    try:
        admin = psycopg.connect(maintenance, autocommit=True)
    except psycopg.OperationalError as e:
        pytest.skip(f"cannot reach postgres maintenance DB ({maintenance}): {e}")
    try:
        with admin.cursor() as cur:
            cur.execute(f"CREATE DATABASE {name}")
    except psycopg.Error as e:
        admin.close()
        pytest.skip(f"cannot create scratch database (CREATEDB privilege?): {e}")
    admin.close()

    url = f"postgresql://localhost:5432/{name}"
    yield url

    try:
        admin = psycopg.connect(maintenance, autocommit=True)
        with admin.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS {name} WITH (FORCE)")
        admin.close()
    except psycopg.Error:
        pass  # best-effort cleanup — the name is unique per run


# ---------------------------------------------------------------------------
# Connection layer against the scratch DB
# ---------------------------------------------------------------------------
def test_exec_and_prepare(scratch_db_url):
    d = db.connect(scratch_db_url)
    try:
        d.exec(
            "CREATE TABLE t_exec (id serial primary key, name text, payload jsonb);"
            "CREATE TABLE t_exec_u (id serial primary key);",
        )
        stmt = d.prepare(
            "INSERT INTO t_exec (name, payload) VALUES (?, ?) RETURNING id",
        )
        row = stmt.get("first", '{"a": 1}')
        assert row == {"id": 1}

        got = d.prepare("SELECT * FROM t_exec WHERE name = ?").all("first")
        assert got == [{"id": 1, "name": "first", "payload": {"a": 1}}]
    finally:
        d.close()


def test_query_jsonb_default_loader(scratch_db_url):
    """Raw psycopg3's default jsonb loader returns parsed objects, not
    strings (Django's connection registers string loaders instead). The
    pipeline never reads jsonb columns today, but the behaviour is
    documented here so it can't silently drift."""
    d = db.connect(scratch_db_url)
    try:
        d.exec("CREATE TABLE t_jsonb (id serial primary key, payload jsonb)")
        stmt = d.prepare("INSERT INTO t_jsonb (payload) VALUES (?)")
        stmt.run('{"k": [1, 2, 3]}')
        stmt.run('{"k": 4}')

        rows = d.prepare("SELECT id, payload FROM t_jsonb ORDER BY id").all()
        assert rows == [
            {"id": 1, "payload": {"k": [1, 2, 3]}},
            {"id": 2, "payload": {"k": 4}},
        ]
        assert isinstance(rows[0]["payload"], dict)
    finally:
        d.close()


def test_transaction_commit(scratch_db_url):
    d = db.connect(scratch_db_url)
    try:
        d.exec("CREATE TABLE t_commit (id int)")
        result = d.transaction(
            lambda tx: tx.prepare("INSERT INTO t_commit VALUES (?) RETURNING id").get(7),
        )
        assert result == {"id": 7}
        # autocommit connection — the committed row is visible
        assert d.prepare("SELECT COUNT(*) AS n FROM t_commit").get() == {"n": 1}
    finally:
        d.close()


def test_transaction_rollback(scratch_db_url):
    d = db.connect(scratch_db_url)
    try:
        d.exec("CREATE TABLE t_rollback (id int)")

        class BoomError(RuntimeError):
            pass

        def _boom(tx):
            tx.prepare("INSERT INTO t_rollback VALUES (?)").run(1)
            raise BoomError("boom")

        with pytest.raises(BoomError):
            d.transaction(_boom)
        assert d.prepare("SELECT COUNT(*) AS n FROM t_rollback").get() == {"n": 0}
    finally:
        d.close()
