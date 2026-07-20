import pytest
from sqlalchemy.exc import SQLAlchemyError

from app.db.engines import introspection_engine
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
