from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

class Settings(BaseSettings):

    model_config = SettingsConfigDict(
        env_file=str(ENV_FILE),
        extra = "allow",
    )
    introspection_db_url: str
    introspection_pool_size: int = 5
    redis_url: str = "redis://127.0.0.1:6379/0"

    ask_db_url: str | None = None
    ask_db_password: str | None = None
    ask_role_name: str = "altermind_ask"
    ask_statement_timeout: str = "10s"
    # Whose tables the default privileges follow. Defaults to the provisioning
    # connection's user.
    ask_table_owner: str | None = None


settings = Settings()