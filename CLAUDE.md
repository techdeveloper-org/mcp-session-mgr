# mcp-session-mgr — Claude Project Context

**Type:** FastMCP Server
**Transport:** stdio
**Python:** 3.8+

---

## What This Server Does

Session lifecycle management for multi-step Claude Code workflows. Creates sessions, accumulates task/tool/file activity, finalizes with summaries, supports tagging for categorization, and inter-session linking for chaining related sessions. Generates comprehensive statistics.

---

## Entry Point

```
server.py
```

Run via `python server.py` — communicates over stdio using the MCP protocol.

---

## Available Tools

- `session_create` — Create a new session with task description and metadata
- `session_accumulate` — Accumulate activity: tool calls, file edits, task steps
- `session_finalize` — Finalize session and generate summary with statistics
- `session_tag` — Add tags to a session for categorization and search
- `session_link` — Link sessions for chaining (parent -> child, prerequisite -> followup)
- `session_get_status` — Get current session status, progress, and budget estimate
- `session_list` — List sessions with filters (tag, date, status, project)
- `session_export_summary` — Export full session summary as structured JSON

---

## Shared Utilities (in this repo)

- `base/` — Shared MCP infrastructure package (response builder, decorators, persistence, clients)
- `mcp_errors.py` — Structured error response helpers
- `input_validator.py` — Null-byte strip, length limits, prompt injection detection
- `rate_limiter.py` — Token bucket rate limiter (enable via ENABLE_RATE_LIMITING=1)

---

## Environment Variables

- `CLAUDE_SESSION_DIR` — Session storage directory (default: ~/.claude/sessions)
- `CLAUDE_SESSION_ID` — Current session ID (auto-generated if not set)
- `CLAUDE_PROJECT` — Project identifier for session grouping

---

## Development

### Running locally

```bash
# Install deps
pip install -r requirements.txt

# Run the MCP server (stdio mode)
python server.py
```

### Testing a tool call manually

```python
import subprocess, json

proc = subprocess.Popen(
    ["python", "server.py"],
    stdin=subprocess.PIPE,
    stdout=subprocess.PIPE,
)
# Send MCP initialize + tool call via stdin
```

### File structure

```
mcp-session-mgr/
+-- server.py          # Main FastMCP server (entry point)
+-- base/              # Shared base package (response, decorators, persistence, clients)
+-- mcp_errors.py      # Error helpers
+-- input_validator.py # Input validation
+-- rate_limiter.py    # Rate limiting
+-- requirements.txt
+-- .gitignore
+-- README.md
+-- CLAUDE.md
```

---

## Key Rules

1. Do NOT edit `base/` directly — it is a copy from `mcp-base` repo
2. To update shared utilities, edit in `mcp-base` and re-copy
3. Keep `server.py` as the single entry point
4. All tool handlers must use `@mcp_tool_handler` decorator for consistent error handling
5. All responses must use `success()` / `error()` / `MCPResponse` builder from `base.response`

---

**Last Updated:** 2026-03-31
