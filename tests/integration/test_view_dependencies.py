"""View dependency resolution, verified against views created in a rolled-back
transaction. Nothing is ever committed, so a crashed test leaves no DDL behind."""

import pytest
from sqlalchemy import text

from app.introspection.schema_snapshot import (
    find_affected_views,
    get_view_dependencies,
    introspect_views,
)

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


def test_dependencies_are_returned_in_sorted_order(deps):
    """Asserts the ordering property itself. Comparing two consecutive runs would
    pass by luck on an unordered query; this cannot. Snapshots are cached and
    compared by value, so unstable ordering makes an unchanged schema look changed."""
    for dependencies in deps.values():
        keys = [(d.schema, d.name) for d in dependencies]
        assert keys == sorted(keys)


def test_direct_column_dependants_are_found(deps):
    """The F3 question this data exists to answer: who breaks if users.email goes?"""
    affected = find_affected_views(deps, "public", "users", column="email")
    assert ("public", "v_simple") in affected
    assert ("public", "v_join") in affected


def test_column_drop_propagates_through_a_view_chain(deps):
    """v_on_view reads users.email only through v_simple. Dropping the column
    requires CASCADE, which drops v_simple and therefore v_on_view too."""
    affected = find_affected_views(deps, "public", "users", column="email")
    assert ("public", "v_on_view") in affected


def test_column_drop_spares_whole_relation_dependants(deps):
    """v_agg is SELECT count(*) FROM organizations -- it reads no column, so
    dropping organizations.name does not break it."""
    affected = find_affected_views(deps, "public", "organizations", column="name")
    assert ("public", "v_agg") not in affected
    assert ("public", "v_join") in affected


def test_relation_drop_includes_whole_relation_dependants(deps):
    """Dropping the table itself does break the aggregate view."""
    affected = find_affected_views(deps, "public", "organizations")
    assert ("public", "v_agg") in affected
    assert ("public", "v_join") in affected


def test_unaffected_relation_yields_nothing(deps):
    assert find_affected_views(deps, "public", "users", column="password_hash") == set()
    assert find_affected_views(deps, "public", "pipeline_runs") == set()


@pytest.fixture
def view_infos(views):
    return {(v.schema, v.name): v for v in introspect_views(views)}


def test_every_view_is_introspected(view_infos):
    assert ALL_VIEWS <= view_infos.keys()


def test_view_columns_are_populated(view_infos):
    """F1 generates SQL against views, so it needs their columns and types."""
    columns = {c.name: c.type for c in view_infos[("public", "v_simple")].columns}
    assert columns == {"id": "UUID", "email": "VARCHAR(255)"}


def test_view_definition_is_populated(view_infos):
    definition = view_infos[("public", "v_simple")].definition
    assert "SELECT" in definition.upper()
    assert "users" in definition


def test_materialized_flag_distinguishes_view_kinds(view_infos):
    assert view_infos[("public", "mv_accounts")].is_materialized is True
    assert view_infos[("public", "v_simple")].is_materialized is False


def test_plain_views_have_no_row_count(view_infos):
    """A plain view stores no rows, so a count would be an invented number."""
    assert view_infos[("public", "v_simple")].approx_row_count is None
    assert view_infos[("public", "mv_accounts")].approx_row_count is not None


def test_view_info_carries_its_dependencies(view_infos):
    sources = {d.qualified_name for d in view_infos[("public", "v_join")].depends_on}
    assert sources == {"public.users", "public.organizations"}
