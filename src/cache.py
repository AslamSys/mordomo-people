"""
Redis cache layer — mordomo-people uses db 0.
Caches person lookups and permissions to avoid hitting Postgres on every request.
"""
import json
import redis.asyncio as aioredis
from typing import Optional
from src.config import REDIS_URL, RESOLVE_CACHE_TTL, PERMISSIONS_CACHE_TTL


_redis: Optional[aioredis.Redis] = None


async def init_redis() -> None:
    global _redis
    _redis = aioredis.from_url(REDIS_URL, decode_responses=True)


async def close_redis() -> None:
    if _redis:
        await _redis.aclose()


# ── Person resolve cache ───────────────────────────────────────────────────────

def _resolve_key(name: str) -> str:
    return f"people:resolve:{name.lower()}"


async def get_cached_person(name: str) -> Optional[dict]:
    raw = await _redis.get(_resolve_key(name))
    return json.loads(raw) if raw else None


async def set_cached_person(name: str, data: dict) -> None:
    await _redis.setex(_resolve_key(name), RESOLVE_CACHE_TTL, json.dumps(data))


async def invalidate_person(name: str) -> None:
    await _redis.delete(_resolve_key(name))


# ── Permissions cache ──────────────────────────────────────────────────────────

def _permissions_key(person_id: str) -> str:
    return f"people:permissions:{person_id}"


async def get_cached_permissions(person_id: str) -> Optional[dict]:
    raw = await _redis.get(_permissions_key(person_id))
    return json.loads(raw) if raw else None


async def set_cached_permissions(person_id: str, data: dict) -> None:
    await _redis.setex(_permissions_key(person_id), PERMISSIONS_CACHE_TTL, json.dumps(data))


async def invalidate_permissions(person_id: str) -> None:
    await _redis.delete(_permissions_key(person_id))
