"""Introspection tests validated against the live catalog. Skipped when no database."""

import pytest
from sqlalchemy import text

from app.introspection.cache import deserialize, serialize
from app.introspection.rendering import render_for_ask

pytestmark = pytest.mark.integration


def catalog_relations(engine):
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT n.nspname, c.relname, c.reltuples::bigint
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind = 'r'
                  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
            """)
        ).fetchall()
    return {(r[0], r[1]): int(r[2]) for r in rows}


def test_snapshot_survives_cache_round_trip(snapshot):
    """Guards every field: dataclasses compare by value, so a dropped field fails here."""
    assert deserialize(serialize(snapshot)) == snapshot


def test_tables_match_catalog(snapshot, engine):
    expected = {key for key in catalog_relations(engine)}
    actual = {(t.schema, t.name) for t in snapshot.tables}
    assert actual == expected


def test_row_counts_match_catalog(snapshot, engine):
    expected = catalog_relations(engine)
    actual = {(t.schema, t.name): t.approx_row_count for t in snapshot.tables}
    assert actual == expected


def test_enum_labels_match_catalog(snapshot, engine):
    """Only enums actually referenced by a column should be collected."""
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT DISTINCT t.typname, e.enumlabel, e.enumsortorder
                FROM pg_attribute a
                JOIN pg_class c ON c.oid = a.attrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                JOIN pg_type t ON t.oid = a.atttypid
                JOIN pg_enum e ON e.enumtypid = t.oid
                WHERE c.relkind = 'r'
                  AND a.attnum > 0
                  AND NOT a.attisdropped
                  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
                ORDER BY t.typname, e.enumsortorder
            """)
        ).fetchall()

    expected = {}
    for typname, label, _ in rows:
        expected.setdefault(typname, []).append(label)

    assert snapshot.enum_types == expected


def test_enum_columns_render_their_type_name(snapshot):
    """The legend lookup keys off the rendered type, so the two must agree."""
    if not snapshot.enum_types:
        pytest.skip("no enum-typed columns in this database")

    referenced = {c.type for t in snapshot.tables for c in t.columns}
    assert set(snapshot.enum_types) <= referenced


def test_primary_keys_match_catalog(snapshot, engine):
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT n.nspname, c.relname, a.attname
                FROM pg_constraint con
                JOIN pg_class c ON c.oid = con.conrelid
                JOIN pg_namespace n ON n.oid = c.relnamespace
                CROSS JOIN LATERAL unnest(con.conkey) AS k(att)
                JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum = k.att
                WHERE con.contype = 'p'
                  AND n.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
            """)
        ).fetchall()

    expected = {}
    for schema, table, col in rows:
        expected.setdefault((schema, table), set()).add(col)

    actual = {
        (t.schema, t.name): {c.name for c in t.columns if c.is_primary_key}
        for t in snapshot.tables
    }
    actual = {k: v for k, v in actual.items() if v}

    assert actual == expected


def test_render_is_non_empty_and_lists_every_table(snapshot):
    rendered = render_for_ask(snapshot)
    for table in snapshot.tables:
        assert f"TABLE {table.qualified_name} " in rendered
