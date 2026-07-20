from contextlib import contextmanager
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
class ViewInfo:
    schema: str
    name: str
    is_materialized: bool
    columns: list[ColumnInfo]
    definition: str
    depends_on: list[ViewDependency]
    approx_row_count: int | None

    @property
    def qualified_name(self) -> str:
        return f"{self.schema}.{self.name}"


@dataclass
class DatabaseSnapshot:
    tables: list[TableInfo]
    schemas: list[str]
    generated_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    enum_types: dict[str, list[str]] = field(default_factory=dict)
    views: list[ViewInfo] = field(default_factory=list)


def list_schemas(bind: Engine | Connection) -> list[str]:
    with _connection(bind) as conn:
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


@contextmanager
def _connection(bind: Engine | Connection):
    """Yields a usable Connection, opening one only if given an Engine.

    Lets callers pass an open Connection so introspection can see objects created
    inside a transaction that has not been committed -- which is how the view
    tests avoid leaving DDL behind.
    """
    if isinstance(bind, Engine):
        with bind.connect() as conn:
            yield conn
    else:
        yield bind


def _get_approx_row_counts(
    bind: Engine | Connection, schemas: list[str]
) -> dict[tuple[str, str], int]:
    with _connection(bind) as conn:
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
    with _connection(bind) as conn:
        rows = conn.execute(_VIEW_DEPENDENCY_QUERY).fetchall()

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


def find_affected_views(
    dependencies: dict[tuple[str, str], list[ViewDependency]],
    schema: str,
    name: str,
    column: str | None = None,
) -> set[tuple[str, str]]:
    """Every view broken by dropping a relation, or one of its columns.

    Walks the dependency graph transitively: Postgres refuses to drop an object a
    view depends on unless CASCADE is used, and CASCADE drops that view, which in
    turn drops anything built on it. So a view two hops away is just as broken as
    a direct dependant.

    Passing a ``column`` narrows only the *first* hop. A view depending on the
    relation as a whole (``SELECT count(*)``) survives a column drop but not a
    relation drop, so it is included only when ``column`` is None. Past that first
    hop the distinction stops applying: once an intermediate view is dropped,
    everything reading it breaks regardless of which columns it read.
    """
    dependants: dict[tuple[str, str], set[tuple[str, str]]] = {}
    for view, sources in dependencies.items():
        for source in sources:
            dependants.setdefault((source.schema, source.name), set()).add(view)

    affected: set[tuple[str, str]] = set()
    queue: list[tuple[str, str]] = []

    for view, sources in dependencies.items():
        for source in sources:
            if (source.schema, source.name) != (schema, name):
                continue
            if column is None or column in source.columns:
                affected.add(view)
                queue.append(view)

    while queue:
        current = queue.pop()
        for dependant in dependants.get(current, ()):
            if dependant not in affected:
                affected.add(dependant)
                queue.append(dependant)

    return affected

def introspect_views(
    bind: Engine | Connection,
    schemas: list[str] | None = None,
    enum_types: dict[str, list[str]] | None = None,
) -> list[ViewInfo]:
    inspector = inspect(bind)
    dialect = bind.dialect
    target_schemas = schemas if schemas is not None else list_schemas(bind)
    row_counts = _get_approx_row_counts(bind, target_schemas)
    dependencies = get_view_dependencies(bind)

    views: list[ViewInfo] = []
    for schema in target_schemas:
        plain = inspector.get_view_names(schema=schema)
        materialized = inspector.get_materialized_view_names(schema=schema)

        for view_name in plain + materialized:
            pk_constraint = inspector.get_pk_constraint(view_name, schema=schema)
            pk_columns = set(pk_constraint.get("constrained_columns") or [])

            columns = []
            for col in inspector.get_columns(view_name, schema=schema):
                labels = getattr(col["type"], "enums", None)
                if labels and enum_types is not None:
                    enum_types[col["type"].name] = list(labels)
                columns.append(
                    ColumnInfo(
                        name=col["name"],
                        type=col["type"].compile(dialect=dialect),
                        nullable=col["nullable"],
                        default=col.get("default"),
                        is_primary_key=col["name"] in pk_columns,
                    )
                )

            views.append(
                ViewInfo(
                    schema=schema,
                    name=view_name,
                    is_materialized=view_name in materialized,
                    columns=columns,
                    definition=inspector.get_view_definition(view_name, schema=schema),
                    depends_on=dependencies.get((schema, view_name), []),
                    # Only materialized views store rows; a plain view has no count.
                    approx_row_count=row_counts.get((schema, view_name)),
                )
            )

    return views


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

    # A view's columns can carry enum types, so collect into the same map.
    views = introspect_views(engine, target_schemas, enum_types=enum_types)

    return DatabaseSnapshot(
        tables=tables,
        schemas=target_schemas,
        enum_types=enum_types,
        views=views,
    )