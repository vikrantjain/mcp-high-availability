from __future__ import annotations

import time

from session_store import SessionStore


class InMemorySessionStore(SessionStore):
    """Dict-backed session store with lazy TTL expiry. For local dev/tests."""

    def __init__(self) -> None:
        # {(session_id, key): (value, expires_at | None)}
        self._data: dict[tuple[str, str], tuple[str, float | None]] = {}

    def _is_alive(self, entry: tuple[str, float | None]) -> bool:
        _, expires = entry
        return expires is None or expires > time.monotonic()

    def _read(self, session_id: str, key: str) -> str | None:
        entry = self._data.get((session_id, key))
        if entry is None:
            return None
        if not self._is_alive(entry):
            del self._data[(session_id, key)]
            return None
        return entry[0]

    async def get(self, session_id: str, key: str) -> str | None:
        return self._read(session_id, key)

    async def set(self, session_id: str, key: str, value: str, ttl: int | None = None) -> None:
        expires = time.monotonic() + ttl if ttl is not None else None
        self._data[(session_id, key)] = (value, expires)

    async def delete(self, session_id: str, key: str) -> None:
        self._data.pop((session_id, key), None)

    async def keys(self, session_id: str) -> list[str]:
        result: list[str] = []
        to_remove: list[tuple[str, str]] = []
        for (sid, k), entry in self._data.items():
            if sid != session_id:
                continue
            if self._is_alive(entry):
                result.append(k)
            else:
                to_remove.append((sid, k))
        for dead in to_remove:
            del self._data[dead]
        return result

    async def copy_session(self, src: str, dst: str, ttl: int | None = None) -> int:
        src_keys = await self.keys(src)
        expires = time.monotonic() + ttl if ttl is not None else None
        copied = 0
        for k in src_keys:
            val = self._read(src, k)
            if val is not None:
                self._data[(dst, k)] = (val, expires)
                copied += 1
        return copied

    async def session_ids(self) -> set[str]:
        ids: set[str] = set()
        to_remove: list[tuple[str, str]] = []
        for (sid, k), entry in self._data.items():
            if self._is_alive(entry):
                ids.add(sid)
            else:
                to_remove.append((sid, k))
        for dead in to_remove:
            del self._data[dead]
        return ids

    async def ping(self) -> bool:
        return True
