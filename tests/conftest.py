import pytest
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from app.db.engines import ask_engine, introspection_engine
from app.introspection.schema_snapshot import introspect_database


@pytest.fixture(scope="session")
def engine():
    """The introspection engine, skipping the test if the database is unreachable."""
    try:
        with introspection_engine.connect():
            pass
    except SQLAlchemyError as exc:
        pytest.skip(f"introspection database unavailable: {exc}")
    return introspection_engine


@pytest.fixture(scope="session")
def snapshot(engine):
    """A single snapshot shared across the integration tests."""
    return introspect_database(engine)


@pytest.fixture(scope="session")
def read_only_engine():
    """The Ask-mode engine, skipping if the read-only role is not provisioned."""
    if ask_engine is None:
        pytest.skip("ask_db_url is not set -- run scripts/provision_roles.py")
    try:
        with ask_engine.connect():
            pass
    except SQLAlchemyError as exc:
        pytest.skip(
            f"read-only role unavailable ({exc}) -- run scripts/provision_roles.py"
        )
    return ask_engine


@pytest.fixture
def writable_session(read_only_engine):
    """A connection with default_transaction_read_only turned off at session level.

    Isolates the grants from the read-only flag: any write that still fails here
    is refused because the role lacks the privilege, not because the transaction
    is read-only. AUTOCOMMIT matters -- setting it inside an open transaction is
    a silent no-op, which makes the flag look like the boundary when it isn't.
    """
    engine = read_only_engine.execution_options(isolation_level="AUTOCOMMIT")
    with engine.connect() as conn:
        conn.execute(text("SET default_transaction_read_only = off"))
        assert conn.execute(text("SHOW default_transaction_read_only")).scalar() == "off"
        yield conn
