# High Availability for Stateful MCP Servers

MCP (Model Context Protocol) servers are inherently stateful -- each client-server session maintains state tied to a `mcp-session-id` header. This project demonstrates how to use **HAProxy's stick-table** feature to provide session-affine routing for MCP's Streamable HTTP transport, with **Redis-backed session state** for durability across server restarts and failovers.

## Architecture

```mermaid
graph TD
    A[MCP Clients] --> B[HAProxy<br/>:8080 proxy / :8404 stats<br/>sticky sessions + redispatch]
    B --> C[mcp-server-1<br/>:8000]
    B --> D[mcp-server-2<br/>:8000]
    B --> E[mcp-server-3<br/>:8000]
    C --> F[Redis 7<br/>session state]
    D --> F
    E --> F

    style B fill:#e1f5ff
    style F fill:#ffe1e1
```

- **3 MCP server instances** running a stateful FastMCP server (session-scoped counters and notes)
- **Redis** for externalized session state -- survives server restarts and enables failover
- **HAProxy** in front, using a stick-table to map `mcp-session-id` headers to backends, with `option redispatch` for failover when a backend is down
- All orchestrated via **Docker Compose**

## Failure Recovery Flow

When a backend crashes or is stopped, the system recovers gracefully thanks to externalized state in Redis:

```mermaid
sequenceDiagram
    participant Client
    participant HAProxy
    participant Server2 as mcp-server-2
    participant Server1 as mcp-server-1
    participant Redis

    Note over Client,Redis: 1. Normal Operation: Session pinned to mcp-server-2 via stick-table
    Client->>HAProxy: increment_counter<br/>[session: abc123]
    HAProxy->>Server2: [stick lookup: abc123→srv-2]
    Server2->>Redis: INCR abc123
    Redis-->>Server2: 5
    Server2-->>HAProxy: counter=5
    HAProxy-->>Client: counter=5

    Note over Server2: 2. Backend Failure: mcp-server-2 crashes
    Server2-xServer2: X (crashed)
    Note over HAProxy,Server2: health checks fail × 3<br/>→ mark DOWN

    Note over Client,Redis: 3. Client Re-initializes: Connection lost, start new session
    Client->>HAProxy: initialize<br/>[no session ID]
    Note over HAProxy: [leastconn] pick healthy
    HAProxy->>Server1: initialize
    Server1-->>HAProxy: [session: xyz789]
    Note over HAProxy: [store xyz789→srv-1]
    HAProxy-->>Client: session: xyz789

    Note over Client,Redis: 4. State Recovery: Copy old session state via resume_session
    Client->>HAProxy: resume_session<br/>[session: xyz789]<br/>[old: abc123]
    HAProxy->>Server1: resume_session
    Server1->>Redis: SCAN abc123:*
    Redis-->>Server1: [counter,...]
    Server1->>Redis: COPY to xyz789
    Redis-->>Server1: OK (2 keys)
    Server1-->>HAProxy: keys_copied: 2
    HAProxy-->>Client: keys_copied: 2

    Note over Client,Redis: 5. Resumed: Continue with recovered state
    Client->>HAProxy: increment_counter<br/>[session: xyz789]
    HAProxy->>Server1: [stick lookup: xyz789→srv-1]
    Server1->>Redis: INCR xyz789
    Redis-->>Server1: 6
    Server1-->>HAProxy: counter=6
    HAProxy-->>Client: counter=6 (continues!)
```

**Key recovery steps:**

1. **Normal operation**: Stick-table routes `abc123` → `mcp-server-2`, state in Redis
2. **Failure detected**: Health checks fail (3 × 5s), HAProxy marks `mcp-server-2` as DOWN
3. **Redispatch**: Client re-initializes, `leastconn` picks healthy `mcp-server-1`, new session `xyz789`
4. **State recovery**: `resume_session(abc123)` copies all Redis keys from old to new session
5. **Continuation**: Counter increments 5→6, application continues seamlessly

**Key points:**
- `option redispatch` prevents 503 errors, automatically reroutes to healthy backends
- Old session state persists in Redis (30-minute TTL)
- `resume_session` copies state: `abc123:* → xyz789:*`
- Zero data loss, only brief connection interruption

## How the Sticky Routing Works

The key is in `haproxy/haproxy.cfg`:

```haproxy
backend mcp_servers
    balance leastconn
    stick-table type string len 64 size 100k expire 30m

    # First request (no session ID): leastconn picks the backend with fewest active connections.
    # Learn the session ID from the response header and bind it to this backend.
    stick store-response res.hdr(mcp-session-id)

    # Subsequent requests: match session ID from request header → same backend.
    stick match req.hdr(mcp-session-id)
```

1. **First request** (`initialize`): Client has no session ID. HAProxy routes to the backend with the fewest active connections (load-aware). The backend responds with a `mcp-session-id` header. HAProxy stores the mapping: `session-id → backend`.
2. **Subsequent requests**: Client sends `mcp-session-id` in the request header. HAProxy looks it up in the stick-table and routes to the same backend.
3. **Backend failure**: With `option redispatch` enabled, HAProxy reroutes to a healthy backend instead of returning 503. Session state is preserved in Redis and can be recovered via `resume_session`.

## Prerequisites

- [Docker](https://docs.docker.com/get-docker/) and Docker Compose
- [uv](https://docs.astral.sh/uv/) (for running tests locally)
- [curl](https://curl.se/) (for manual verification)

## Quick Start

### 1. Start the Stack

```bash
docker compose up -d --build
```

This builds 3 MCP server images and starts them along with HAProxy.

### 2. Verify Containers Are Running

```bash
docker compose ps
```

You should see 5 containers: `redis`, `mcp-server-1`, `mcp-server-2`, `mcp-server-3`, and `haproxy`.

### 3. Check HAProxy Stats

Open [http://localhost:8404/stats](http://localhost:8404/stats) in your browser. You should see all 3 backends with status **UP** (green).

### 4. Check Health Endpoint

```bash
curl http://localhost:8080/health
```

Expected output (backend will vary):

```json
{"status":"ok","instance":"mcp-server-1"}
```

The health endpoint also checks Redis connectivity. If Redis is unreachable, it returns `503` with `"status":"degraded"`.

## Manual Verification with curl

### Initialize a Session

```bash
# Send initialize request and capture response headers
curl -s -D - http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2025-11-25",
      "capabilities": {},
      "clientInfo": {"name": "curl-test", "version": "1.0.0"}
    }
  }'
```

Note the `mcp-session-id` header in the response. Copy it for subsequent requests.

### Test Stickiness

Using the session ID from above, call `increment_counter` multiple times:

```bash
SESSION_ID="<paste-session-id-here>"

# First increment → counter: 1
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SESSION_ID" \
  -d '{
    "jsonrpc": "2.0", "id": 2,
    "method": "tools/call",
    "params": {"name": "increment_counter", "arguments": {}}
  }'

# Second increment → counter: 2 (same instance!)
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $SESSION_ID" \
  -d '{
    "jsonrpc": "2.0", "id": 3,
    "method": "tools/call",
    "params": {"name": "increment_counter", "arguments": {}}
  }'
```

If stickiness works, the counter increments sequentially and the `instance` field stays the same across calls.

## Automated Tests

Install dependencies and run the test suite:

```bash
uv sync
uv run python test_lb.py
```

Expected output:

```
MCP Load Balancing Tests
==================================================
Target: http://localhost:8080/mcp

HAProxy health: 200

=== Test: Sticky Sessions ===
  Session ID: d528c59430d14bd1be7f711f910926ff
  Routed to: mcp-server-3
  Counter reached 10 on mcp-server-3 - stickiness confirmed!
  PASSED

=== Test: Distribution Across Backends ===
  Sessions distributed across: {'mcp-server-2', 'mcp-server-1', 'mcp-server-3'}
  PASSED

=== Test: Session State Isolation ===
  Session state is properly isolated
  PASSED

=== Test: Health Endpoint ===
  Health OK from mcp-server-2
  PASSED

=== Test: get_status Tool ===
  Instance: mcp-server-1, uptime: 60.4s, sessions: 7
  PASSED

=== Test: resume_session Same ID ===
  Correctly returned same_session for own session ID
  PASSED

=== Test: Notes CRUD ===
  Notes after add: ['first note', 'second note', 'third note']
  PASSED

=== Test: analyze_data with Notifications ===
  Message notifications: 7
  Log levels seen: {'info', 'debug'}
  Result: items=3, score=9.0
  Session summary confirms analysis stored
  PASSED

=== Test: Session Summary Resource ===
  Summary: counter=3, notes=['hello', 'world'], instance=mcp-server-3
  PASSED

=== Test: watch_counter with Notifications ===
  Message notifications: 5
  Change detection messages: 3
  Result: 3 change(s) detected
  PASSED

=== Test: Backend Failure - State Recovery ===
  Session 381eb66a18a4... on: mcp-server-1
  State before crash: counter=3, notes=['survive-crash']
  Stopping mcp-server-1...
  New session db123608fb3b... on: mcp-server-3
  Resumed 2 keys from old session
  Counter continues: 4 (state fully recovered)
  Restarting mcp-server-1...
  PASSED

==================================================
All tests passed!
```

### What the Tests Verify

| Test | What it proves |
|------|----------------|
| **Sticky Sessions** | Counter increments 1–10 on the same backend. Session ID always routes to the same server. |
| **Distribution** | 10 independent sessions spread across at least 2 of 3 backends (leastconn distributes by active connections). |
| **Session State Isolation** | Two concurrent sessions have independent counters and notes. |
| **Health Endpoint** | `GET /health` returns 200 with `status: ok` and instance ID; also checks Redis connectivity. |
| **get_status** | Tool returns instance, uptime, active session count, and timestamp. |
| **resume_session Same ID** | Calling `resume_session` with the current session's own ID returns `{"status": "same_session"}`. |
| **Notes CRUD** | Full add/list cycle with multiple notes; verifies insertion order. |
| **analyze_data with Notifications** | SSE stream contains info- and debug-level log notifications alongside the final result; result is stored in session and readable via the resource endpoint. |
| **Session Summary Resource** | `resources/read` on `resource://session/{id}/summary` returns correct counter, notes, and instance. |
| **watch_counter with Notifications** | Concurrent counter increments (background thread) are detected by the watcher; change notifications appear in the SSE stream. |
| **Backend Failure - State Recovery** | When a sticky backend is stopped, HAProxy redispatches; `resume_session` copies Redis keys to the new session and the counter continues from where it left off. |

## Testing Backend Failure and Recovery Manually

```bash
# 1. Note which backend your session is on (from the curl test above)
#    and save the session ID:
OLD_SESSION_ID="$SESSION_ID"

# 2. Stop that backend:
docker compose stop mcp-server-2

# 3. Wait for HAProxy health check (fall 3 × inter 5s = ~15s)
sleep 16

# 4. Re-initialize a new session (HAProxy redispatches to a healthy backend):
curl -s -D - http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{
    "jsonrpc": "2.0", "id": 1,
    "method": "initialize",
    "params": {
      "protocolVersion": "2025-11-25",
      "capabilities": {},
      "clientInfo": {"name": "curl-test", "version": "1.0.0"}
    }
  }'

# Copy the new mcp-session-id from the response:
NEW_SESSION_ID="<paste-new-session-id>"

# 5. Resume state from old session:
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $NEW_SESSION_ID" \
  -d '{"jsonrpc":"2.0","id":10,"method":"tools/call","params":{"name":"resume_session","arguments":{"old_session_id":"'"$OLD_SESSION_ID"'"}}}'

# 6. Verify counter is recovered:
curl -s http://localhost:8080/mcp \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -H "mcp-session-id: $NEW_SESSION_ID" \
  -d '{"jsonrpc":"2.0","id":11,"method":"tools/call","params":{"name":"get_counter","arguments":{}}}'

# 7. Restart the backend
docker compose start mcp-server-2
```

## Project Structure

```
mcp-high-availability/
├── server.py              # FastMCP server — tools, resources, health endpoint
├── session_store.py       # SessionStore ABC + Session helper (JSON serde, TTL)
├── stores/
│   ├── redis_store.py     # Redis-backed store (only file that imports redis.asyncio)
│   └── memory_store.py    # Dict-backed store with lazy TTL expiry (local dev)
├── pyproject.toml         # Python project config (fastmcp, httpx, redis)
├── .python-version        # Python 3.12
├── Dockerfile             # Server container image
├── docker-compose.yaml    # Redis + 3 MCP servers + HAProxy
├── haproxy/
│   └── haproxy.cfg        # HAProxy config with sticky sessions + redispatch
├── client.http            # Step-by-step manual testing via VS Code REST Client
├── test_lb.py             # Automated integration test suite
└── README.md
```

## MCP Server Tools

The server (`server.py`) exposes these tools, all returning the `instance` field to show which backend is serving:

| Tool | Description |
|------|-------------|
| `increment_counter` | Increment a session-scoped counter (proves stickiness) |
| `get_counter` | Get current counter value |
| `add_note` | Add a note to the session's note list |
| `list_notes` | List all notes for the session |
| `get_server_info` | Return instance ID and active session count |
| `get_status` | Return instance ID, uptime, active session count, and current timestamp |
| `analyze_data` | Multi-step analysis pipeline; streams progress and log notifications via SSE |
| `watch_counter` | Poll the session counter for changes and stream log notifications on each change |
| `resume_session` | Copy state from a previous session into the current one (for recovery after crash/restart) |

The server also exposes a `resource://session/{session_id}/summary` resource that returns a JSON snapshot of the session's counter, notes, and last analysis result.

## Design Decisions

### Why `leastconn` Instead of `roundrobin` or Consistent Hashing?

**The problem:** Sessions have unequal lifetimes and request rates. Round-robin assigns sessions evenly at creation time, but over time one backend can accumulate many long-lived or high-traffic sessions while another sits idle.

**Why not consistent hashing?** Consistent hashing (`hash(session-id) → backend`) provides deterministic routing, but it's blind to actual backend load. Once a session hashes to a node, it's pinned there regardless of whether that backend is overloaded. It's designed for cache locality in stateless systems, not for load-aware routing in stateful ones.

**Why `leastconn` + stick-table?** This combines the best of both worlds:
- **Load-aware initial assignment:** New sessions go to the backend with the fewest active connections
- **Session affinity:** Stick-table pins existing sessions to their assigned backend (O(1) hash table lookup)
- **Externalized state:** Redis means any backend can serve any session after failover

The stick-table lookup (~50-100ns) is negligible compared to Redis I/O (~200-1000µs), so performance is dominated by state storage, not routing strategy.

### Key Findings

- **`stick store-response res.hdr()` works with SSE responses.** HAProxy processes HTTP response headers before the body streams, so it captures the `mcp-session-id` header even though the body is `text/event-stream`.
- **Redis externalizes session state.** Counters use `INCR` (atomic, no read-modify-write races) and notes use `RPUSH`/`LRANGE` (native list operations). All keys have a 30-minute sliding TTL matching the HAProxy stick-table expiry.
- **`option redispatch` enables failover.** When a sticky backend goes down, HAProxy reroutes to a healthy backend instead of returning 503. Since state is in Redis, any backend can serve any session.
- **MCP session IDs are ephemeral.** When a backend crashes, the MCP protocol session is lost and the client must re-initialize (getting a new `mcp-session-id`). The `resume_session` tool bridges old and new sessions by copying Redis keys.
- **`ctx.session_id` from FastMCP** (context.py) provides the `mcp-session-id` header value -- perfect for keying state in Redis.
- **Timeouts matter for SSE.** The `timeout client`/`timeout server` values must be high (300s) to support long-lived SSE streams. The `timeout tunnel` (600s) covers upgraded connections.

## Cleanup

```bash
docker compose down
```
