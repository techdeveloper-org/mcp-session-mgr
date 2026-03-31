# mcp-session-mgr

A FastMCP server providing **Session Mgr** capabilities for Claude Code workflows.

---

## Overview

Session lifecycle management for multi-step Claude Code workflows. Creates sessions, accumulates task/tool/file activity, finalizes with summaries, supports tagging for categorization, and inter-session linking for chaining related sessions. Generates comprehensive statistics.

---

## Tools

| Tool | Description |
|------|-------------|
| `session_create` | Create a new session with task description and metadata |
| `session_accumulate` | Accumulate activity: tool calls, file edits, task steps |
| `session_finalize` | Finalize session and generate summary with statistics |
| `session_tag` | Add tags to a session for categorization and search |
| `session_link` | Link sessions for chaining (parent -> child, prerequisite -> followup) |
| `session_get_status` | Get current session status, progress, and budget estimate |
| `session_list` | List sessions with filters (tag, date, status, project) |
| `session_export_summary` | Export full session summary as structured JSON |

---

## Installation

### 1. Clone the repository

```bash
git clone https://github.com/techdeveloper-org/mcp-session-mgr.git
cd mcp-session-mgr
```

### 2. Install dependencies

```bash
pip install mcp fastmcp
```

### 3. Configure environment

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

---

## Configuration

### Environment Variables

| Variable | Description |
|----------|-------------|
| `CLAUDE_SESSION_DIR` | Session storage directory (default: ~/.claude/sessions) |
| `CLAUDE_SESSION_ID` | Current session ID (auto-generated if not set) |
| `CLAUDE_PROJECT` | Project identifier for session grouping |

---

## Usage in Claude Code

Add to your `~/.claude/settings.json`:

```json
{
  "mcpServers": {
    "session-mgr": {
      "command": "python",
      "args": [
        "/path/to/mcp-session-mgr/server.py"
      ],
      "env": {}
    }
  }
}
```

---

## Benefits

- Session chaining enables multi-conversation task continuity
- Tool/file accumulation provides full audit trail per session
- Tag-based search enables finding related sessions across projects
- Export summary integrates with external reporting and dashboards

---

## Requirements

- Python 3.8+
- `mcp fastmcp`
- See `requirements.txt` for pinned versions

---

## Project Context

This MCP server is part of the **Claude Workflow Engine** ecosystem — a LangGraph-based
orchestration pipeline for automating Claude Code development workflows.

Related repos:
- [`claude-workflow-engine`](https://github.com/techdeveloper-org/claude-workflow-engine) — Main pipeline
- [`mcp-base`](https://github.com/techdeveloper-org/mcp-base) — Shared base utilities used by all MCP servers

---

## License

Private — techdeveloper-org
