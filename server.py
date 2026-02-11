import os

import redis.asyncio as aioredis
from fastmcp import FastMCP
from fastmcp.server.context import Context
from starlette.requests import Request
from starlette.responses import JSONResponse

INSTANCE_ID = os.environ.get("INSTANCE_ID", "unknown")
REDIS_URL = os.environ.get("REDIS_URL", "redis://localhost:6379/0")
SESSION_TTL = 1800  # 30 minutes, matches HAProxy stick-table expire

rdb = aioredis.from_url(REDIS_URL, decode_responses=True)

mcp = FastMCP(
    name="mcp_lb_demo",
    instructions=f"Stateful MCP server instance: {INSTANCE_ID}",
)


def _key(session_id: str, suffix: str) -> str:
    return f"mcp:session:{session_id}:{suffix}"



@mcp.tool
async def increment_counter(ctx: Context) -> dict:
    """Increment the session-scoped counter and return its value."""
    sid = ctx.session_id
    key = _key(sid, "counter")
    val = await rdb.incr(key)
    await rdb.expire(key, SESSION_TTL)
    return {"counter": val, "instance": INSTANCE_ID}


@mcp.tool
async def get_counter(ctx: Context) -> dict:
    """Get the current value of the session-scoped counter."""
    sid = ctx.session_id
    val = await rdb.get(_key(sid, "counter"))
    return {"counter": int(val) if val else 0, "instance": INSTANCE_ID}


@mcp.tool
async def add_note(note: str, ctx: Context) -> dict:
    """Add a note to the session-scoped note list.

    Args:
        note: The note text to add.
    """
    sid = ctx.session_id
    key = _key(sid, "notes")
    await rdb.rpush(key, note)
    await rdb.expire(key, SESSION_TTL)
    count = await rdb.llen(key)
    return {"notes_count": count, "instance": INSTANCE_ID}


@mcp.tool
async def list_notes(ctx: Context) -> dict:
    """List all notes in the session-scoped note list."""
    sid = ctx.session_id
    notes = await rdb.lrange(_key(sid, "notes"), 0, -1)
    return {"notes": notes, "instance": INSTANCE_ID}


@mcp.tool
async def get_server_info() -> dict:
    """Return server instance information."""
    session_keys = set()
    async for key in rdb.scan_iter(match="mcp:session:*:counter"):
        parts = key.split(":")
        if len(parts) >= 3:
            session_keys.add(parts[2])
    return {"instance": INSTANCE_ID, "sessions": len(session_keys)}


@mcp.tool
async def resume_session(old_session_id: str, ctx: Context) -> dict:
    """Copy state from a previous session into the current session.

    Use this after reconnecting to recover state from a prior session
    that was lost due to a server restart or failover.

    Args:
        old_session_id: The mcp-session-id from the previous session.
    """
    new_sid = ctx.session_id
    if old_session_id == new_sid:
        return {"status": "same_session", "instance": INSTANCE_ID}

    copied = 0
    async for old_key in rdb.scan_iter(match=f"mcp:session:{old_session_id}:*"):
        suffix = old_key.split(":", 3)[3]  # everything after "mcp:session:{id}:"
        new_key = _key(new_sid, suffix)
        key_type = await rdb.type(old_key)
        if key_type == "string":
            val = await rdb.get(old_key)
            if val is not None:
                await rdb.set(new_key, val, ex=SESSION_TTL)
                copied += 1
        elif key_type == "list":
            values = await rdb.lrange(old_key, 0, -1)
            if values:
                await rdb.delete(new_key)
                await rdb.rpush(new_key, *values)
                await rdb.expire(new_key, SESSION_TTL)
                copied += 1

    return {"status": "resumed", "keys_copied": copied, "instance": INSTANCE_ID}


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    try:
        await rdb.ping()
        return JSONResponse({"status": "ok", "instance": INSTANCE_ID})
    except Exception:
        return JSONResponse(
            {"status": "degraded", "instance": INSTANCE_ID, "redis": "unreachable"},
            status_code=503,
        )


if __name__ == "__main__":
    mcp.run()
