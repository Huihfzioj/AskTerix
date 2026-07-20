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
    

settings = Settings()