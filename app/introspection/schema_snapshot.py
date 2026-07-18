from dataclasses import dataclass, field
from datetime import datetime, timezone
from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine

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

    def render_for_llm(self, table_filter: list[str] | None = None) -> str:
        tables = self.tables
        if table_filter is not None:
            wanted = set(table_filter)
            tables = [t for t in tables if t.qualified_name in wanted]

        lines = [f"SCHEMAS: {', '.join(self.schemas)}", ""]

        for table in tables:
            lines.append(f"TABLE {table.qualified_name} (~{table.approx_row_count:,} rows)")
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


def _get_approx_row_count(engine: Engine, schema: str, table_name: str) -> int:
    with engine.connect() as conn:
        result = conn.execute(
            text("""
                SELECT c.reltuples::bigint
                FROM pg_class c
                JOIN pg_namespace n ON n.oid = c.relnamespace
                WHERE n.nspname = :schema AND c.relname = :name
            """),
            {"schema": schema, "name": table_name},
        ).scalar()
        return int(result) if result is not None else 0


def introspect_database(engine: Engine, schemas: list[str] | None = None) -> DatabaseSnapshot:

    inspector = inspect(engine)
    target_schemas = schemas if schemas is not None else list_schemas(engine)
    default_schema = inspector.default_schema_name

    tables: list[TableInfo] = []
    for schema in target_schemas:
        for table_name in inspector.get_table_names(schema=schema):
            pk_constraint = inspector.get_pk_constraint(table_name, schema=schema)
            pk_columns = set(pk_constraint.get("constrained_columns") or [])

            columns = [
                ColumnInfo(
                    name=col["name"],
                    type=str(col["type"]),
                    nullable=col["nullable"],
                    default=col.get("default"),
                    is_primary_key=col["name"] in pk_columns,
                )
                for col in inspector.get_columns(table_name, schema=schema)
            ]

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
                    approx_row_count=_get_approx_row_count(engine, schema, table_name),
                )
            )

    return DatabaseSnapshot(tables=tables, schemas=target_schemas)