from pathlib import Path
from pydantic_settings import BaseSettings

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

class Settings(BaseSettings):

    introspection_db_url: str 
    intospection_pool_size: int = 5
    
    class Config:
        env_file = str(ENV_FILE)

settings = Settings()