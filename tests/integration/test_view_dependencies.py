"""View dependency resolution, verified against views created in a rolled-back
transaction. Nothing is ever committed, so a crashed test leaves no DDL behind."""

import pytest
from sqlalchemy import text

from app.introspection.schema_snapshot import get_view_dependencies

pytestmark = pytest.mark.integration

SETUP = [
    # ordinary view selecting specific columns
    "CREATE VIEW v_simple AS SELECT id, email FROM users",
    # aggregate-only view: depends on the relation, but on no specific column
    "CREATE VIEW v_agg AS SELECT count(*) AS n FROM organizations",
    # view built on another view rather than on a table
    "CREATE VIEW v_on_view AS SELECT email FROM v_simple",
    # materialized view over a table
    "CREATE MATERIALIZED VIEW mv_accounts AS SELECT id, alias FROM cloud_accounts",
    # view joining two tables
    "CREATE VIEW v_join AS SELECT u.email, o.name FROM users u JOIN organizations o ON true",
]

ALL_VIEWS = {
    ("public", "v_simple"),
    ("public", "v_agg"),
    ("public", "v_on_view"),
    ("public", "mv_accounts"),
    ("public", "v_join"),
}


@pytest.fixture
def views(engine):
    """Creates the five view shapes, then rolls back."""
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            for statement in SETUP:
                conn.execute(text(statement))
            yield conn
        finally:
            trans.rollback()


@pytest.fixture
def deps(views):
    return get_view_dependencies(views)


def sources(deps, view_name):
    return {d.qualified_name for d in deps[("public", view_name)]}


def columns_for(deps, view_name, source):
    for dependency in deps[("public", view_name)]:
        if dependency.qualified_name == source:
            return set(dependency.columns)
    raise AssertionError(f"{view_name} has no dependency on {source}")


def test_every_view_shape_is_found(deps):
    """The regression this exists for: two shapes were silently dropped."""
    assert ALL_VIEWS <= deps.keys()


def test_aggregate_only_view_is_not_dropped(deps):
    """SELECT count(*) records a relation-level dependency with refobjsubid = 0.

    Filtering those out made the view invisible, so a column/table drop would be
    reported as safe when it is not.
    """
    assert sources(deps, "v_agg") == {"public.organizations"}
    assert columns_for(deps, "v_agg", "public.organizations") == set()


def test_view_built_on_another_view_is_found(deps):
    """Restricting sources to relkind 'r' hid view-on-view chains entirely."""
    assert sources(deps, "v_on_view") == {"public.v_simple"}
    assert columns_for(deps, "v_on_view", "public.v_simple") == {"email"}


def test_column_level_dependencies_are_retained(deps):
    assert columns_for(deps, "v_simple", "public.users") == {"id", "email"}


def test_join_records_every_source_relation(deps):
    assert sources(deps, "v_join") == {"public.users", "public.organizations"}
    assert columns_for(deps, "v_join", "public.users") == {"email"}
    assert columns_for(deps, "v_join", "public.organizations") == {"name"}


def test_materialized_view_is_found_with_its_kind(deps):
    assert sources(deps, "mv_accounts") == {"public.cloud_accounts"}
    assert columns_for(deps, "mv_accounts", "public.cloud_accounts") == {"id", "alias"}


def test_source_kind_is_reported(deps):
    (table_dep,) = deps[("public", "v_simple")]
    assert table_dep.kind == "r"

    (view_dep,) = deps[("public", "v_on_view")]
    assert view_dep.kind == "v"


def test_a_view_never_depends_on_itself(deps):
    for (schema, name), dependencies in deps.items():
        assert (schema, name) not in {(d.schema, d.name) for d in dependencies}


def test_result_ordering_is_deterministic(views):
    """Snapshots are cached and compared by value, so unstable ordering would
    make an unchanged schema look changed."""
    first = get_view_dependencies(views)
    second = get_view_dependencies(views)
    assert first == second


def test_dropping_a_column_can_be_traced_to_affected_views(deps):
    """The F3 query this data exists to answer: who breaks if users.email goes?"""
    affected = {
        view
        for view, dependencies in deps.items()
        for d in dependencies
        if d.qualified_name == "public.users" and "email" in d.columns
    }
    assert ("public", "v_simple") in affected
    assert ("public", "v_join") in affected
    # v_on_view reads users.email only through v_simple, so it is not a direct
    # dependant -- reaching it requires walking the chain transitively.
    assert ("public", "v_on_view") not in affected
