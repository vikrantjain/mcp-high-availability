"""FastMCP client-based tests for MCP server functionality.

Tests tool responses, notifications, and resources using the FastMCP client library.
Does not test infrastructure (HAProxy, load balancing, sticky sessions).
"""

import asyncio
import json
import sys
from resilient_client import ResilientClient

SERVER_URL = "http://localhost:8080/mcp"


async def test_tools(client: ResilientClient):
    """Test all MCP tools return correct responses."""
    print("=== Test: Tools ===")
    
    result = await client.call_tool("increment_counter", {})
    assert result.data["counter"] == 1
    print(f"  ✓ increment_counter: {result.data['counter']}")
    
    result = await client.call_tool("get_counter", {})
    assert result.data["counter"] == 1
    print(f"  ✓ get_counter: {result.data['counter']}")
    
    result = await client.call_tool("add_note", {"note": "test note"})
    assert result.data["notes_count"] == 1
    print(f"  ✓ add_note: {result.data['notes_count']} notes")
    
    result = await client.call_tool("list_notes", {})
    assert result.data["notes"] == ["test note"]
    print(f"  ✓ list_notes: {result.data['notes']}")
    
    result = await client.call_tool("get_server_info", {})
    assert "instance" in result.data
    print(f"  ✓ get_server_info: {result.data['instance']}")
    
    result = await client.call_tool("get_status", {})
    assert "uptime_seconds" in result.data
    print(f"  ✓ get_status: uptime={result.data['uptime_seconds']:.1f}s")
    
    print("  PASSED\n")


async def test_notifications(client: ResilientClient):
    """Test tools that emit notifications."""
    print("=== Test: Notifications ===")
    
    result = await client.call_tool("analyze_data", {"num_items": 3})
    assert result.data["items_processed"] == 3
    assert result.data["total_score"] == 9.0
    print(f"  ✓ analyze_data: {result.data['items_processed']} items, score={result.data['total_score']}")
    
    print("  PASSED\n")


async def test_resources(client: ResilientClient):
    """Test reading MCP resources."""
    print("=== Test: Resources ===")
    
    await client.call_tool("increment_counter", {})
    await client.call_tool("add_note", {"note": "resource test"})
    
    resources = await client.list_resources()
    print(f"  ✓ Listed {len(resources)} resources")
    
    # Test session summary resource if available
    if resources:
        session_uri = next((r.uri for r in resources if "session" in r.uri), None)
        if session_uri:
            contents = await client.read_resource(session_uri)
            data = json.loads(contents[0].text)
            assert "counter" in data
            assert "notes" in data
            print(f"  ✓ Session summary: counter={data['counter']}, notes={data['notes']}")
    
    print("  PASSED\n")


async def test_resume_session(client: ResilientClient):
    """Test session resumption logic."""
    print("=== Test: Resume Session ===")
    
    result = await client.call_tool("resume_session", {"old_session_id": "nonexistent"})
    assert result.data["status"] == "resumed" and result.data["keys_copied"] == 0
    print(f"  ✓ Non-existent session: {result.data['status']}, keys_copied={result.data['keys_copied']}")
    
    print("  PASSED\n")


async def test_watch_counter(client: ResilientClient):
    """Test watch_counter with concurrent updates."""
    print("=== Test: Watch Counter ===")
    
    # Ensure counter has a known starting value
    current = await client.call_tool("get_counter", {})
    start_value = current.data["counter"]
    
    # Increment once to have a baseline
    await client.call_tool("increment_counter", {})
    
    async def run_watcher():
        result = await client.call_tool("watch_counter", {"duration_seconds": 3})
        return result
    
    async def increment_loop():
        await asyncio.sleep(1)
        for _ in range(3):
            await client.call_tool("increment_counter", {})
            await asyncio.sleep(0.8)

    watcher_task = asyncio.create_task(run_watcher())
    increment_task = asyncio.create_task(increment_loop())

    data = await watcher_task
    await increment_task

    assert data.data["total_changes"] >= 1, f"Expected at least 1 change, got {data.data['total_changes']}"
    print(f"  ✓ Detected {data.data['total_changes']} changes (started from counter={start_value})")
    
    print("  PASSED\n")


async def main():
    print("FastMCP Client Tests")
    print("=" * 50)
    print(f"Target: {SERVER_URL}\n")
    
    try:
        async with ResilientClient(SERVER_URL) as client:
            print("✓ Server reachable\n")
            
            await test_tools(client)
            await test_notifications(client)
            await test_resources(client)
            await test_resume_session(client)
            await test_watch_counter(client)
            
            print("=" * 50)
            print("All tests passed!")
    except Exception as e:
        print(f"ERROR: {e}")
        print("Run: docker compose up -d")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
