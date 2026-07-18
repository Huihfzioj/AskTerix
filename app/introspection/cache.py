import json 
from dataclasses import asdict
from datetime import datetime
import redis
from sqlalchemy.engine import Engine
from app.introspection.schema_snapshot import ColumnInfo, DatabaseSnapshot, ForeignKeyInfo, IndexInfo, TableInfo, introspect_database

DEFAULT_TTL_SECONDS = 300
LOCK_TIMEOUT_SECONDS = 30

def serialize(snapshot : DatabaseSnapshot) -> str:
    payload = asdict(snapshot)
    payload["generated_at"] = snapshot.generated_at.isoformat()
    return json.dumps(payload)

def deserialize(raw : str) -> DatabaseSnapshot:
    payload = json.loads(raw)
    tables = [
        TableInfo(
            schema=t["schema"],
            name=t["name"],
            columns=[ColumnInfo(**c) for c in t["columns"]],
            foreign_keys=[ForeignKeyInfo(**fk) for fk in t["foreign_keys"]],
            indexes=[IndexInfo(**idx) for idx in t["indexes"]],
            approx_row_count=t["approx_row_count"],
        )
        for t in payload["tables"]
    ]
    return DatabaseSnapshot(
        tables=tables,
        schemas=payload["schemas"],
        generated_at=datetime.fromisoformat(payload["generated_at"]),
    )

class RedisSchemaCache:
    def __init__(self, redis_client: redis.Redis, ttl_seconds: int = DEFAULT_TTL_SECONDS):
        self.redis = redis_client
        self.ttl = ttl_seconds
    
    def cache_key(self, engine: Engine) -> str:
        return f"schema_snapshot:{engine.url.render_as_string(hide_password=True)}"
    
    def get(self, engine: Engine) -> DatabaseSnapshot:
        key = self.cache_key(engine)
        cached = self.redis.get(key)
        if cached is not None:
            return deserialize(cached)
        
        lock = self.redis.lock(f"{key}:lock", timeout=LOCK_TIMEOUT_SECONDS, blocking_timeout=10)
        acquired = lock.acquire(blocking=True)
        try:
            cached = self.redis.get(key)
            if cached is not None:
                return deserialize(cached)
            snapshot = introspect_database(engine)
            self.redis.setex(key, self.ttl, serialize(snapshot))
            return snapshot
        finally:
            if acquired:
                lock.release()

    def invalidate(self, engine: Engine) -> None:
        self.redis.delete(self.cache_key(engine))