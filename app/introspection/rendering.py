from app.introspection.schema_snapshot import DatabaseSnapshot, TableInfo, ViewInfo


def _row_count(count: int | None) -> str:
    if count is None:
        return "no stored rows"
    if count < 0:
        return "row count unknown"
    return f"~{count:,} rows"


def _column_line(col, *, include_default: bool) -> str:
    pk_marker = " [PK]" if col.is_primary_key else ""
    null_marker = "" if col.nullable else " NOT NULL"
    default_marker = f" DEFAULT {col.default}" if include_default and col.default else ""
    return f"  - {col.name}: {col.type}{pk_marker}{null_marker}{default_marker}"


def _enum_legend(
    snapshot: DatabaseSnapshot,
    tables: list[TableInfo],
    views: list[ViewInfo],
) -> list[str]:
    """Emits each enum's labels once, keyed by the rendered type name.

    Scoped to the relations actually being rendered, so a filtered prompt does not
    carry the whole database's enums.
    """
    referenced = {c.type for t in tables for c in t.columns}
    referenced |= {c.type for v in views for c in v.columns}
    used = referenced & snapshot.enum_types.keys()
    if not used:
        return []

    lines = ["ENUMS:"]
    for name in sorted(used):
        lines.append(f"  {name}: {' | '.join(snapshot.enum_types[name])}")
    lines.append("")
    return lines


def _select(snapshot: DatabaseSnapshot, table_filter: list[str] | None):
    if table_filter is None:
        return snapshot.tables, snapshot.views
    wanted = set(table_filter)
    return (
        [t for t in snapshot.tables if t.qualified_name in wanted],
        [v for v in snapshot.views if v.qualified_name in wanted],
    )


def render_for_ask(
    snapshot: DatabaseSnapshot, table_filter: list[str] | None = None
) -> str:
    """Schema context for read-only NL-to-SQL.

    Omits indexes and view definitions: neither helps a model write a correct
    SELECT, and indexes alone are roughly a fifth of a full render. Foreign keys
    are kept because they are what make joins inferable.
    """
    tables, views = _select(snapshot, table_filter)
    lines = [f"SCHEMAS: {', '.join(snapshot.schemas)}", ""]
    lines += _enum_legend(snapshot, tables, views)

    for table in tables:
        lines.append(f"TABLE {table.qualified_name} ({_row_count(table.approx_row_count)})")
        for col in table.columns:
            lines.append(_column_line(col, include_default=False))
        for fk in table.foreign_keys:
            lines.append(
                f"  FK: ({', '.join(fk.constrained_columns)}) -> "
                f"{fk.referred_schema}.{fk.referred_table}({', '.join(fk.referred_columns)})"
            )
        lines.append("")

    for view in views:
        kind = "MATERIALIZED VIEW" if view.is_materialized else "VIEW"
        lines.append(f"{kind} {view.qualified_name} ({_row_count(view.approx_row_count)})")
        for col in view.columns:
            lines.append(_column_line(col, include_default=False))
        lines.append("")

    return "\n".join(lines).strip()


def render_for_migration(
    snapshot: DatabaseSnapshot, table_filter: list[str] | None = None
) -> str:
    """Schema context for generating a migration.

    Keeps what Ask drops: indexes (so an index is not proposed twice), defaults and
    nullability (which decide whether an ALTER rewrites the table), and the views
    depending on each table (so a column drop's blast radius is visible).
    """
    tables, views = _select(snapshot, table_filter)
    lines = [f"SCHEMAS: {', '.join(snapshot.schemas)}", ""]
    lines += _enum_legend(snapshot, tables, views)

    dependants: dict[str, list[str]] = {}
    for view in snapshot.views:
        for dep in view.depends_on:
            dependants.setdefault(dep.qualified_name, []).append(view.qualified_name)

    for table in tables:
        lines.append(f"TABLE {table.qualified_name} ({_row_count(table.approx_row_count)})")
        for col in table.columns:
            lines.append(_column_line(col, include_default=True))
        for fk in table.foreign_keys:
            lines.append(
                f"  FK: ({', '.join(fk.constrained_columns)}) -> "
                f"{fk.referred_schema}.{fk.referred_table}({', '.join(fk.referred_columns)})"
            )
        for idx in table.indexes:
            unique_marker = "UNIQUE " if idx.unique else ""
            lines.append(f"  INDEX {idx.name}: {unique_marker}({', '.join(idx.columns)})")
        for view_name in sorted(dependants.get(table.qualified_name, [])):
            lines.append(f"  DEPENDED ON BY: {view_name}")
        lines.append("")

    for view in views:
        kind = "MATERIALIZED VIEW" if view.is_materialized else "VIEW"
        lines.append(f"{kind} {view.qualified_name} ({_row_count(view.approx_row_count)})")
        for col in view.columns:
            lines.append(_column_line(col, include_default=False))
        for dep in view.depends_on:
            columns = f" ({', '.join(dep.columns)})" if dep.columns else ""
            lines.append(f"  READS: {dep.qualified_name}{columns}")
        lines.append("")

    return "\n".join(lines).strip()
