"""Test resilient client with actual container restarts."""

import asyncio
import subprocess
import sys

import httpx

from resilient_client import ResilientClient

SERVER_URL = "http://localhost:8080/mcp"


async def test_resilience_with_restart():
    """Test that client survives container restarts with session resumption."""
    print("Resilient Client Test - Container Restart")
    print("=" * 50)
    
    async with ResilientClient(SERVER_URL, max_retries=5) as client:
        # Phase 1: Build up state
        print("\n=== Phase 1: Building State ===")
        for i in range(1, 4):
            result = await client.call_tool("increment_counter", {})
            print(f"  Counter: {result.data['counter']} on {result.data['instance']}")
        
        result = await client.call_tool("add_note", {"note": "before restart"})
        print(f"  Added note, total: {result.data['notes_count']}")
        
        # Phase 2: Restart all MCP servers
        print("\n=== Phase 2: Restarting MCP Servers ===")
        print("  Stopping mcp-server-1, mcp-server-2, mcp-server-3...")
        proc = subprocess.run(
            ["docker", "compose", "restart", "mcp-server-1", "mcp-server-2", "mcp-server-3"],
            capture_output=True, text=True
        )
        if proc.returncode != 0:
            print(f"  docker compose restart failed:\n{proc.stderr}")
            raise RuntimeError("docker compose restart failed")
        print("  Waiting for servers to come back up...")
        async with httpx.AsyncClient() as http:
            for i in range(30):
                try:
                    resp = await http.get("http://localhost:8080/health", timeout=2)
                    if resp.status_code == 200:
                        print(f"  Health check passed after {i + 1}s")
                        break
                except httpx.HTTPError:
                    pass
                await asyncio.sleep(1)
            else:
                raise RuntimeError("Servers did not come back up within 30s")
        
        # Phase 3: Continue using client (should auto-reconnect and resume)
        print("\n=== Phase 3: Resuming After Restart ===")
        result = await client.call_tool("get_counter", {})
        print(f"  Counter after restart: {result.data['counter']} on {result.data['instance']}")
        assert result.data['counter'] == 3, f"Expected counter=3, got {result.data['counter']}"
        
        result = await client.call_tool("list_notes", {})
        print(f"  Notes after restart: {result.data['notes']}")
        assert "before restart" in result.data['notes'], "Note should survive restart"
        
        # Phase 4: Continue incrementing
        print("\n=== Phase 4: Continuing Operations ===")
        result = await client.call_tool("increment_counter", {})
        print(f"  Counter continues: {result.data['counter']} on {result.data['instance']}")
        assert result.data['counter'] == 4, f"Expected counter=4, got {result.data['counter']}"
        
        result = await client.call_tool("add_note", {"note": "after restart"})
        print(f"  Added note, total: {result.data['notes_count']}")
        
        result = await client.call_tool("list_notes", {})
        print(f"  Final notes: {result.data['notes']}")
        assert len(result.data['notes']) == 2, f"Expected 2 notes, got {len(result.data['notes'])}"
        
        print("\n" + "=" * 50)
        print("✓ Resilience test passed!")
        print("  State survived container restart via Redis + session resumption")


if __name__ == "__main__":
    try:
        asyncio.run(test_resilience_with_restart())
    except Exception as e:
        print(f"\n✗ Test failed: {e}")
        sys.exit(1)
