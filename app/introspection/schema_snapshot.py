from dataclasses import dataclass, field
from datetime import datetime, timezone
from sqlalchemy import inspect, text
from sqlalchemy.engine import Connection, Engine

SYSTEM_SCHEMAS = {"information_schema", "pg_catalog", "pg_toast"}


@dataclass
class ColumnInfo:
    name: str
    type: str
    nullable: bool
    default: str | None
    is_primary_key: bool


@dataclass
class ForeignKeyInfo:
    constrained_columns: list[str]
    referred_schema: str
    referred_table: str
    referred_columns: list[str]


@dataclass
class IndexInfo:
    name: str
    columns: list[str]
    unique: bool


@dataclass
class ViewDependency:
    schema: str
    name: str
    kind: str
    columns: list[str] = field(default_factory=list)

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass
class TableInfo:
    schema: str
    name: str
    columns: list[ColumnInfo]
    foreign_keys: list[ForeignKeyInfo]
    indexes: list[IndexInfo]
    approx_row_count: int

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass
class DatabaseSnapshot:
    tables: list[TableInfo]
    schemas: list[str]
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    enum_types: dict[str, list[str]] = field(default_factory=dict)

    def render_for_llm(self, table_filter: list[str] | None = None) -> str:
        tables = self.tables
        if table_filter is not None:
            wanted = set(table_filter)
            tables = [t for t in tables if t.qualified_name in wanted]

        lines = [f"SCHEMAS: {', '.join(self.schemas)}", ""]

        used = {c.type for t in tables for c in t.columns} & self.enum_types.keys()
        if used:
            lines.append("ENUMS:")
            for name in sorted(used):
                lines.append(f"  {name}: {' | '.join(self.enum_types[name])}")
            lines.append("")

        for table in tables:
            count = f"~{table.approx_row_count:,} rows" if table.approx_row_count >=0 else "row count unknown"
            lines.append(f"TABLE {table.qualified_name} ({count})")
            for col in table.columns:
                pk_marker = " [PK]" if col.is_primary_key else ""
                null_marker = "" if col.nullable else " NOT NULL"
                default_marker = f" DEFAULT {col.default}" if col.default else ""
                lines.append(f"  - {col.name}: {col.type}{pk_marker}{null_marker}{default_marker}")

            for fk in table.foreign_keys:
                lines.append(
                    f"  FK: ({', '.join(fk.constrained_columns)}) -> "
                    f"{fk.referred_schema}.{fk.referred_table}({', '.join(fk.referred_columns)})"
                )

            for idx in table.indexes:
                unique_marker = "UNIQUE " if idx.unique else ""
                lines.append(f"  INDEX {idx.name}: {unique_marker}({', '.join(idx.columns)})")

            lines.append("")

        return "\n".join(lines).strip()


def list_schemas(engine: Engine) -> list[str]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT nspname
                FROM pg_namespace
                WHERE nspname NOT LIKE 'pg\\_%' ESCAPE '\\'
                  AND nspname != 'information_schema'
                  AND has_schema_privilege(current_user, nspname, 'USAGE')
                ORDER BY nspname
            """)
        ).fetchall()
    return [r[0] for r in rows if r[0] not in SYSTEM_SCHEMAS]


def _get_approx_row_counts(engine: Engine, schemas: list[str]) -> dict[tuple[str, str], int]:
    with engine.connect() as conn:
        rows = conn.execute(
            text("""
                SELECT n.nspname, c.relname, c.reltuples::bigint
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE c.relkind IN ('r', 'p', 'f', 'm')
                  AND n.nspname = ANY(:schemas)
            """),
            {"schemas": schemas},
        ).fetchall()
    return {(r[0], r[1]): int(r[2]) for r in rows}


_VIEW_DEPENDENCY_QUERY = text("""
    SELECT DISTINCT
        view_ns.nspname   AS view_schema,
        view_rel.relname  AS view_name,
        src_ns.nspname    AS source_schema,
        src.relname       AS source_name,
        src.relkind       AS source_kind,
        att.attname       AS source_column
    FROM pg_depend dep
    JOIN pg_rewrite rw
      ON dep.objid = rw.oid
     AND dep.classid = 'pg_rewrite'::regclass
    JOIN pg_class view_rel ON rw.ev_class = view_rel.oid
    JOIN pg_namespace view_ns ON view_rel.relnamespace = view_ns.oid
    JOIN pg_class src ON dep.refobjid = src.oid
    JOIN pg_namespace src_ns ON src.relnamespace = src_ns.oid
    LEFT JOIN pg_attribute att
      ON att.attrelid = src.oid
     AND att.attnum = dep.refobjsubid
    WHERE view_rel.relkind IN ('v', 'm')
      AND src.relkind IN ('r', 'v', 'm', 'p', 'f')
      AND view_rel.oid <> src.oid
      AND src_ns.nspname NOT IN ('pg_catalog', 'information_schema', 'pg_toast')
    ORDER BY 1, 2, 3, 4, 6
""")


def get_view_dependencies(
    bind: Engine | Connection,
) -> dict[tuple[str, str], list[ViewDependency]]:
    if isinstance(bind, Engine):
        with bind.connect() as conn:
            rows = conn.execute(_VIEW_DEPENDENCY_QUERY).fetchall()
    else:
        rows = bind.execute(_VIEW_DEPENDENCY_QUERY).fetchall()

    deps: dict[tuple[str, str], dict[tuple[str, str], ViewDependency]] = {}
    for view_schema, view_name, src_schema, src_name, src_kind, src_column in rows:
        sources = deps.setdefault((view_schema, view_name), {})
        dependency = sources.get((src_schema, src_name))
        if dependency is None:
            dependency = ViewDependency(
                schema=src_schema, name=src_name, kind=src_kind, columns=[]
            )
            sources[(src_schema, src_name)] = dependency
        # NULL column means a whole-relation dependency, which carries no column name.
        if src_column is not None and src_column not in dependency.columns:
            dependency.columns.append(src_column)

    return {
        view: sorted(sources.values(), key=lambda d: (d.schema, d.name))
        for view, sources in deps.items()
    }


def introspect_database(engine: Engine, schemas: list[str] | None = None) -> DatabaseSnapshot:

    inspector = inspect(engine)
    target_schemas = schemas if schemas is not None else list_schemas(engine)
    default_schema = inspector.default_schema_name

    enum_types : dict[str, list[str]] = {}
    row_counts = _get_approx_row_counts(engine, target_schemas)
    tables: list[TableInfo] = []
    for schema in target_schemas:
        for table_name in inspector.get_table_names(schema=schema):
            pk_constraint = inspector.get_pk_constraint(table_name, schema=schema)
            pk_columns = set(pk_constraint.get("constrained_columns") or [])

            columns = []
            for col in inspector.get_columns(table_name, schema=schema):
                labels = getattr(col["type"], "enums", None)
                if labels:
                    enum_types[col["type"].name] = list(labels)
                columns.append(
                    ColumnInfo(
                        name=col["name"],
                        type=col["type"].compile(dialect=engine.dialect),
                        nullable=col["nullable"],
                        default=col.get("default"),
                        is_primary_key=col["name"] in pk_columns,
                    )
                )

            foreign_keys = [
                ForeignKeyInfo(
                    constrained_columns=fk["constrained_columns"],
                    referred_schema=fk.get("referred_schema") or default_schema,
                    referred_table=fk["referred_table"],
                    referred_columns=fk["referred_columns"],
                )
                for fk in inspector.get_foreign_keys(table_name, schema=schema)
            ]

            indexes = [
                IndexInfo(name=idx["name"], columns=idx["column_names"], unique=idx["unique"])
                for idx in inspector.get_indexes(table_name, schema=schema)
            ]

            tables.append(
                TableInfo(
                    schema=schema,
                    name=table_name,
                    columns=columns,
                    foreign_keys=foreign_keys,
                    indexes=indexes,
                    approx_row_count=row_counts.get((schema, table_name), -1),
                )
            )

    return DatabaseSnapshot(tables=tables, schemas=target_schemas, enum_types=enum_types)