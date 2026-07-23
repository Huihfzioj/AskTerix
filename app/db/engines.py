from app.config import settings
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

introspection_engine : Engine = create_engine(
    settings.introspection_db_url,
    pool_size =settings.introspection_pool_size,
    pool_pre_ping=True,
)

# F1 (Ask mode). Points at the same instance as introspection for now, but
# through the read-only role -- so promoting to a real replica later is a
# change to ask_db_url alone, not a refactor of anything in ask/.
#
# None until the role is provisioned (scripts/provision_roles.py).
ask_engine: Engine | None = (
    create_engine(
        settings.ask_db_url,
        pool_size=settings.introspection_pool_size,
        pool_pre_ping=True,
    )
    if settings.ask_db_url
    else None
)
