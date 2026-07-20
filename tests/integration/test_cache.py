"""RedisSchemaCache against a live Redis. Covers the paths serialize/deserialize
unit tests cannot: TTL, lock acquisition, and the concurrent-miss stampede guard."""

import threading
import time

import pytest
import redis

from app.config import settings
from app.introspection.cache import RedisSchemaCache
from app.introspection.schema_snapshot import DatabaseSnapshot

pytestmark = pytest.mark.integration


@pytest.fixture
def redis_client():
    client = redis.Redis.from_url(settings.redis_url)
    try:
        client.ping()
    except redis.exceptions.RedisError as exc:
        pytest.skip(f"redis unavailable: {exc}")
    return client


@pytest.fixture
def cache(redis_client, engine):
    cache = RedisSchemaCache(redis_client, ttl_seconds=60)
    cache.invalidate(engine)
    yield cache
    cache.invalidate(engine)


def test_cold_miss_introspects_and_populates(cache, redis_client, engine):
    assert redis_client.get(cache.cache_key(engine)) is None

    snapshot = cache.get(engine)

    assert isinstance(snapshot, DatabaseSnapshot)
    assert snapshot.tables
    assert redis_client.get(cache.cache_key(engine)) is not None


def test_warm_hit_returns_equal_snapshot(cache, engine):
    first = cache.get(engine)
    second = cache.get(engine)
    assert second == first


def test_warm_hit_does_not_reintrospect(cache, engine, monkeypatch):
    """A hit must not touch the database -- that is the point of the cache."""
    cache.get(engine)

    import app.introspection.cache as cache_module

    def fail(*args, **kwargs):
        raise AssertionError("introspect_database called on a cache hit")

    monkeypatch.setattr(cache_module, "introspect_database", fail)
    assert cache.get(engine).tables


def test_cache_key_excludes_the_password(cache, engine):
    key = cache.cache_key(engine)
    assert "nephelix12340000" not in key
    assert "***" in key or "@" in key


def test_ttl_is_applied(cache, redis_client, engine):
    cache.get(engine)
    ttl = redis_client.ttl(cache.cache_key(engine))
    assert 0 < ttl <= 60


def test_invalidate_forces_a_refetch(cache, redis_client, engine):
    cache.get(engine)
    cache.invalidate(engine)
    assert redis_client.get(cache.cache_key(engine)) is None


def test_concurrent_cold_misses_introspect_once(cache, redis_client, engine):
    """The lock exists to stop a stampede: N callers arriving on a cold cache
    should produce one introspection, not N."""
    import app.introspection.cache as cache_module

    calls = []
    real = cache_module.introspect_database

    def counting(*args, **kwargs):
        calls.append(1)
        time.sleep(0.3)  # widen the window so threads genuinely overlap
        return real(*args, **kwargs)

    cache_module.introspect_database = counting
    try:
        results = []
        threads = [
            threading.Thread(target=lambda: results.append(cache.get(engine)))
            for _ in range(5)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
    finally:
        cache_module.introspect_database = real

    assert len(results) == 5
    assert all(r == results[0] for r in results)
    assert len(calls) == 1, f"introspected {len(calls)} times, expected 1"
