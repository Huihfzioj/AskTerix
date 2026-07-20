"""Rendering tests. These build snapshots by hand and never touch a database."""

from app.introspection.rendering import render_for_ask, render_for_migration
from app.introspection.schema_snapshot import (
    ColumnInfo,
    DatabaseSnapshot,
    IndexInfo,
    TableInfo,
)


def make_table(name, columns, **kwargs):
    return TableInfo(
        schema="public",
        name=name,
        columns=columns,
        foreign_keys=kwargs.get("foreign_keys", []),
        indexes=kwargs.get("indexes", []),
        approx_row_count=kwargs.get("approx_row_count", 0),
    )


def column(name, type_, **kwargs):
    return ColumnInfo(
        name=name,
        type=type_,
        nullable=kwargs.get("nullable", True),
        default=kwargs.get("default"),
        is_primary_key=kwargs.get("is_primary_key", False),
    )


def test_negative_row_count_renders_as_unknown():
    snapshot = DatabaseSnapshot(
        tables=[make_table("t", [column("id", "UUID")], approx_row_count=-1)],
        schemas=["public"],
    )
    rendered = render_for_migration(snapshot)

    assert "row count unknown" in rendered
    assert "-1" not in rendered


def test_known_row_count_renders_with_thousands_separator():
    snapshot = DatabaseSnapshot(
        tables=[make_table("t", [column("id", "UUID")], approx_row_count=2198)],
        schemas=["public"],
    )
    assert "(~2,198 rows)" in render_for_migration(snapshot)


def test_zero_row_count_is_not_reported_as_unknown():
    snapshot = DatabaseSnapshot(
        tables=[make_table("t", [column("id", "UUID")], approx_row_count=0)],
        schemas=["public"],
    )
    rendered = render_for_migration(snapshot)

    assert "(~0 rows)" in rendered
    assert "unknown" not in rendered


def test_enum_labels_appear_once_regardless_of_column_count():
    columns = [column(f"c{i}", "cloudtype") for i in range(5)]
    snapshot = DatabaseSnapshot(
        tables=[make_table("t", columns)],
        schemas=["public"],
        enum_types={"cloudtype": ["AWS", "AZURE", "GCP"]},
    )
    rendered = render_for_migration(snapshot)

    assert rendered.count("AWS | AZURE | GCP") == 1
    assert rendered.count("cloudtype") == 6  # one legend entry, five column references


def test_legend_omits_enums_not_used_by_filtered_tables():
    snapshot = DatabaseSnapshot(
        tables=[
            make_table("uses_enum", [column("kind", "cloudtype")]),
            make_table("plain", [column("name", "VARCHAR(10)")]),
        ],
        schemas=["public"],
        enum_types={
            "cloudtype": ["AWS", "AZURE", "GCP"],
            "alerttype": ["EMAIL", "WEBHOOK"],
        },
    )

    rendered = render_for_migration(snapshot, table_filter=["public.uses_enum"])
    assert "cloudtype: AWS | AZURE | GCP" in rendered
    assert "alerttype" not in rendered

    rendered = render_for_migration(snapshot, table_filter=["public.plain"])
    assert "ENUMS:" not in rendered


def test_column_markers_are_rendered():
    snapshot = DatabaseSnapshot(
        tables=[
            make_table(
                "t",
                [
                    column("id", "UUID", nullable=False, is_primary_key=True),
                    column("pct", "DOUBLE PRECISION", default="80.0"),
                ],
                indexes=[IndexInfo(name="uq_t", columns=["id"], unique=True)],
            )
        ],
        schemas=["public"],
    )
    rendered = render_for_migration(snapshot)

    assert "- id: UUID [PK] NOT NULL" in rendered
    assert "- pct: DOUBLE PRECISION DEFAULT 80.0" in rendered
    assert "INDEX uq_t: UNIQUE (id)" in rendered
