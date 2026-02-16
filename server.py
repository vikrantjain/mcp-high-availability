import asyncio
import json
import os
import time
from datetime import UTC, datetime

from fastmcp import FastMCP
from fastmcp.server.context import Context
from starlette.requests import Request
from starlette.responses import JSONResponse

from session_store import Session, SessionStore
from stores.redis_store import RedisSessionStore
from stores.memory_store import InMemorySessionStore

INSTANCE_ID = os.environ.get("INSTANCE_ID", "unknown")
REDIS_URL = os.environ.get("REDIS_URL", "")
SESSION_TTL = 1800  # 30 minutes, matches HAProxy stick-table expire
START_TIME = time.monotonic()

store: SessionStore = RedisSessionStore(REDIS_URL) if REDIS_URL else InMemorySessionStore()

mcp = FastMCP(
    name="mcp_lb_demo",
    instructions=f"Stateful MCP server instance: {INSTANCE_ID}",
)


def get_session(ctx: Context) -> Session:
    return Session(store, ctx.session_id, default_ttl=SESSION_TTL)


@mcp.tool
async def increment_counter(ctx: Context) -> dict:
    """Increment the session-scoped counter and return its value."""
    s = get_session(ctx)
    val = (await s.get("counter") or 0) + 1
    await s.set("counter", val)
    return {"counter": val, "instance": INSTANCE_ID}


@mcp.tool
async def get_counter(ctx: Context) -> dict:
    """Get the current value of the session-scoped counter."""
    s = get_session(ctx)
    val = await s.get("counter")
    return {"counter": val if val else 0, "instance": INSTANCE_ID}


@mcp.tool
async def add_note(note: str, ctx: Context) -> dict:
    """Add a note to the session-scoped note list.

    Args:
        note: The note text to add.
    """
    s = get_session(ctx)
    notes = await s.get("notes") or []
    notes.append(note)
    await s.set("notes", notes)
    return {"notes_count": len(notes), "instance": INSTANCE_ID}


@mcp.tool
async def list_notes(ctx: Context) -> dict:
    """List all notes in the session-scoped note list."""
    s = get_session(ctx)
    notes = await s.get("notes") or []
    return {"notes": notes, "instance": INSTANCE_ID}


@mcp.tool
async def get_server_info() -> dict:
    """Return server instance information."""
    session_ids = await store.session_ids()
    return {"instance": INSTANCE_ID, "sessions": len(session_ids)}


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

    s = get_session(ctx)
    copied = await s.copy_from(old_session_id)
    return {"status": "resumed", "keys_copied": copied, "instance": INSTANCE_ID}


@mcp.tool
async def analyze_data(num_items: int, ctx: Context) -> dict:
    """Run a multi-step data analysis pipeline on the session's data.

    Args:
        num_items: Number of items to process (controls duration).
    """
    s = get_session(ctx)
    total_steps = num_items
    results = []

    await ctx.info(f"Starting analysis of {num_items} items on {INSTANCE_ID}")

    # Phase 1: Validation
    await ctx.report_progress(0, total_steps, "Validating input data")
    await ctx.debug("Phase 1: Input validation")
    await asyncio.sleep(0.5)

    # Phase 2: Processing each item
    for i in range(1, num_items + 1):
        await ctx.report_progress(i, total_steps, f"Processing item {i}/{num_items}")
        await ctx.debug(f"Processing item {i}")
        await asyncio.sleep(0.3)
        results.append({"item": i, "score": i * 1.5})

    # Phase 3: Aggregation
    await ctx.info("Aggregating results")
    await asyncio.sleep(0.3)
    total_score = sum(r["score"] for r in results)

    await s.set("analysis_result", {"total_score": total_score, "items_processed": num_items})

    await ctx.info(f"Analysis complete: processed {num_items} items, total score={total_score}")

    return {
        "items_processed": num_items,
        "total_score": total_score,
        "instance": INSTANCE_ID,
    }


@mcp.tool
async def get_status(ctx: Context) -> dict:
    """Get real-time server status including active session count and uptime.

    This is a fast tool that can be called while other long-running tools are executing.
    """
    session_ids = await store.session_ids()
    uptime = time.monotonic() - START_TIME
    return {
        "instance": INSTANCE_ID,
        "uptime_seconds": round(uptime, 1),
        "active_sessions": len(session_ids),
        "timestamp": datetime.now(UTC).isoformat(),
    }


@mcp.tool
async def watch_counter(duration_seconds: int, ctx: Context) -> dict:
    """Watch the session counter for changes and stream updates via log notifications.

    Runs for the specified duration, polling and notifying on changes.
    This simulates a resource subscription — the client receives real-time
    log notifications whenever the counter changes.

    Args:
        duration_seconds: How long to watch (max 30 seconds).
    """
    s = get_session(ctx)
    duration_seconds = min(duration_seconds, 30)

    last_value = await s.get("counter")
    changes = []

    await ctx.info(f"Watching counter for {duration_seconds}s (current: {last_value or 0})")

    end_time = asyncio.get_event_loop().time() + duration_seconds
    elapsed = 0
    while asyncio.get_event_loop().time() < end_time:
        await asyncio.sleep(0.5)
        elapsed += 0.5
        current = await s.get("counter")
        await ctx.report_progress(elapsed, duration_seconds, "Watching...")
        if current != last_value:
            change = {"from": last_value or 0, "to": current or 0, "at": elapsed}
            changes.append(change)
            await ctx.info(f"Counter changed: {change['from']} -> {change['to']}")
            last_value = current

    await ctx.info(f"Watch complete. {len(changes)} change(s) detected.")
    return {"changes": changes, "total_changes": len(changes), "instance": INSTANCE_ID}


@mcp.resource("resource://session/{session_id}/summary")
async def session_summary(session_id: str) -> str:
    """Summary of all state for a given session."""
    s = Session(store, session_id, default_ttl=SESSION_TTL)
    counter = await s.get("counter")
    notes = await s.get("notes")
    analysis = await s.get("analysis_result")

    summary = {
        "session_id": session_id,
        "counter": counter if counter else 0,
        "notes": notes or [],
        "analysis_result": analysis,
        "instance": INSTANCE_ID,
    }
    return json.dumps(summary, indent=2)


@mcp.custom_route("/health", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    try:
        await store.ping()
        return JSONResponse({"status": "ok", "instance": INSTANCE_ID})
    except Exception:
        return JSONResponse(
            {"status": "degraded", "instance": INSTANCE_ID, "store": "unreachable"},
            status_code=503,
        )


if __name__ == "__main__":
    mcp.run()
