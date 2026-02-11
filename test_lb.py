"""Automated tests for MCP load balancing with HAProxy sticky sessions."""

import json
import subprocess
import sys
import time

import httpx

HAPROXY_URL = "http://localhost:8080/mcp"


def parse_sse_json(response: httpx.Response) -> dict | None:
    """Parse a JSON-RPC message from an SSE response body.

    FastMCP returns SSE-formatted responses with lines like:
        event: message
        data: {"jsonrpc": "2.0", ...}
    """
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        return response.json()

    # Parse SSE: find the last 'data:' line
    for line in response.text.strip().splitlines():
        if line.startswith("data: "):
            return json.loads(line[6:])
    return None


def mcp_request(client: httpx.Client, method: str, params: dict | None = None, req_id: int | None = None) -> httpx.Response:
    """Send a JSON-RPC request to the MCP server through HAProxy."""
    body: dict = {
        "jsonrpc": "2.0",
        "method": method,
    }
    if req_id is not None:
        body["id"] = req_id
    if params is not None:
        body["params"] = params

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    return client.post(HAPROXY_URL, json=body, headers=headers)


def initialize_session(client: httpx.Client) -> str:
    """Initialize an MCP session and return the session ID."""
    resp = mcp_request(
        client,
        "initialize",
        params={
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test-lb-client", "version": "1.0.0"},
        },
        req_id=1,
    )
    assert resp.status_code == 200, f"Initialize failed: {resp.status_code} {resp.text}"
    session_id = resp.headers.get("mcp-session-id")
    assert session_id, "No mcp-session-id in response headers"

    # Set session header for all subsequent requests
    client.headers["mcp-session-id"] = session_id

    # Send initialized notification
    mcp_request(client, "notifications/initialized")

    return session_id


def call_tool(client: httpx.Client, tool_name: str, arguments: dict | None = None, req_id: int = 2) -> dict:
    """Call an MCP tool and return the parsed result content."""
    resp = mcp_request(
        client,
        "tools/call",
        params={"name": tool_name, "arguments": arguments or {}},
        req_id=req_id,
    )
    assert resp.status_code == 200, f"Tool call failed: {resp.status_code} {resp.text}"

    data = parse_sse_json(resp)
    assert data, f"Could not parse response: {resp.text[:200]}"
    assert "result" in data, f"No result in response: {data}"

    content = data["result"]["content"]
    assert len(content) > 0, "Empty content"
    return json.loads(content[0]["text"])


def test_sticky_sessions():
    """Test that a session is always routed to the same backend."""
    print("=== Test: Sticky Sessions ===")

    with httpx.Client(timeout=30) as client:
        session_id = initialize_session(client)
        print(f"  Session ID: {session_id}")

        instance = None
        for i in range(1, 11):
            result = call_tool(client, "increment_counter", req_id=i + 10)
            assert result["counter"] == i, f"Expected counter={i}, got {result['counter']}"

            if instance is None:
                instance = result["instance"]
                print(f"  Routed to: {instance}")
            else:
                assert result["instance"] == instance, (
                    f"Session routed to different backend! Expected {instance}, got {result['instance']}"
                )

        print(f"  Counter reached {result['counter']} on {instance} - stickiness confirmed!")
    print("  PASSED\n")


def test_distribution():
    """Test that different sessions are distributed across backends."""
    print("=== Test: Distribution Across Backends ===")

    instances = set()
    for i in range(10):
        with httpx.Client(timeout=30) as client:
            initialize_session(client)
            result = call_tool(client, "get_server_info", req_id=100 + i)
            instances.add(result["instance"])

    print(f"  Sessions distributed across: {instances}")
    assert len(instances) >= 2, f"Expected at least 2 backends, got {len(instances)}: {instances}"
    print("  PASSED\n")


def test_session_state_isolation():
    """Test that different sessions have independent state."""
    print("=== Test: Session State Isolation ===")

    with httpx.Client(timeout=30) as client_a, httpx.Client(timeout=30) as client_b:
        initialize_session(client_a)
        initialize_session(client_b)

        # Increment counter on session A 3 times
        for i in range(1, 4):
            result = call_tool(client_a, "increment_counter", req_id=200 + i)
            assert result["counter"] == i

        # Session B counter should still be 0
        result_b = call_tool(client_b, "get_counter", req_id=210)
        assert result_b["counter"] == 0, f"Session B counter should be 0, got {result_b['counter']}"

        # Add notes on session A
        call_tool(client_a, "add_note", {"note": "session A note"}, req_id=220)

        # Session B notes should be empty
        result_b_notes = call_tool(client_b, "list_notes", req_id=221)
        assert result_b_notes["notes"] == [], f"Session B notes should be empty, got {result_b_notes['notes']}"

    print("  Session state is properly isolated")
    print("  PASSED\n")


def test_backend_failure():
    """Test that session state in Redis survives a backend crash and can be recovered."""
    print("=== Test: Backend Failure - State Recovery ===")

    with httpx.Client(timeout=30) as client:
        old_session_id = initialize_session(client)
        result = call_tool(client, "get_server_info", req_id=300)
        original_instance = result["instance"]
        print(f"  Session {old_session_id[:12]}... on: {original_instance}")

        # Build up state: counter=3, one note
        for i in range(1, 4):
            call_tool(client, "increment_counter", req_id=300 + i)
        call_tool(client, "add_note", {"note": "survive-crash"}, req_id=304)

        # Verify state before crash
        counter_before = call_tool(client, "get_counter", req_id=305)
        assert counter_before["counter"] == 3, f"Expected counter=3, got {counter_before['counter']}"
        notes_before = call_tool(client, "list_notes", req_id=306)
        assert notes_before["notes"] == ["survive-crash"]
        print(f"  State before crash: counter=3, notes={notes_before['notes']}")

        # Stop the backend container
        service = original_instance
        print(f"  Stopping {service}...")
        subprocess.run(["docker", "compose", "stop", service], check=True, capture_output=True)

        # Wait for HAProxy health check to detect failure
        time.sleep(8)

    # New client + new MCP session (old session is gone with the crashed process)
    # HAProxy redispatches to a healthy backend
    with httpx.Client(timeout=30) as client2:
        new_session_id = initialize_session(client2)
        info = call_tool(client2, "get_server_info", req_id=310)
        new_instance = info["instance"]
        print(f"  New session {new_session_id[:12]}... on: {new_instance}")
        assert new_instance != original_instance, "Should be routed to a different backend"

        # New session starts fresh (counter=0)
        fresh = call_tool(client2, "get_counter", req_id=311)
        assert fresh["counter"] == 0, f"New session should start at 0, got {fresh['counter']}"

        # Resume old session state via resume_session tool
        resume = call_tool(client2, "resume_session", {"old_session_id": old_session_id}, req_id=312)
        assert resume["status"] == "resumed", f"Expected resumed, got {resume['status']}"
        assert resume["keys_copied"] == 2, f"Expected 2 keys copied (counter+notes), got {resume['keys_copied']}"
        print(f"  Resumed {resume['keys_copied']} keys from old session")

        # Verify recovered state
        counter_after = call_tool(client2, "get_counter", req_id=313)
        assert counter_after["counter"] == 3, f"Expected counter=3 after resume, got {counter_after['counter']}"

        notes_after = call_tool(client2, "list_notes", req_id=314)
        assert notes_after["notes"] == ["survive-crash"], f"Expected notes after resume, got {notes_after['notes']}"

        # Counter continues from where it left off
        inc = call_tool(client2, "increment_counter", req_id=315)
        assert inc["counter"] == 4, f"Expected counter=4, got {inc['counter']}"
        print(f"  Counter continues: {inc['counter']} (state fully recovered)")

    # Restart the stopped backend
    print(f"  Restarting {service}...")
    subprocess.run(["docker", "compose", "start", service], check=True, capture_output=True)
    time.sleep(5)

    print("  PASSED\n")


if __name__ == "__main__":
    print("MCP Load Balancing Tests")
    print("=" * 50)
    print(f"Target: {HAPROXY_URL}\n")

    # Check HAProxy is reachable
    try:
        resp = httpx.get("http://localhost:8080/health", timeout=5)
        print(f"HAProxy health: {resp.status_code}\n")
    except httpx.ConnectError:
        print("ERROR: Cannot reach HAProxy at localhost:8080")
        print("Run: docker compose up -d")
        sys.exit(1)

    test_sticky_sessions()
    test_distribution()
    test_session_state_isolation()
    test_backend_failure()

    print("=" * 50)
    print("All tests passed!")
