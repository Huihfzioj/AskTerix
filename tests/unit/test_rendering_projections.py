"""Projection tests: what each renderer deliberately includes and omits.

Built by hand rather than from a live database, since the dev database has no
views -- a round-trip or rendering test over an empty view list proves nothing.
"""

from app.introspection.cache import deserialize, serialize
from app.introspection.rendering import render_for_ask, render_for_migration
from app.introspection.schema_snapshot import (
    ColumnInfo,
    DatabaseSnapshot,
    ForeignKeyInfo,
    IndexInfo,
    TableInfo,
    ViewDependency,
    ViewInfo,
)


def column(name, type_, **kwargs):
    return ColumnInfo(
        name=name,
        type=type_,
        nullable=kwargs.get("nullable", True),
        default=kwargs.get("default"),
        is_primary_key=kwargs.get("is_primary_key", False),
    )


def snapshot_with_view():
    users = TableInfo(
        schema="public",
        name="users",
        columns=[
            column("id", "UUID", nullable=False, is_primary_key=True),
            column("email", "VARCHAR(255)", nullable=False),
            column("kind", "cloudtype", default="'AWS'"),
        ],
        foreign_keys=[
            ForeignKeyInfo(
                constrained_columns=["org_id"],
                referred_schema="public",
                referred_table="organizations",
                referred_columns=["id"],
            )
        ],
        indexes=[IndexInfo(name="ix_users_email", columns=["email"], unique=True)],
        approx_row_count=42,
    )
    view = ViewInfo(
        schema="public",
        name="v_users",
        is_materialized=False,
        columns=[column("email", "VARCHAR(255)")],
        definition="SELECT email FROM users WHERE is_active",
        depends_on=[
            ViewDependency(schema="public", name="users", kind="r", columns=["email"])
        ],
        approx_row_count=None,
    )
    return DatabaseSnapshot(
        tables=[users],
        schemas=["public"],
        enum_types={"cloudtype": ["AWS", "AZURE", "GCP"]},
        views=[view],
    )


def test_ask_omits_indexes():
    """Index names cannot help a model write a SELECT and are ~19% of a render."""
    rendered = render_for_ask(snapshot_with_view())
    assert "ix_users_email" not in rendered
    assert "INDEX" not in rendered


def test_migration_includes_indexes():
    """F2 needs them, or it proposes an index that already exists."""
    assert "INDEX ix_users_email: UNIQUE (email)" in render_for_migration(snapshot_with_view())


def test_ask_keeps_foreign_keys():
    """Dropping these would make joins uninferable."""
    assert "FK: (org_id) -> public.organizations(id)" in render_for_ask(snapshot_with_view())


def test_ask_omits_defaults_but_migration_keeps_them():
    assert "DEFAULT 'AWS'" not in render_for_ask(snapshot_with_view())
    assert "DEFAULT 'AWS'" in render_for_migration(snapshot_with_view())


def test_view_definitions_never_reach_the_ask_prompt():
    rendered = render_for_ask(snapshot_with_view())
    assert "WHERE is_active" not in rendered


def test_both_renderers_expose_view_columns():
    """A view is queryable, so F1 needs its columns even without the definition."""
    for rendered in (render_for_ask(snapshot_with_view()), render_for_migration(snapshot_with_view())):
        assert "VIEW public.v_users" in rendered
        assert "- email: VARCHAR(255)" in rendered


def test_plain_view_row_count_is_not_invented():
    assert "no stored rows" in render_for_ask(snapshot_with_view())


def test_migration_surfaces_the_blast_radius_of_a_table():
    """F2 must see that changing users breaks v_users."""
    rendered = render_for_migration(snapshot_with_view())
    assert "DEPENDED ON BY: public.v_users" in rendered
    assert "READS: public.users (email)" in rendered


def test_enum_legend_covers_types_reachable_only_through_views():
    view = ViewInfo(
        schema="public",
        name="v_kinds",
        is_materialized=False,
        columns=[column("kind", "cloudtype")],
        definition="SELECT kind FROM users",
        depends_on=[],
        approx_row_count=None,
    )
    snapshot = DatabaseSnapshot(
        tables=[],
        schemas=["public"],
        enum_types={"cloudtype": ["AWS", "AZURE", "GCP"]},
        views=[view],
    )
    assert "cloudtype: AWS | AZURE | GCP" in render_for_ask(snapshot)


def test_views_survive_the_cache_round_trip():
    """The failure mode that silently dropped enum_types: deserialize rebuilds
    fields by hand, so a new field is lost unless it is added there too."""
    original = snapshot_with_view()
    restored = deserialize(serialize(original))

    assert restored == original
    assert restored.views[0].depends_on[0].columns == ["email"]
    assert restored.views[0].definition == original.views[0].definition
