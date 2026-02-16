"""Automated tests for MCP load balancing with HAProxy sticky sessions."""

import json
import subprocess
import sys
import threading
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


def parse_all_sse_events(response: httpx.Response) -> list[dict]:
    """Parse ALL JSON-RPC messages from an SSE response body.

    Returns every 'data:' line as a parsed dict — includes notifications
    (progress, log messages) and the final result.
    """
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        return [response.json()]

    events = []
    for line in response.text.strip().splitlines():
        if line.startswith("data: "):
            try:
                events.append(json.loads(line[6:]))
            except json.JSONDecodeError:
                pass
    return events


def find_notifications(events: list[dict], method: str) -> list[dict]:
    """Filter SSE events to only JSON-RPC notifications matching the given method."""
    return [e for e in events if e.get("method") == method]


def find_result(events: list[dict], req_id: int) -> dict | None:
    """Find the JSON-RPC result event matching the given request id."""
    for e in events:
        if e.get("id") == req_id and "result" in e:
            return e
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


def test_health_endpoint():
    """Test that GET /health returns 200 with status and instance info."""
    print("=== Test: Health Endpoint ===")

    resp = httpx.get("http://localhost:8080/health", timeout=10)
    assert resp.status_code == 200, f"Health check failed: {resp.status_code} {resp.text}"

    data = resp.json()
    assert data["status"] == "ok", f"Expected status=ok, got {data['status']}"
    assert "instance" in data, f"No instance in health response: {data}"
    print(f"  Health OK from {data['instance']}")
    print("  PASSED\n")


def test_get_status():
    """Test that get_status returns instance, uptime, active_sessions, timestamp."""
    print("=== Test: get_status Tool ===")

    with httpx.Client(timeout=30) as client:
        initialize_session(client)
        result = call_tool(client, "get_status", req_id=400)

        assert "instance" in result, f"Missing 'instance': {result}"
        assert "uptime_seconds" in result, f"Missing 'uptime_seconds': {result}"
        assert isinstance(result["uptime_seconds"], (int, float)), f"uptime not numeric: {result}"
        assert result["uptime_seconds"] > 0, f"uptime should be > 0: {result}"
        assert "active_sessions" in result, f"Missing 'active_sessions': {result}"
        assert isinstance(result["active_sessions"], int), f"active_sessions not int: {result}"
        assert "timestamp" in result, f"Missing 'timestamp': {result}"

        print(f"  Instance: {result['instance']}, uptime: {result['uptime_seconds']}s, "
              f"sessions: {result['active_sessions']}")
    print("  PASSED\n")


def test_resume_session_same_id():
    """Test that resume_session with the current session ID returns same_session."""
    print("=== Test: resume_session Same ID ===")

    with httpx.Client(timeout=30) as client:
        session_id = initialize_session(client)

        result = call_tool(client, "resume_session", {"old_session_id": session_id}, req_id=410)
        assert result["status"] == "same_session", f"Expected same_session, got {result['status']}"
        print(f"  Correctly returned same_session for own session ID")
    print("  PASSED\n")


def test_notes_crud():
    """Test full add_note + list_notes cycle with multiple notes."""
    print("=== Test: Notes CRUD ===")

    with httpx.Client(timeout=30) as client:
        initialize_session(client)

        # Start with empty notes
        result = call_tool(client, "list_notes", req_id=420)
        assert result["notes"] == [], f"Expected empty notes, got {result['notes']}"

        # Add multiple notes
        notes_to_add = ["first note", "second note", "third note"]
        for i, note in enumerate(notes_to_add):
            result = call_tool(client, "add_note", {"note": note}, req_id=421 + i)
            assert result["notes_count"] == i + 1, f"Expected count={i+1}, got {result['notes_count']}"

        # List and verify ordering
        result = call_tool(client, "list_notes", req_id=430)
        assert result["notes"] == notes_to_add, f"Expected {notes_to_add}, got {result['notes']}"
        print(f"  Notes after add: {result['notes']}")
    print("  PASSED\n")


def test_analyze_data_with_notifications():
    """Test analyze_data emits progress and log notifications, returns correct result."""
    print("=== Test: analyze_data with Notifications ===")

    with httpx.Client(timeout=60) as client:
        session_id = initialize_session(client)

        req_id = 440
        resp = mcp_request(
            client,
            "tools/call",
            params={"name": "analyze_data", "arguments": {"num_items": 3}},
            req_id=req_id,
        )
        assert resp.status_code == 200, f"analyze_data failed: {resp.status_code} {resp.text}"

        events = parse_all_sse_events(resp)
        assert len(events) > 1, f"Expected multiple SSE events (notifications + result), got {len(events)}"

        # Check for log/message notifications (ctx.info / ctx.debug)
        message_notifs = find_notifications(events, "notifications/message")
        print(f"  Message notifications: {len(message_notifs)}")
        assert len(message_notifs) > 0, "Expected at least one message notification"

        # Verify some messages are info-level and some are debug-level
        levels = {n["params"]["level"] for n in message_notifs}
        print(f"  Log levels seen: {levels}")
        assert "info" in levels, f"Expected info-level logs, got levels: {levels}"
        assert "debug" in levels, f"Expected debug-level logs, got levels: {levels}"

        # Verify message content (uses 'msg' key in data)
        msgs = [n["params"]["data"]["msg"] for n in message_notifs]
        assert any("Starting analysis" in m for m in msgs), f"Missing 'Starting analysis' log: {msgs}"
        assert any("Analysis complete" in m for m in msgs), f"Missing 'Analysis complete' log: {msgs}"

        # Check the final result
        result_event = find_result(events, req_id)
        assert result_event, f"No result event found for req_id={req_id}"
        content = result_event["result"]["content"]
        result = json.loads(content[0]["text"])

        assert result["items_processed"] == 3, f"Expected 3 items, got {result['items_processed']}"
        expected_score = 1 * 1.5 + 2 * 1.5 + 3 * 1.5  # 9.0
        assert result["total_score"] == expected_score, f"Expected score={expected_score}, got {result['total_score']}"
        print(f"  Result: items={result['items_processed']}, score={result['total_score']}")

        # Verify result was stored in session via session_summary resource
        res_resp = mcp_request(
            client,
            "resources/read",
            params={"uri": f"resource://session/{session_id}/summary"},
            req_id=441,
        )
        assert res_resp.status_code == 200, f"Resource read failed: {res_resp.status_code} {res_resp.text}"
        res_data = parse_sse_json(res_resp)
        assert res_data and "result" in res_data, f"No result in resource response: {res_data}"
        resource_contents = res_data["result"]["contents"]
        assert len(resource_contents) > 0, "Empty resource contents"
        summary = json.loads(resource_contents[0]["text"])
        assert summary["analysis_result"]["items_processed"] == 3, (
            f"Session summary missing analysis: {summary}"
        )
        print(f"  Session summary confirms analysis stored")
    print("  PASSED\n")


def test_session_summary_resource():
    """Test reading the session summary resource with counter and notes."""
    print("=== Test: Session Summary Resource ===")

    with httpx.Client(timeout=30) as client:
        session_id = initialize_session(client)

        # Set up some state
        for i in range(1, 4):
            call_tool(client, "increment_counter", req_id=450 + i)
        call_tool(client, "add_note", {"note": "hello"}, req_id=460)
        call_tool(client, "add_note", {"note": "world"}, req_id=461)

        # Read the resource
        resp = mcp_request(
            client,
            "resources/read",
            params={"uri": f"resource://session/{session_id}/summary"},
            req_id=470,
        )
        assert resp.status_code == 200, f"Resource read failed: {resp.status_code} {resp.text}"

        data = parse_sse_json(resp)
        assert data and "result" in data, f"No result in resource response: {data}"
        contents = data["result"]["contents"]
        assert len(contents) > 0, "Empty resource contents"

        summary = json.loads(contents[0]["text"])
        assert summary["session_id"] == session_id, f"Wrong session_id: {summary['session_id']}"
        assert summary["counter"] == 3, f"Expected counter=3, got {summary['counter']}"
        assert summary["notes"] == ["hello", "world"], f"Expected notes, got {summary['notes']}"
        assert "instance" in summary, f"Missing instance: {summary}"

        print(f"  Summary: counter={summary['counter']}, notes={summary['notes']}, "
              f"instance={summary['instance']}")
    print("  PASSED\n")


def test_watch_counter_with_notifications():
    """Test watch_counter detects changes and emits progress + log notifications."""
    print("=== Test: watch_counter with Notifications ===")

    with httpx.Client(timeout=60) as client:
        session_id = initialize_session(client)

        # Set initial counter
        call_tool(client, "increment_counter", req_id=480)

        # We'll use a separate thread to increment the counter while watch runs.
        # Need a separate client with the same session header.
        session_header = client.headers["mcp-session-id"]
        increment_errors = []

        def increment_in_background():
            """Increment counter a few times with delays."""
            time.sleep(1.5)  # Let the watcher start
            try:
                with httpx.Client(timeout=30) as inc_client:
                    inc_client.headers["mcp-session-id"] = session_header
                    inc_client.headers["Content-Type"] = "application/json"
                    inc_client.headers["Accept"] = "application/json, text/event-stream"
                    for i in range(3):
                        time.sleep(0.8)
                        call_tool(inc_client, "increment_counter", req_id=490 + i)
            except Exception as e:
                increment_errors.append(e)

        # Start background incrementer
        bg_thread = threading.Thread(target=increment_in_background, daemon=True)
        bg_thread.start()

        # Start the watcher (short duration)
        req_id = 485
        resp = mcp_request(
            client,
            "tools/call",
            params={"name": "watch_counter", "arguments": {"duration_seconds": 5}},
            req_id=req_id,
        )
        assert resp.status_code == 200, f"watch_counter failed: {resp.status_code} {resp.text}"

        bg_thread.join(timeout=10)
        assert not increment_errors, f"Background increment errors: {increment_errors}"

        events = parse_all_sse_events(resp)
        assert len(events) > 1, f"Expected multiple SSE events, got {len(events)}"

        # Check log notifications
        message_notifs = find_notifications(events, "notifications/message")
        print(f"  Message notifications: {len(message_notifs)}")
        assert len(message_notifs) > 0, "Expected message notifications from watch_counter"

        # Check for change detection messages (uses 'msg' key in data)
        change_messages = [
            n for n in message_notifs
            if "changed" in n.get("params", {}).get("data", {}).get("msg", "").lower()
        ]
        print(f"  Change detection messages: {len(change_messages)}")

        # Check the final result
        result_event = find_result(events, req_id)
        assert result_event, f"No result event found for req_id={req_id}"
        content = result_event["result"]["content"]
        result = json.loads(content[0]["text"])

        print(f"  Result: {result['total_changes']} change(s) detected")
        assert result["total_changes"] > 0, f"Expected at least 1 change, got {result['total_changes']}"
        assert len(result["changes"]) == result["total_changes"]
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
    test_health_endpoint()
    test_get_status()
    test_resume_session_same_id()
    test_notes_crud()
    test_analyze_data_with_notifications()
    test_session_summary_resource()
    test_watch_counter_with_notifications()
    test_backend_failure()

    print("=" * 50)
    print("All tests passed!")
