![Python 3.8+](https://img.shields.io/badge/Python-3.8%2B-blue)
![License MIT](https://img.shields.io/badge/License-MIT-green)
![Part of claude-workflow-engine](https://img.shields.io/badge/part%20of-claude--workflow--engine-orange)

# mcp-session-mgr

`mcp-session-mgr` is a FastMCP server that provides full session lifecycle management for
Claude Code workflows. It replaces ad-hoc session scripts with 14 structured MCP tools that
handle session creation, per-request data accumulation, rich summary finalization, tag-based
search, session chaining across `/clear` boundaries, and intra-session work-item tracking.
All data is persisted atomically to disk via direct file I/O with backup-and-recovery
semantics. The server communicates over stdio JSON-RPC and requires no external database.

---

## Features

- **Session creation** with auto-generated IDs (`SESSION-YYYYMMDD-HHMMSS-XXXX`) and
  automatic tag extraction from prompt text, skill names, and working-directory paths
- **Per-request accumulation** of task type, skill, complexity score, model choice,
  context-window percentage, plan-mode usage, and supplementary skill lists
- **Rich finalization** that merges accumulated data with tool-tracker stats and flow-trace
  pipeline decisions into a structured markdown summary per session
- **Session chaining** across `/clear` commands: link parent and child sessions so context
  is never lost between conversation resets
- **Tag-based search** with a persistent tag index; sessions sharing two or more tags are
  auto-related at tag time
- **Session querying** by project, keyword, and date range
- **Archival** of sessions older than a configurable threshold to a separate archive
  directory
- **Work-item tracking** inside a session with typed IDs, status transitions
  (`IN_PROGRESS`, `COMPLETED`, `FAILED`, `SKIPPED`), and arbitrary metadata
- **Corruption recovery**: every load falls back to a `.bak` file and then to a minimal
  valid recovery dict before surfacing an error

---

## Tool Reference

| Tool | Description | Key Parameters |
|------|-------------|----------------|
| `session_create` | Create a new session, generate a unique ID, register in the chain index, update the current-session pointer | `project`, `task_type`, `skill`, `prompt`, `project_cwd` |
| `session_accumulate` | Append a per-request entry to the session log; update aggregate stats (skills used, models, complexity, context %) | `session_id`, `prompt`, `task_type`, `skill`, `complexity`, `model`, `cwd`, `plan_mode`, `context_pct`, `supplementary_skills`, `standards_count`, `rules_count` |
| `session_finalize` | Merge accumulated data with tool-tracker and flow-trace files; generate a markdown summary; mark session `COMPLETED` | `session_id` |
| `session_save` | Atomically write session data of a given type to disk | `session_id`, `data_type` (`summary`/`state`/`context`), `content`, `project` |
| `session_load` | Read session data by type; returns latest state file when no `session_id` is given | `session_id`, `data_type`, `project` |
| `session_list` | List available sessions, optionally filtered by project, sorted by modification time | `project`, `limit` |
| `session_archive` | Move sessions older than N days to `_archived/` | `days_old` |
| `session_query` | Query sessions by project, date range, and keyword (searches file content) | `filters` (JSON string: `project`, `date_from`, `date_to`, `keyword`) |
| `session_link` | Link a child session to its parent for `/clear` continuity | `child_id`, `parent_id` |
| `session_tag` | Add tags to a session; auto-relate sessions that share 2 or more tags; optionally update summary text | `session_id`, `tags` (comma-separated), `summary` |
| `session_get_context` | Walk the parent chain and collect related sessions for context continuity across conversation resets | `session_id`, `max_ancestors`, `max_related` |
| `session_search_tags` | Return sessions matching any of the supplied tags, sorted by tag-match count | `tags` (comma-separated), `limit` |
| `session_add_work_item` | Add a typed, trackable work item to a session | `session_id`, `description`, `work_type`, `metadata` |
| `session_complete_work_item` | Mark a work item as `COMPLETED`, `FAILED`, or `SKIPPED` | `session_id`, `work_id`, `status` |

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/techdeveloper-org/mcp-session-mgr.git
cd mcp-session-mgr
```

### 2. Install dependencies

```bash
pip install -r requirements.txt
```

`requirements.txt` contains:

```
mcp>=1.0.0
fastmcp>=0.1.0
```

### 3. Verify the server starts

```bash
python server.py
```

The server reads from stdin and writes to stdout. No output on startup is expected and
correct — the FastMCP runtime waits for JSON-RPC messages over stdio.

### 4. Register in Claude Code

Add the following entry to `~/.claude/settings.json` under `mcpServers`:

```json
{
  "mcpServers": {
    "session-mgr": {
      "command": "python",
      "args": ["/absolute/path/to/mcp-session-mgr/server.py"],
      "env": {}
    }
  }
}
```

Replace `/absolute/path/to/mcp-session-mgr` with the actual path on your system. On
Windows use forward slashes or escaped backslashes:

```json
"args": ["C:/Users/yourname/repos/mcp-session-mgr/server.py"]
```

---

## Configuration

The server derives its storage root from `utils/path_resolver.get_config_dir()`, which
resolves to the Claude configuration directory (`~/.claude` by default). All paths below
are relative to that root.

| Path | Purpose |
|------|---------|
| `sessions/{project}/session-{id}.md` | Markdown session summaries written by `session_save` |
| `sessions/{project}/context-{id}.json` | Context snapshots written by `session_save` |
| `sessions/_archived/` | Destination for archived sessions (`session_archive`) |
| `.state/{project}.json` | Latest project state, written atomically by `session_save` |
| `.chain-index.json` | Global session chain index: parent/child links, tags, related list |
| `.current-session.json` | Pointer to the currently active session ID |
| `logs/sessions/{id}/session-summary.json` | Accumulated JSON data written by `session_accumulate` |
| `logs/sessions/{id}/session-summary.md` | Generated markdown summary written by `session_finalize` |
| `logs/sessions/{id}/flow-trace.json` | Pipeline decisions read by `session_finalize` (written by policy-enforcement server) |
| `logs/sessions/{id}/tool-tracker.jsonl` | Tool call log read by `session_finalize` (written by post-tool-tracker server) |

There are no required environment variables. The server uses no network connections and
requires no API keys.

---

## Usage Examples

### Create a new session

```json
{
  "tool": "session_create",
  "arguments": {
    "project": "claude-workflow-engine",
    "task_type": "Implementation",
    "skill": "python-backend-engineer",
    "prompt": "Add Redis caching layer to the FastAPI service",
    "project_cwd": "/Users/dev/repos/claude-workflow-engine"
  }
}
```

Response:

```json
{
  "success": true,
  "session_id": "SESSION-20260414-093015-K7PX",
  "project": "claude-workflow-engine",
  "tags": ["claude-workflow-engine", "fastapi", "implementation", "python-backend-engineer", "redis"],
  "created_at": "2026-04-14T09:30:15.421083"
}
```

### Accumulate a request entry mid-session

```json
{
  "tool": "session_accumulate",
  "arguments": {
    "session_id": "SESSION-20260414-093015-K7PX",
    "prompt": "Add Redis caching layer to the FastAPI service",
    "task_type": "Implementation",
    "skill": "python-backend-engineer",
    "complexity": 14,
    "model": "SONNET",
    "cwd": "/Users/dev/repos/claude-workflow-engine",
    "plan_mode": false,
    "context_pct": 22,
    "supplementary_skills": "redis-core,performance-optimization"
  }
}
```

Response:

```json
{
  "success": true,
  "session_id": "SESSION-20260414-093015-K7PX",
  "request_number": 1,
  "skills_used": ["python-backend-engineer"],
  "peak_context": 22
}
```

### Link sessions across a /clear boundary

```json
{
  "tool": "session_link",
  "arguments": {
    "child_id": "SESSION-20260414-101200-B3NQ",
    "parent_id": "SESSION-20260414-093015-K7PX"
  }
}
```

Response:

```json
{
  "success": true,
  "child": "SESSION-20260414-101200-B3NQ",
  "parent": "SESSION-20260414-093015-K7PX",
  "message": "Linked SESSION-20260414-101200-B3NQ -> SESSION-20260414-093015-K7PX"
}
```

### Finalize a session and generate the summary

```json
{
  "tool": "session_finalize",
  "arguments": {
    "session_id": "SESSION-20260414-093015-K7PX"
  }
}
```

Response:

```json
{
  "success": true,
  "session_id": "SESSION-20260414-093015-K7PX",
  "summary_file": "/Users/dev/.claude/logs/sessions/SESSION-20260414-093015-K7PX/session-summary.md",
  "duration": "45m 12s",
  "requests": 6,
  "tools": 38,
  "files_modified": 4,
  "policies": 12
}
```

---

## Integration with Claude Workflow Engine

`mcp-session-mgr` is one of 13 MCP servers in the
[Claude Workflow Engine](https://github.com/techdeveloper-org/claude-workflow-engine)
ecosystem — a LangGraph orchestration pipeline for automating Claude Code development
workflows across an 8-step execution model (Pre-0, Step 0, Steps 8-14).

### Pipeline integration points

| Hook / Step | Call | Purpose |
|-------------|------|---------|
| **UserPromptSubmit hook** | `session_create` | Create a new session at the start of each user message |
| **Level 1 Sync** | `session_accumulate` | Record task type, skill, complexity, model, and context % after every routing decision |
| **Stop hook** | `session_finalize` | Generate the full session summary when the conversation ends |
| **Stop hook** | `session_link` | Link the just-closed session to its successor if the user restarted with `/clear` |

### Related servers called alongside session-mgr

The `session_finalize` tool reads log files written by two other servers in the pipeline:

- `mcp-post-tool-tracker` writes `logs/sessions/{id}/tool-tracker.jsonl` — consumed by
  `session_finalize` to compute file-modification counts and tool-call success rates.
- `mcp-policy-enforcement` writes `logs/sessions/{id}/flow-trace.json` — consumed by
  `session_finalize` to include pipeline decision history in the markdown summary.

---

## Architecture Notes

### In-engine copy in `src/mcp/`

The Claude Workflow Engine parent project keeps a copy of this server at
`src/mcp/session_mcp_server.py`. That copy exists because the session bridge module
(`src/mcp/session_hooks.py`) imports it in-process for tight coupling in the hook chain.

This repository is the source of truth. Changes must be made here first, then the
in-engine copy at `src/mcp/` in the parent project must be updated to match.

### Shared base package (`base/`)

Each server in the ecosystem includes a copy of the
[mcp-base](https://github.com/techdeveloper-org/mcp-base) shared library in its `base/`
directory. This copy provides:

- `base.decorators.mcp_tool_handler` — standardized error handling and response wrapping
  for every `@mcp.tool()` handler
- `base.persistence.AtomicJsonStore` — atomic read-modify-write for JSON files with
  default-factory support

The `base/` copy is included so each server repo is self-contained and installable
without a separate package installation step.

### Storage model

All persistence uses direct file I/O via `pathlib`. There is no database dependency. The
chain index (`.chain-index.json`) is the single coordination file shared across all tools
and is accessed through `AtomicJsonStore` to prevent concurrent-write corruption. Session
summary files use a write-to-temp-then-rename pattern to ensure partial writes are never
visible to readers.

---

## Contributing

1. Fork this repository.
2. Create a feature branch: `git checkout -b feature/your-change`.
3. Make your changes and add tests where applicable.
4. Ensure all existing tests pass.
5. Submit a pull request against `main`.

Bug reports and feature requests are welcome via
[GitHub Issues](https://github.com/techdeveloper-org/mcp-session-mgr/issues).

---

## License

MIT License. See [LICENSE](LICENSE) for details.
