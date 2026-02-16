from __future__ import annotations

import redis.asyncio as aioredis

from session_store import SessionStore

_PREFIX = "mcp:session"


class RedisSessionStore(SessionStore):
    """Redis-backed session store. The ONLY file that imports redis.asyncio."""

    def __init__(self, url: str) -> None:
        self._rdb = aioredis.from_url(url, decode_responses=True)

    def _fqkey(self, session_id: str, key: str) -> str:
        return f"{_PREFIX}:{session_id}:{key}"

    async def get(self, session_id: str, key: str) -> str | None:
        return await self._rdb.get(self._fqkey(session_id, key))

    async def set(self, session_id: str, key: str, value: str, ttl: int | None = None) -> None:
        fqk = self._fqkey(session_id, key)
        if ttl is not None:
            await self._rdb.set(fqk, value, ex=ttl)
        else:
            await self._rdb.set(fqk, value)

    async def delete(self, session_id: str, key: str) -> None:
        await self._rdb.delete(self._fqkey(session_id, key))

    async def keys(self, session_id: str) -> list[str]:
        prefix = f"{_PREFIX}:{session_id}:"
        result: list[str] = []
        async for fqk in self._rdb.scan_iter(match=f"{prefix}*"):
            result.append(fqk[len(prefix):])
        return result

    async def copy_session(self, src: str, dst: str, ttl: int | None = None) -> int:
        src_prefix = f"{_PREFIX}:{src}:"
        copied = 0
        async for old_key in self._rdb.scan_iter(match=f"{src_prefix}*"):
            suffix = old_key[len(src_prefix):]
            new_key = self._fqkey(dst, suffix)
            key_type = await self._rdb.type(old_key)
            if key_type == "string":
                val = await self._rdb.get(old_key)
                if val is not None:
                    if ttl is not None:
                        await self._rdb.set(new_key, val, ex=ttl)
                    else:
                        await self._rdb.set(new_key, val)
                    copied += 1
            elif key_type == "list":
                values = await self._rdb.lrange(old_key, 0, -1)
                if values:
                    await self._rdb.delete(new_key)
                    await self._rdb.rpush(new_key, *values)
                    if ttl is not None:
                        await self._rdb.expire(new_key, ttl)
                    copied += 1
        return copied

    async def session_ids(self) -> set[str]:
        ids: set[str] = set()
        async for key in self._rdb.scan_iter(match=f"{_PREFIX}:*"):
            parts = key.split(":")
            if len(parts) >= 3:
                ids.add(parts[2])
        return ids

    async def ping(self) -> bool:
        return await self._rdb.ping()
