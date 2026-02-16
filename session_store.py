from __future__ import annotations

import abc
import json
from typing import Any


class SessionStore(abc.ABC):
    """Minimal backend interface for session storage. Works with raw strings."""

    @abc.abstractmethod
    async def get(self, session_id: str, key: str) -> str | None: ...

    @abc.abstractmethod
    async def set(self, session_id: str, key: str, value: str, ttl: int | None = None) -> None: ...

    @abc.abstractmethod
    async def delete(self, session_id: str, key: str) -> None: ...

    @abc.abstractmethod
    async def keys(self, session_id: str) -> list[str]: ...

    @abc.abstractmethod
    async def copy_session(self, src: str, dst: str, ttl: int | None = None) -> int: ...

    @abc.abstractmethod
    async def session_ids(self) -> set[str]: ...

    @abc.abstractmethod
    async def ping(self) -> bool: ...


class Session:
    """Binds a store + session_id + TTL. Adds JSON serialization.

    Tool authors only see ``session.get("counter")`` / ``session.set("notes", [...])``.
    """

    def __init__(self, store: SessionStore, session_id: str, default_ttl: int) -> None:
        self._store = store
        self._session_id = session_id
        self._ttl = default_ttl

    async def get(self, key: str) -> Any:
        raw = await self._store.get(self._session_id, key)
        return json.loads(raw) if raw is not None else None

    async def set(self, key: str, value: Any) -> None:
        await self._store.set(self._session_id, key, json.dumps(value), ttl=self._ttl)

    async def delete(self, key: str) -> None:
        await self._store.delete(self._session_id, key)

    async def copy_from(self, old_session_id: str) -> int:
        return await self._store.copy_session(old_session_id, self._session_id, ttl=self._ttl)
