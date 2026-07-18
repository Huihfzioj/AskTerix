from app.config import settings
from sqlalchemy import create_engine
from sqlalchemy.engine import Engine

introspection_engine : Engine = create_engine(
    settings.introspection_db_url,
    pool_size =settings.introspection_pool_size,
    pool_pre_ping=True,
)
