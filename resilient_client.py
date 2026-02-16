"""Resilient FastMCP Client with automatic session resumption on failures."""

import asyncio
import functools
import inspect

from fastmcp import Client


class ResilientClient:
    """FastMCP Client wrapper with automatic session resumption on failures.

    All async methods delegated to the inner Client are wrapped with retry logic
    that reconnects and resumes the session on failure.
    """

    def __init__(self, server_url: str, max_retries: int = 3):
        self.server_url = server_url
        self.max_retries = max_retries
        self._client: Client | None = None
        self._session_id: str | None = None

    async def __aenter__(self):
        await self._connect()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._client:
            await self._client.__aexit__(exc_type, exc_val, exc_tb)

    async def _connect(self):
        self._client = Client(self.server_url)
        await self._client.__aenter__()
        self._session_id = self._client.transport.get_session_id()

    async def _reconnect_and_resume(self):
        old_session_id = self._session_id
        if self._client:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                pass
        await self._connect()
        if old_session_id:
            try:
                result = await self._client.call_tool("resume_session", {"old_session_id": old_session_id})
                if result.data.get("status") == "resumed":
                    print(f"  Session resumed: {result.data.get('keys_copied', 0)} keys restored")
            except Exception as e:
                print(f"  Session resume failed: {e}")

    def __getattr__(self, name):
        attr = getattr(self._client, name)
        if not callable(attr) or not inspect.iscoroutinefunction(attr):
            return attr

        @functools.wraps(attr)
        async def wrapper(*args, **kwargs):
            for attempt in range(self.max_retries):
                try:
                    return await getattr(self._client, name)(*args, **kwargs)
                except Exception:
                    if attempt < self.max_retries - 1:
                        print(f"  Connection failed (attempt {attempt + 1}/{self.max_retries}), reconnecting...")
                        await asyncio.sleep(attempt + 1)
                        await self._reconnect_and_resume()
                    else:
                        raise

        return wrapper
