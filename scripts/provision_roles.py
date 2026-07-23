"""Applies sql/001_read_only_role.sql against the target database.

Run with a superuser (or role-creating) connection:

    py -m scripts.provision_roles

Idempotent -- re-running is safe and is how you rotate the role's password.
"""

import sys
from pathlib import Path

from psycopg2 import sql
from sqlalchemy import create_engine

from app.config import settings

SQL_FILE = Path(__file__).resolve().parent.parent / "sql" / "001_read_only_role.sql"


def build_statement(
    role_name: str, database: str, owner: str, password: str, statement_timeout: str
) -> sql.Composed:
    """Composes the SQL file, quoting identifiers and literals separately.

    Postgres accepts a parameter for the password literal but not for an
    identifier, so role/database/owner names go through Identifier() rather
    than through bind parameters.
    """
    return sql.SQL(SQL_FILE.read_text(encoding="utf-8")).format(
        role=sql.Identifier(role_name),
        role_name=sql.Literal(role_name),
        database=sql.Identifier(database),
        owner=sql.Identifier(owner),
        password=sql.Literal(password),
        statement_timeout=sql.Literal(statement_timeout),
    )


def main() -> int:
    if not settings.ask_db_password:
        print(
            "ask_db_password is not set. Add it to .env before provisioning:\n"
            '  ask_db_password="<a password you choose>"',
            file=sys.stderr,
        )
        return 1

    engine = create_engine(settings.introspection_db_url)
    database = engine.url.database
    owner = settings.ask_table_owner or engine.url.username

    statement = build_statement(
        role_name=settings.ask_role_name,
        database=database,
        owner=owner,
        password=settings.ask_db_password,
        statement_timeout=settings.ask_statement_timeout,
    )

    connection = engine.raw_connection()
    try:
        with connection.cursor() as cursor:
            cursor.execute(statement)
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()

    print(
        f"Provisioned role {settings.ask_role_name!r} on database {database!r} "
        f"(default privileges follow tables owned by {owner!r})."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
