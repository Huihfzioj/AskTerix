"""ADR-2 evidence: the read path is restricted by Postgres, not by application code.

Per spec 12/1.2, these confirm Postgres itself rejects the write -- so the tests
issue real write statements through the Ask role rather than asserting that our
code declines to issue them.

The `writable_session` tests are the load-bearing ones: they disable
default_transaction_read_only so a passing test proves the *grants* are the
boundary. Without that isolation every write fails with 25006 (read-only
transaction) and the absence of grants is never actually exercised.
"""

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from app.config import settings

pytestmark = pytest.mark.integration

READ_ONLY_TXN = "25006"
INSUFFICIENT_PRIVILEGE = "42501"


def pgcode(excinfo) -> str:
    return getattr(excinfo.value.orig, "pgcode", None)


def test_connects_as_the_ask_role(read_only_engine):
    with read_only_engine.connect() as conn:
        assert conn.execute(text("SELECT current_user")).scalar() == settings.ask_role_name


def test_session_defaults_are_applied(read_only_engine):
    with read_only_engine.connect() as conn:
        assert conn.execute(text("SHOW default_transaction_read_only")).scalar() == "on"
        assert (
            conn.execute(text("SHOW statement_timeout")).scalar()
            == settings.ask_statement_timeout
        )


def test_can_read_tables(read_only_engine):
    with read_only_engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM users")).scalar() >= 0


def test_can_read_the_catalog(read_only_engine):
    """Introspection runs through this role, so catalog access is required."""
    with read_only_engine.connect() as conn:
        count = conn.execute(
            text("SELECT count(*) FROM pg_class WHERE relnamespace = 'public'::regnamespace")
        ).scalar()
    assert count > 0


def test_can_read_views_and_materialized_views(read_only_engine):
    """GRANT SELECT ON ALL TABLES covers relkind 'v' and 'm' as well as 'r'."""
    with read_only_engine.connect() as conn:
        relations = conn.execute(
            text("""
                SELECT n.nspname, c.relname
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind IN ('v', 'm') AND n.nspname = 'public'
            """)
        ).fetchall()

        if not relations:
            pytest.skip("no views in this database")

        for schema, name in relations:
            conn.execute(text(f'SELECT count(*) FROM "{schema}"."{name}"')).scalar()


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO users (id, email) VALUES (gen_random_uuid(), 'x@example.com')",
        "UPDATE users SET email = 'x@example.com'",
        "DELETE FROM users",
        "CREATE TABLE should_not_exist (id int)",
        "DROP TABLE users",
        "ALTER TABLE users ADD COLUMN should_not_exist int",
        "TRUNCATE users",
    ],
)
def test_writes_are_refused(read_only_engine, statement):
    """The everyday path: read-only transactions reject writes outright."""
    with pytest.raises(DBAPIError) as excinfo:
        with read_only_engine.connect() as conn:
            conn.execute(text(statement))
            conn.commit()

    assert pgcode(excinfo) in (READ_ONLY_TXN, INSUFFICIENT_PRIVILEGE)


@pytest.mark.parametrize(
    "statement",
    [
        "INSERT INTO users (id, email) VALUES (gen_random_uuid(), 'x@example.com')",
        "UPDATE users SET email = 'x@example.com'",
        "DELETE FROM users",
        "CREATE TABLE should_not_exist (id int)",
        "ALTER TABLE users ADD COLUMN should_not_exist int",
        "TRUNCATE users",
    ],
)
def test_writes_are_refused_on_privileges_alone(writable_session, statement):
    """ADR-2 proper: with the read-only flag off, the missing grant is what blocks.

    A failure here means a write privilege was granted to the Ask role.
    """
    with pytest.raises(DBAPIError) as excinfo:
        writable_session.execute(text(statement))

    assert pgcode(excinfo) == INSUFFICIENT_PRIVILEGE


def test_drop_is_refused_on_ownership(writable_session):
    """DROP reports 42501 for a different reason -- ownership, not a grant."""
    with pytest.raises(DBAPIError) as excinfo:
        writable_session.execute(text("DROP TABLE users"))

    assert pgcode(excinfo) == INSUFFICIENT_PRIVILEGE
    assert "must be owner" in str(excinfo.value.orig)


def test_cannot_create_objects_in_public_schema(writable_session):
    """PostgreSQL 15+ removed CREATE from PUBLIC on the public schema; the
    provisioning script also REVOKEs it for older clusters."""
    with pytest.raises(DBAPIError) as excinfo:
        writable_session.execute(text("CREATE TABLE should_not_exist (id int)"))

    assert pgcode(excinfo) == INSUFFICIENT_PRIVILEGE
    assert "schema public" in str(excinfo.value.orig)


def test_no_write_attempt_left_anything_behind(read_only_engine):
    with read_only_engine.connect() as conn:
        leftovers = conn.execute(
            text("SELECT count(*) FROM pg_class WHERE relname = 'should_not_exist'")
        ).scalar()
        column = conn.execute(
            text("""
                SELECT count(*) FROM information_schema.columns
                WHERE table_name = 'users' AND column_name = 'should_not_exist'
            """)
        ).scalar()
    assert leftovers == 0
    assert column == 0
