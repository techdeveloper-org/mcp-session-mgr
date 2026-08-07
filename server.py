"""
Session Management MCP Server - FastMCP-based session persistence for Claude Code.

Replaces 5+ session scripts with direct MCP tools. Includes session chaining,
ID generation, tag extraction, and flow context building.
Backend: Direct file I/O with pathlib
Transport: stdio

Tools (14):
  session_save, session_load, session_list, session_archive, session_query,
  session_create, session_link, session_tag, session_get_context,
  session_search_tags, session_accumulate, session_finalize,
  session_add_work_item, session_complete_work_item
"""

import json
import random
import shutil
import string
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Annotated

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from utils.path_resolver import get_config_dir

# mcp 2.0 renamed FastMCP to MCPServer and moved it to mcp.server.mcpserver.
# Both names are probed so this server runs under either major version; the
# API used below (tool decorator, run(transport=...)) is identical in both.
try:
    from mcp.server.mcpserver import MCPServer
except ImportError:  # mcp < 2.0
    from mcp.server.fastmcp import FastMCP as MCPServer
from mcp.types import ToolAnnotations
from pydantic import Field
from base.decorators import mcp_tool_handler
from base.persistence import AtomicJsonStore

mcp = MCPServer("session-mgr", instructions="Session management with direct file I/O")

# Base paths
MEMORY_PATH = get_config_dir()
SESSIONS_PATH = MEMORY_PATH / "sessions"
STATE_PATH = MEMORY_PATH / ".state"
CHAIN_INDEX_FILE = MEMORY_PATH / ".chain-index.json"
CURRENT_SESSION_FILE = MEMORY_PATH / ".current-session.json"
LOGS_PATH = MEMORY_PATH / "logs" / "sessions"

# Chain index store (module-level singleton)
_chain_store = AtomicJsonStore(
    CHAIN_INDEX_FILE,
    default_factory=lambda: {"version": "1.0.0", "sessions": {}, "tag_index": {}}
)

# Tech keywords for auto-tag extraction
_TECH_KEYWORDS = [
    "spring-boot", "docker", "kubernetes", "jenkins", "angular", "react",
    "mongodb", "postgresql", "redis", "elasticsearch", "eureka",
    "config-server", "gateway", "auth", "blog", "scheduler", "sitemap",
    "python", "java", "kotlin", "swift", "css", "seo", "mcp", "langgraph",
    "flask", "fastapi", "django", "terraform", "github-actions", "kafka",
]


# Read-only annotation reused by every tool that only inspects stored sessions.
_READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    destructiveHint=False,
    idempotentHint=True,
    openWorldHint=False,
)


def _safe_component(value: str, field_name: str) -> str:
    """Validate a caller-supplied string that becomes a single path component.

    Session and project names are interpolated directly into filesystem paths.
    Without this check a value such as '../../..' or an absolute path would let
    a tool call read or write outside the session storage tree.

    Args:
        value: Caller-supplied name.
        field_name: Parameter name, used in the error message.

    Returns:
        The validated value unchanged.

    Raises:
        ValueError: If the value is empty, contains a path separator, a null
            byte, a drive letter, or resolves to a parent-directory reference.
    """
    if not value:
        raise ValueError(f"{field_name} must not be empty")
    if value in (".", ".."):
        raise ValueError(f"{field_name} must not be a directory reference")
    if "\x00" in value:
        raise ValueError(f"{field_name} must not contain null bytes")
    if "/" in value or "\\" in value or ":" in value:
        raise ValueError(
            f"{field_name} must be a single name, not a path "
            f"(got {value!r})"
        )
    return value


def _ensure_dirs(project: str = None):
    """Ensure session directories exist.

    Args:
        project: Optional project name. Validated as a single path component
            before a directory is created for it.
    """
    SESSIONS_PATH.mkdir(parents=True, exist_ok=True)
    STATE_PATH.mkdir(parents=True, exist_ok=True)
    if project:
        _safe_component(project, "project")
        (SESSIONS_PATH / project).mkdir(parents=True, exist_ok=True)


def safe_load_session(session_file):
    """Load a session JSON file with corruption recovery.

    Tries the primary file first. If it fails JSON parsing, tries:
    1. The .bak backup file
    2. Returns a minimal valid session dict as last resort

    Args:
        session_file: Path or str to the session JSON file

    Returns:
        dict: The loaded session data, or a minimal recovery dict
    """
    session_path = Path(session_file)

    # Try primary file
    try:
        data = json.loads(session_path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, IOError, ValueError):
        pass

    # Try .bak backup
    bak_path = session_path.with_suffix(session_path.suffix + ".bak")
    try:
        if bak_path.exists():
            data = json.loads(bak_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except (json.JSONDecodeError, IOError, ValueError):
        pass

    # Last resort: return minimal valid session
    return {
        "session_id": session_path.stem,
        "status": "recovered",
        "recovered_at": datetime.now().isoformat(),
        "recovery_reason": "primary and backup files corrupted or missing",
    }


def _atomic_write(file_path: Path, content: str) -> None:
    """Write text to a file via temp-then-rename so no reader sees a partial file.

    The temp file name appends '.tmp' rather than replacing the existing
    suffix, so a session id containing a dot cannot make two concurrent writes
    collide on the same temp path.

    Note: this guarantees readers never observe a half-written file. It does not
    serialize two independent writers - the later rename still wins outright.

    Args:
        file_path: Destination path.
        content: Text to write, UTF-8 encoded.
    """
    file_path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = file_path.with_name(file_path.name + ".tmp")
    temp_path.write_text(content, encoding="utf-8")
    temp_path.replace(file_path)


def _extract_tags(prompt: str, task_type: str = "", skill: str = "",
                  project_cwd: str = "") -> list:
    """Auto-extract tags from prompt and metadata."""
    tags = set()
    if task_type:
        tags.add(task_type.lower().replace(" ", "-"))
    if skill and skill != "adaptive-skill-intelligence":
        tags.add(skill)
    if project_cwd:
        parts = project_cwd.replace("\\", "/").rstrip("/").split("/")
        if parts:
            tags.add(parts[-1])
    prompt_lower = prompt.lower()
    for kw in _TECH_KEYWORDS:
        if kw in prompt_lower:
            tags.add(kw)
    return sorted(tags)


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=False,
    idempotentHint=True, openWorldHint=False))
@mcp_tool_handler
def session_save(
    session_id: Annotated[str, Field(
        description="Session identifier the data belongs to. Must be a single "
                    "name, not a path.")],
    data_type: Annotated[str, Field(
        description="Which artifact to write: 'summary' (markdown), 'state' "
                    "(per-project JSON), or 'context' (JSON snapshot).")],
    content: Annotated[str, Field(
        description="Payload to persist. Markdown for 'summary'; a JSON string "
                    "for 'state' and 'context'.")],
    project: Annotated[str, Field(
        description="Project name used to group sessions on disk. Must be a single directory name, not a path.")] = "default",
) -> dict:
    """Save session data to disk atomically.

    Args:
        session_id: Unique session identifier (e.g., '2026-03-15-14-30')
        data_type: Type of data - 'summary', 'state', 'context'
        content: Content to save (markdown or JSON string)
        project: Project name for organizing sessions
    """
    _safe_component(project, "project")
    _safe_component(session_id, "session_id")
    _ensure_dirs(project)

    if data_type == "state":
        # State files go to .state directory
        file_path = STATE_PATH / f"{project}.json"
        # Parse content as JSON if possible, otherwise wrap it
        try:
            data = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            data = {"content": content, "updated_at": datetime.now().isoformat()}

        _atomic_write(file_path, json.dumps(data, indent=2, default=str))

    elif data_type == "summary":
        # Session summaries go to sessions/{project}/. Written through the same
        # temp-then-rename path as the other types; a plain truncating write
        # leaves a half-written summary behind if it is interrupted.
        file_path = SESSIONS_PATH / project / f"session-{session_id}.md"
        _atomic_write(file_path, content)

    elif data_type == "context":
        # Context snapshots
        file_path = SESSIONS_PATH / project / f"context-{session_id}.json"
        _atomic_write(file_path, content)

    else:
        return {"success": False, "error": f"Unknown data_type: {data_type}"}

    return {
        "success": True,
        "file": str(file_path),
        "data_type": data_type,
        "session_id": session_id,
        "size_bytes": file_path.stat().st_size
    }


@mcp.tool(annotations=_READ_ONLY)
@mcp_tool_handler
def session_load(
    session_id: Annotated[str, Field(
        description="Session identifier to load. Leave empty with "
                    "data_type='summary' to load the most recent session.")] = "",
    data_type: Annotated[str, Field(
        description="Which artifact to read: 'summary', 'state', or "
                    "'context'.")] = "state",
    project: Annotated[str, Field(
        description="Project name used to group sessions on disk. Must be a single directory name, not a path.")] = "default",
) -> dict:
    """Load session data from disk.

    Args:
        session_id: Session identifier (empty for latest state)
        data_type: 'summary', 'state', or 'context'
        project: Project name
    """
    _safe_component(project, "project")
    if session_id:
        _safe_component(session_id, "session_id")

    if data_type == "state":
        file_path = STATE_PATH / f"{project}.json"
        if not file_path.exists():
            return {
                "success": True,
                "data": {},
                "message": "No state file found"
            }
        # Recovers from a corrupt primary file via its .bak backup instead of
        # raising, matching the corruption handling the context branch uses.
        data = safe_load_session(file_path)
        return {"success": True, "data": data, "file": str(file_path)}

    elif data_type == "summary":
        if session_id:
            file_path = SESSIONS_PATH / project / f"session-{session_id}.md"
        else:
            # Find latest session file
            project_dir = SESSIONS_PATH / project
            if not project_dir.exists():
                return {"success": True, "data": "", "message": "No sessions found"}
            session_files = sorted(project_dir.glob("session-*.md"), reverse=True)
            if not session_files:
                return {"success": True, "data": "", "message": "No sessions found"}
            file_path = session_files[0]

        if not file_path.exists():
            return {"success": False, "error": f"Session not found: {session_id}"}
        with open(file_path, "r", encoding="utf-8") as f:
            content = f.read()
        return {"success": True, "data": content, "file": str(file_path)}

    elif data_type == "context":
        if not session_id:
            return {"success": False, "error": "session_id is required for data_type 'context'"}
        file_path = SESSIONS_PATH / project / f"context-{session_id}.json"
        if not file_path.exists():
            return {"success": False, "error": f"Context not found: {session_id}"}
        data = safe_load_session(file_path)
        return {"success": True, "data": data, "file": str(file_path)}

    else:
        return {"success": False, "error": f"Unknown data_type: {data_type}"}


@mcp.tool(annotations=_READ_ONLY)
@mcp_tool_handler
def session_list(
    project: Annotated[str, Field(
        description="Restrict to one project. Empty lists every project.")] = "",
    limit: Annotated[int, Field(
        ge=1, le=500,
        description="Maximum sessions to return. Results are sorted by "
                    "modification time descending before this bound is "
                    "applied; the response reports total_matched and "
                    "truncated.")] = 20,
) -> dict:
    """List available sessions.

    Args:
        project: Filter by project name (empty = all projects)
        limit: Maximum number of sessions to return
    """
    if project:
        _safe_component(project, "project")
        project_dirs = [SESSIONS_PATH / project]
    else:
        if not SESSIONS_PATH.exists():
            return {"success": True, "sessions": [], "count": 0, "total_matched": 0,
                    "truncated": False}
        project_dirs = [d for d in SESSIONS_PATH.iterdir() if d.is_dir()]

    # Collect every candidate before bounding. The previous implementation
    # stopped collecting as soon as `limit` rows had been gathered in raw
    # directory-iteration order, and only sorted by modification time
    # afterwards. With more than `limit` sessions in the first project scanned,
    # no session from any later project could ever appear - even when those
    # were the most recently modified - and nothing in the response indicated
    # that results had been dropped.
    sessions = []
    for proj_dir in project_dirs:
        if not proj_dir.exists():
            continue
        proj_name = proj_dir.name
        for session_file in proj_dir.glob("session-*.md"):
            try:
                stat = session_file.stat()
            except OSError:
                continue
            sessions.append({
                "project": proj_name,
                "session_id": session_file.stem.replace("session-", ""),
                "file": str(session_file),
                "size_bytes": stat.st_size,
                "modified": datetime.fromtimestamp(stat.st_mtime).isoformat()
            })

    total_matched = len(sessions)
    sessions.sort(key=lambda s: s["modified"], reverse=True)
    sessions = sessions[:limit]

    return {
        "success": True,
        "sessions": sessions,
        "count": len(sessions),
        "total_matched": total_matched,
        "truncated": total_matched > len(sessions),
    }


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=True,
    idempotentHint=False, openWorldHint=False))
@mcp_tool_handler
def session_archive(
    days_old: Annotated[int, Field(
        ge=1,
        description="Archive sessions whose last modification is older than "
                    "this many days. Files are moved into sessions/_archived/, "
                    "not deleted.")] = 30,
) -> dict:
    """Archive sessions older than specified days.

    Args:
        days_old: Archive sessions older than this many days (default: 30)
    """
    if not SESSIONS_PATH.exists():
        return {"success": True, "archived": [], "count": 0, "failed": []}

    cutoff = datetime.now() - timedelta(days=days_old)
    archived = []
    failed = []
    archive_dir = SESSIONS_PATH / "_archived"
    archive_dir.mkdir(parents=True, exist_ok=True)

    for proj_dir in SESSIONS_PATH.iterdir():
        if not proj_dir.is_dir() or proj_dir.name.startswith("_"):
            continue

        for session_file in proj_dir.glob("session-*.md"):
            try:
                file_mtime = datetime.fromtimestamp(session_file.stat().st_mtime)
            except OSError as e:
                failed.append({"file": session_file.name, "project": proj_dir.name,
                               "error": str(e)[:150]})
                continue
            if file_mtime >= cutoff:
                continue

            dest_dir = archive_dir / proj_dir.name
            dest_dir.mkdir(parents=True, exist_ok=True)
            dest = dest_dir / session_file.name
            # A failed move must be reported: silently skipping it would make
            # the response claim a clean archive run while files stayed behind.
            try:
                shutil.move(str(session_file), str(dest))
            except (OSError, shutil.Error) as e:
                failed.append({"file": session_file.name, "project": proj_dir.name,
                               "error": str(e)[:150]})
                continue
            archived.append({
                "file": session_file.name,
                "project": proj_dir.name,
                "age_days": (datetime.now() - file_mtime).days
            })

    return {
        "success": not failed,
        "archived": archived,
        "count": len(archived),
        "failed": failed,
        "failed_count": len(failed),
        "archive_dir": str(archive_dir)
    }


@mcp.tool(annotations=_READ_ONLY)
@mcp_tool_handler
def session_query(
    filters: Annotated[str, Field(
        description="JSON object string of filter criteria. Keys: project "
                    "(single name), date_from and date_to (ISO 8601), keyword "
                    "(case-insensitive full-text match), limit (positive "
                    "integer, default 50).")] = "{}",
) -> dict:
    """Query sessions with filters.

    Args:
        filters: JSON string with filter criteria.
            Supported keys: project, date_from, date_to, keyword
            Example: '{"project": "claude-insight", "keyword": "MCP"}'
    """
    try:
        filter_dict = json.loads(filters)
    except json.JSONDecodeError:
        return {"success": False, "error": "Invalid JSON in filters parameter"}

    project_filter = filter_dict.get("project", "")
    keyword = filter_dict.get("keyword", "").lower()
    limit = filter_dict.get("limit", 50)
    if not isinstance(limit, int) or limit < 1:
        return {"success": False, "error": "filters.limit must be a positive integer"}

    # Parse date bounds once, up front, so a malformed date is reported as a
    # clear input error rather than raised from inside the scan loop.
    bounds = {}
    for key in ("date_from", "date_to"):
        raw = filter_dict.get(key, "")
        if not raw:
            continue
        try:
            bounds[key] = datetime.fromisoformat(raw)
        except (TypeError, ValueError):
            return {"success": False,
                    "error": f"filters.{key} is not a valid ISO 8601 datetime: {raw!r}"}

    if not SESSIONS_PATH.exists():
        return {"success": True, "results": [], "count": 0, "total_matched": 0,
                "truncated": False, "filters": filter_dict}

    if project_filter:
        _safe_component(project_filter, "filters.project")
        project_dirs = [SESSIONS_PATH / project_filter]
    else:
        project_dirs = [d for d in SESSIONS_PATH.iterdir() if d.is_dir() and not d.name.startswith("_")]

    # Filter the full candidate set first, then sort, then bound. The previous
    # implementation stopped at 50 rows mid-scan in raw directory order, so
    # later project directories were starved of their matches and the retained
    # 50 were an arbitrary subset rather than the most recent ones.
    results = []
    for proj_dir in project_dirs:
        if not proj_dir.exists():
            continue

        for session_file in proj_dir.glob("session-*.md"):
            try:
                stat = session_file.stat()
            except OSError:
                continue
            file_date = datetime.fromtimestamp(stat.st_mtime)

            if "date_from" in bounds and file_date < bounds["date_from"]:
                continue
            if "date_to" in bounds and file_date > bounds["date_to"]:
                continue

            if keyword:
                try:
                    content = session_file.read_text(encoding="utf-8", errors="ignore").lower()
                except OSError:
                    continue
                if keyword not in content:
                    continue

            results.append({
                "project": proj_dir.name,
                "session_id": session_file.stem.replace("session-", ""),
                "file": str(session_file),
                "modified": file_date.isoformat(),
                "size_bytes": stat.st_size
            })

    total_matched = len(results)
    results.sort(key=lambda r: r["modified"], reverse=True)
    results = results[:limit]

    return {
        "success": True,
        "results": results,
        "count": len(results),
        "total_matched": total_matched,
        "truncated": total_matched > len(results),
        "filters": filter_dict
    }


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=False,
    idempotentHint=False, openWorldHint=False))
@mcp_tool_handler
def session_create(
    project: Annotated[str, Field(
        description="Project name used to group sessions on disk. Must be a single directory name, not a path.")] = "default",
    task_type: Annotated[str, Field(
        description="Classification of the work, e.g. 'Implementation' or "
                    "'Bug Fix'. Recorded as a tag.")] = "",
    skill: Annotated[str, Field(
        description="Primary skill in use for this session. Recorded as a "
                    "tag.")] = "",
    prompt: Annotated[str, Field(
        description="User prompt text. Scanned for known technology keywords "
                    "to auto-extract tags; the first 200 characters are "
                    "retained.")] = "",
    project_cwd: Annotated[str, Field(
        description="Working directory path. Its final segment becomes a "
                    "project tag.")] = "",
) -> dict:
    """Create a new session with unique ID and register it in the chain index.

    Generates ID format: SESSION-YYYYMMDD-HHMMSS-XXXX

    Args:
        project: Project name
        task_type: Type of task (e.g., 'Implementation', 'Bug Fix')
        skill: Skill being used
        prompt: User's prompt text (used for auto-tag extraction)
        project_cwd: Working directory path (used for project tag extraction)
    """
    now = datetime.now()
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    session_id = f"SESSION-{now.strftime('%Y%m%d-%H%M%S')}-{suffix}"

    # Auto-extract tags
    tags = _extract_tags(prompt, task_type, skill, project_cwd)

    # Register in chain index
    index = _chain_store.load()
    index["sessions"][session_id] = {
        "parent": None,
        "children": [],
        "related": [],
        "tags": tags,
        "project": project,
        "skill": skill,
        "task_type": task_type,
        "summary": "",
        "created_at": now.isoformat(),
        "last_prompt": prompt[:200] if prompt else ""
    }

    # Update tag index
    for tag in tags:
        if tag not in index["tag_index"]:
            index["tag_index"][tag] = []
        if session_id not in index["tag_index"][tag]:
            index["tag_index"][tag].append(session_id)

    _chain_store.save(index)

    # Update current session pointer
    CURRENT_SESSION_FILE.parent.mkdir(parents=True, exist_ok=True)
    CURRENT_SESSION_FILE.write_text(json.dumps({
        "current_session_id": session_id,
        "project": project,
        "created_at": now.isoformat()
    }, indent=2), encoding="utf-8")

    _ensure_dirs(project)

    return {
        "success": True,
        "session_id": session_id,
        "project": project,
        "tags": tags,
        "created_at": now.isoformat()
    }


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=False,
    idempotentHint=True, openWorldHint=False))
@mcp_tool_handler
def session_link(
    child_id: Annotated[str, Field(
        description="Session that continues the parent, typically a new "
                    "session created after /clear.")],
    parent_id: Annotated[str, Field(
        description="Preceding session that the child continues from.")],
) -> dict:
    """Link a child session to its parent (for /clear continuity).

    Args:
        child_id: New session ID (child)
        parent_id: Previous session ID (parent)
    """
    index = _chain_store.load()

    if parent_id not in index["sessions"]:
        index["sessions"][parent_id] = {
            "parent": None, "children": [], "related": [],
            "tags": [], "project": "", "skill": "", "task_type": "",
            "summary": "", "created_at": "", "last_prompt": ""
        }

    if child_id not in index["sessions"]:
        index["sessions"][child_id] = {
            "parent": parent_id, "children": [], "related": [],
            "tags": [], "project": "", "skill": "", "task_type": "",
            "summary": "", "created_at": datetime.now().isoformat(),
            "last_prompt": ""
        }
    else:
        index["sessions"][child_id]["parent"] = parent_id

    if child_id not in index["sessions"][parent_id]["children"]:
        index["sessions"][parent_id]["children"].append(child_id)

    _chain_store.save(index)

    return {
        "success": True,
        "child": child_id,
        "parent": parent_id,
        "message": f"Linked {child_id} -> {parent_id}"
    }


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=False,
    idempotentHint=True, openWorldHint=False))
@mcp_tool_handler
def session_tag(
    session_id: Annotated[str, Field(
        description="Session to tag. Created in the chain index if absent.")],
    tags: Annotated[str, Field(
        description="Comma-separated tags, e.g. 'spring-boot,docker'. Merged "
                    "with any existing tags.")],
    summary: Annotated[str, Field(
        description="Optional summary text stored on the session.")] = "",
) -> dict:
    """Add tags and optional summary to a session. Auto-relates by shared tags.

    Args:
        session_id: Session ID to tag
        tags: Comma-separated tag list (e.g., 'spring-boot,docker,scheduler')
        summary: Optional session summary text
    """
    index = _chain_store.load()
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    if session_id not in index["sessions"]:
        index["sessions"][session_id] = {
            "parent": None, "children": [], "related": [],
            "tags": [], "project": "", "skill": "", "task_type": "",
            "summary": "", "created_at": datetime.now().isoformat(),
            "last_prompt": ""
        }

    session = index["sessions"][session_id]
    existing_tags = set(session.get("tags", []))
    new_tags = existing_tags | set(tag_list)
    session["tags"] = sorted(new_tags)

    if summary:
        session["summary"] = summary

    # Update tag index
    for tag in tag_list:
        if tag not in index["tag_index"]:
            index["tag_index"][tag] = []
        if session_id not in index["tag_index"][tag]:
            index["tag_index"][tag].append(session_id)

    # Auto-relate sessions that share 2+ tags
    related_found = []
    for other_id, other_session in index["sessions"].items():
        if other_id == session_id:
            continue
        other_tags = set(other_session.get("tags", []))
        shared = new_tags & other_tags
        if len(shared) >= 2:
            if other_id not in session.get("related", []):
                session.setdefault("related", []).append(other_id)
            if session_id not in other_session.get("related", []):
                other_session.setdefault("related", []).append(session_id)
            related_found.append(other_id)

    _chain_store.save(index)

    return {
        "success": True,
        "session_id": session_id,
        "tags": session["tags"],
        "auto_related": related_found
    }


@mcp.tool(annotations=_READ_ONLY)
@mcp_tool_handler
def session_get_context(
    session_id: Annotated[str, Field(
        description="Session whose chain context to assemble.")],
    max_ancestors: Annotated[int, Field(
        ge=0, le=100,
        description="Maximum parent sessions to walk up the chain.")] = 5,
    max_related: Annotated[int, Field(
        ge=0, le=100,
        description="Maximum tag-related sessions to include.")] = 5,
) -> dict:
    """Get chain context for a session (ancestors + related sessions).

    Walks the parent chain and finds related sessions for context continuity.

    Args:
        session_id: Session to get context for
        max_ancestors: Max parent sessions to walk (default: 5)
        max_related: Max related sessions to include (default: 5)
    """
    index = _chain_store.load()

    if session_id not in index["sessions"]:
        return {
            "success": True,
            "session_id": session_id,
            "ancestors": [],
            "related": [],
            "message": "Session not found in chain index"
        }

    session = index["sessions"][session_id]

    # Walk parent chain
    ancestors = []
    current = session.get("parent")
    visited = {session_id}
    while current and len(ancestors) < max_ancestors and current not in visited:
        visited.add(current)
        if current in index["sessions"]:
            parent = index["sessions"][current]
            ancestors.append({
                "session_id": current,
                "summary": parent.get("summary", ""),
                "tags": parent.get("tags", []),
                "task_type": parent.get("task_type", ""),
                "skill": parent.get("skill", ""),
                "created_at": parent.get("created_at", "")
            })
            current = parent.get("parent")
        else:
            break

    # Find related sessions
    related = []
    for rel_id in session.get("related", [])[:max_related]:
        if rel_id in index["sessions"]:
            rel = index["sessions"][rel_id]
            shared_tags = set(session.get("tags", [])) & set(rel.get("tags", []))
            related.append({
                "session_id": rel_id,
                "summary": rel.get("summary", ""),
                "tags": rel.get("tags", []),
                "shared_tags": sorted(shared_tags),
                "created_at": rel.get("created_at", "")
            })

    return {
        "success": True,
        "session_id": session_id,
        "current": {
            "tags": session.get("tags", []),
            "task_type": session.get("task_type", ""),
            "skill": session.get("skill", ""),
            "project": session.get("project", "")
        },
        "ancestors": ancestors,
        "related": related,
        "chain_depth": len(ancestors)
    }


@mcp.tool(annotations=_READ_ONLY)
@mcp_tool_handler
def session_search_tags(
    tags: Annotated[str, Field(
        description="Comma-separated tags. A session matching ANY tag is "
                    "returned, ranked by how many tags it matched.")],
    limit: Annotated[int, Field(
        ge=1, le=500,
        description="Maximum results to return, applied after ranking.")] = 20,
) -> dict:
    """Search sessions by tags. Returns sessions matching ANY of the given tags.

    Args:
        tags: Comma-separated tag list to search for
        limit: Max results (default: 20)
    """
    index = _chain_store.load()
    tag_list = [t.strip() for t in tags.split(",") if t.strip()]

    matching = {}
    for tag in tag_list:
        for sid in index.get("tag_index", {}).get(tag, []):
            if sid not in matching:
                matching[sid] = {"session_id": sid, "matched_tags": [], "all_tags": []}
            matching[sid]["matched_tags"].append(tag)
            if sid in index["sessions"]:
                matching[sid]["all_tags"] = index["sessions"][sid].get("tags", [])
                matching[sid]["summary"] = index["sessions"][sid].get("summary", "")
                matching[sid]["project"] = index["sessions"][sid].get("project", "")

    # Sort by number of matched tags (most relevant first)
    results = sorted(matching.values(),
                    key=lambda x: len(x["matched_tags"]), reverse=True)[:limit]

    return {
        "success": True,
        "results": results,
        "count": len(results),
        "searched_tags": tag_list
    }


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=False,
    idempotentHint=False, openWorldHint=False))
@mcp_tool_handler
def session_accumulate(
    session_id: Annotated[str, Field(
        description="Active session to append this request to. Required.")],
    prompt: Annotated[str, Field(
        description="User message text. Truncated to 500 characters in the "
                    "stored entry; the full length is recorded separately.")] = "",
    task_type: Annotated[str, Field(
        description="Classified task type for this request, e.g. 'Backend' or "
                    "'Bug Fix'.")] = "",
    skill: Annotated[str, Field(
        description="Primary skill selected for this request.")] = "",
    complexity: Annotated[int, Field(
        ge=0, le=25,
        description="Task complexity score, 0 through 25. Accumulated into the "
                    "session total and maximum.")] = 0,
    model: Annotated[str, Field(
        description="Model chosen for this request, e.g. 'SONNET' or "
                    "'OPUS'.")] = "",
    cwd: Annotated[str, Field(
        description="Working directory the request ran in.")] = "",
    plan_mode: Annotated[bool, Field(
        description="True when plan mode was used, incrementing the session's "
                    "plan-mode counter.")] = False,
    context_pct: Annotated[int, Field(
        ge=0, le=100,
        description="Estimated context-window usage percentage for this "
                    "request. The session peak is tracked from this.")] = 0,
    supplementary_skills: Annotated[str, Field(
        description="Comma-separated secondary skill names used alongside the "
                    "primary skill.")] = "",
    standards_count: Annotated[int, Field(
        ge=0,
        description="Number of active standards loaded for this request.")] = 0,
    rules_count: Annotated[int, Field(
        ge=0,
        description="Number of active rules loaded for this request.")] = 0,
) -> dict:
    """Accumulate per-request data for session summary generation.

    Called by 3-level-flow.py after every user message. Appends a request
    entry and updates aggregate fields.

    Args:
        session_id: Active session ID
        prompt: User message text (truncated to 500 chars)
        task_type: Classified task type (e.g., 'Backend', 'Bug Fix')
        skill: Primary skill selected
        complexity: Task complexity score (0-25)
        model: Model name (e.g., 'SONNET', 'OPUS')
        cwd: Working directory path
        plan_mode: Whether plan mode was used
        context_pct: Estimated context window usage percentage
        supplementary_skills: Comma-separated supplementary skill names
        standards_count: Number of active standards loaded
        rules_count: Number of active rules loaded
    """
    if not session_id:
        return {"success": False, "error": "session_id is required"}

    store = AtomicJsonStore(LOGS_PATH / session_id / "session-summary.json")
    data = store.load()
    if not data:
        data = {
            "session_id": session_id,
            "created_at": datetime.now().isoformat(),
            "requests": [],
            "request_count": 0,
            "skills_used": [],
            "task_types": [],
            "models_used": [],
            "all_supplementary_skills": [],
            "plan_mode_count": 0,
            "context_history": [],
            "peak_context_pct": 0,
            "total_complexity": 0,
            "max_complexity": 0,
            "status": "IN_PROGRESS"
        }

    # Add request entry
    entry = {
        "timestamp": datetime.now().isoformat(),
        "prompt": prompt[:500],
        "prompt_char_count": len(prompt),
        "task_type": task_type,
        "skill": skill,
        "complexity": complexity,
        "model": model,
        "cwd": cwd,
        "plan_mode": plan_mode,
        "context_pct": context_pct,
        "supplementary_skills": [s.strip() for s in supplementary_skills.split(",") if s.strip()] if supplementary_skills else [],
        "decision_rationale": f"Complexity {complexity} -> Model {model}, Skill {skill}",
    }

    data["requests"].append(entry)
    data["request_count"] = len(data["requests"])
    data["last_updated"] = datetime.now().isoformat()

    # Track unique values
    if skill and skill not in data["skills_used"]:
        data["skills_used"].append(skill)
    if task_type and task_type not in data["task_types"]:
        data["task_types"].append(task_type)
    if model and model not in data["models_used"]:
        data["models_used"].append(model)

    # Supplementary skills
    if supplementary_skills:
        for s in supplementary_skills.split(","):
            s = s.strip()
            if s and s not in data.get("all_supplementary_skills", []):
                data.setdefault("all_supplementary_skills", []).append(s)

    # Plan mode
    if plan_mode:
        data["plan_mode_count"] = data.get("plan_mode_count", 0) + 1

    # Context tracking
    ctx = int(context_pct)
    data.setdefault("context_history", []).append(ctx)
    data["peak_context_pct"] = max(data.get("peak_context_pct", 0), ctx)

    # Complexity tracking
    comp = int(complexity)
    data["total_complexity"] = data.get("total_complexity", 0) + comp
    data["max_complexity"] = max(data.get("max_complexity", 0), comp)

    store.save(data)

    return {
        "success": True,
        "session_id": session_id,
        "request_number": data["request_count"],
        "skills_used": data["skills_used"],
        "peak_context": data["peak_context_pct"]
    }


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=False,
    idempotentHint=True, openWorldHint=False))
@mcp_tool_handler
def session_finalize(
    session_id: Annotated[str, Field(
        description="Session to close out. Merges accumulated request data "
                    "with tool stats and flow-trace decisions into a markdown "
                    "summary.")],
) -> dict:
    """Generate comprehensive session summary on session close.

    Merges accumulated request data with tool stats, flow-trace decisions,
    and generates a rich markdown summary.

    Called by clear-session-handler.py on /clear.

    Args:
        session_id: Session ID to finalize
    """
    if not session_id:
        return {"success": False, "error": "session_id is required"}

    store = AtomicJsonStore(LOGS_PATH / session_id / "session-summary.json")
    data = store.load()
    if not data:
        return {
            "success": False,
            "error": f"No accumulated data for {session_id}"
        }

    # Warnings recorded on the summary so a partially-derived report never
    # presents itself as a complete one.
    source_warnings = []

    # Load flow-trace for pipeline decisions
    flow_trace_file = LOGS_PATH / session_id / "flow-trace.json"
    flow_trace = None
    if flow_trace_file.exists():
        try:
            flow_trace = json.loads(flow_trace_file.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as e:
            source_warnings.append(
                f"flow-trace.json unreadable ({type(e).__name__}) - policy "
                "counts omitted from this summary"
            )

    # Load tool tracker for file stats. Each line is parsed independently: a
    # single malformed line previously aborted the whole scan, silently
    # under-reporting tool counts and modified files in the generated summary.
    tool_tracker_file = LOGS_PATH / session_id / "tool-tracker.jsonl"
    files_modified = set()
    files_read = set()
    tool_count = 0
    error_count = 0
    skipped_lines = 0
    if tool_tracker_file.exists():
        try:
            raw_lines = tool_tracker_file.read_text(encoding="utf-8").splitlines()
        except OSError as e:
            raw_lines = []
            source_warnings.append(
                f"tool-tracker.jsonl unreadable ({type(e).__name__}) - tool "
                "statistics omitted from this summary"
            )
        for line in raw_lines:
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                skipped_lines += 1
                continue
            if not isinstance(entry, dict):
                skipped_lines += 1
                continue
            tool_count += 1
            tool_name = entry.get("tool", "")
            file_path = entry.get("file", "")
            if entry.get("error"):
                error_count += 1
            if file_path:
                if tool_name in ("Write", "Edit"):
                    files_modified.add(file_path)
                elif tool_name == "Read":
                    files_read.add(file_path)
        if skipped_lines:
            source_warnings.append(
                f"{skipped_lines} malformed line(s) in tool-tracker.jsonl were "
                "skipped"
            )

    # Calculate duration
    created = data.get("created_at", "")
    last_updated = data.get("last_updated", datetime.now().isoformat())
    duration_human = ""
    duration_seconds = 0
    if created:
        try:
            start = datetime.fromisoformat(created)
            end = datetime.fromisoformat(last_updated)
            diff = end - start
            duration_seconds = int(diff.total_seconds())
            hours, remainder = divmod(duration_seconds, 3600)
            minutes, secs = divmod(remainder, 60)
            if hours > 0:
                duration_human = f"{hours}h {minutes}m {secs}s"
            elif minutes > 0:
                duration_human = f"{minutes}m {secs}s"
            else:
                duration_human = f"{secs}s"
        except (TypeError, ValueError) as e:
            source_warnings.append(
                f"Could not compute duration ({type(e).__name__}): timestamps "
                "are not valid ISO 8601"
            )

    # Calculate stats
    req_count = data.get("request_count", 0)
    total_complexity = data.get("total_complexity", 0)
    avg_complexity = round(total_complexity / req_count, 1) if req_count > 0 else 0
    success_rate = round(((tool_count - error_count) / tool_count) * 100, 1) if tool_count > 0 else 100.0

    # Policy execution stats
    policy_count = 0
    policy_duration = 0
    if flow_trace:
        policies = flow_trace.get("all_policies_executed", [])
        policy_count = len(policies)
        policy_duration = sum(p.get("duration_ms", 0) for p in policies)

    # Generate markdown summary
    md_lines = [
        f"# Session Summary: {session_id}",
        "",
        f"**Duration:** {duration_human or 'N/A'} | **Requests:** {req_count} | **Complexity:** avg {avg_complexity}, max {data.get('max_complexity', 0)}",
        "",
        "## Overview",
        "",
        "| Metric | Value |",
        "|--------|-------|",
        f"| Skills Used | {', '.join(data.get('skills_used', [])) or 'None'} |",
        f"| Task Types | {', '.join(data.get('task_types', [])) or 'None'} |",
        f"| Models Used | {', '.join(data.get('models_used', [])) or 'None'} |",
        f"| Plan Mode | {data.get('plan_mode_count', 0)} times |",
        f"| Peak Context | {data.get('peak_context_pct', 0)}% |",
        f"| Tool Calls | {tool_count} (success rate: {success_rate}%) |",
        f"| Files Modified | {len(files_modified)} |",
        f"| Files Read | {len(files_read)} |",
        f"| Policies Executed | {policy_count} ({policy_duration}ms total) |",
        "",
    ]

    # Supplementary skills
    supp = data.get("all_supplementary_skills", [])
    if supp:
        md_lines.append(f"**Supplementary Skills:** {', '.join(supp)}")
        md_lines.append("")

    # Request log
    md_lines.append("## Request Log")
    md_lines.append("")
    md_lines.append("| # | Time | Type | Skill | Complexity | Model |")
    md_lines.append("|---|------|------|-------|-----------|-------|")
    for i, req in enumerate(data.get("requests", []), 1):
        ts = req.get("timestamp", "")[:19]
        md_lines.append(
            f"| {i} | {ts} | {req.get('task_type', '')} | "
            f"{req.get('skill', '')} | {req.get('complexity', 0)} | "
            f"{req.get('model', '')} |"
        )
    md_lines.append("")

    # Files section
    if files_modified:
        md_lines.append("## Files Modified")
        md_lines.append("")
        for f in sorted(files_modified):
            md_lines.append(f"- {f}")
        md_lines.append("")

    # Pipeline decisions
    if flow_trace:
        decisions = flow_trace.get("decisions_timeline", [])
        if decisions:
            md_lines.append("## Pipeline Decisions")
            md_lines.append("")
            for d in decisions[-10:]:
                md_lines.append(f"- **{d.get('policy', '')}**: {d.get('decision', '')}")
            md_lines.append("")

    md_lines.append("---")
    md_lines.append(f"*Generated at {datetime.now().isoformat()[:19]}*")

    summary_md = "\n".join(md_lines)

    # Save markdown summary
    session_dir = LOGS_PATH / session_id
    session_dir.mkdir(parents=True, exist_ok=True)
    md_path = session_dir / "session-summary.md"
    md_path.write_text(summary_md, encoding="utf-8")

    # Update accumulated data status
    data["status"] = "COMPLETED"
    data["duration_human"] = duration_human
    data["duration_seconds"] = duration_seconds
    data["avg_complexity"] = avg_complexity
    data["files_modified"] = sorted(files_modified)
    data["files_read"] = sorted(files_read)
    data["total_tool_calls"] = tool_count
    data["error_count"] = error_count
    data["success_rate_pct"] = success_rate
    data["source_warnings"] = source_warnings
    store.save(data)

    # Update chain index summary
    index = _chain_store.load()
    if session_id in index.get("sessions", {}):
        index["sessions"][session_id]["summary"] = (
            f"{req_count} requests, {', '.join(data.get('skills_used', [])[:3])}, "
            f"complexity avg {avg_complexity}"
        )
        _chain_store.save(index)

    return {
        "success": True,
        "session_id": session_id,
        "summary_file": str(md_path),
        "duration": duration_human,
        "requests": req_count,
        "tools": tool_count,
        "files_modified": len(files_modified),
        "policies": policy_count,
        "source_warnings": source_warnings
    }


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=False,
    idempotentHint=False, openWorldHint=False))
@mcp_tool_handler
def session_add_work_item(
    session_id: Annotated[str, Field(
        description="Existing session to attach the work item to.")],
    description: Annotated[str, Field(
        description="What the work item covers.")],
    work_type: Annotated[str, Field(
        description="Prefix for the generated work item id, e.g. 'TASK', "
                    "'WORK', 'BUG'.")] = "TASK",
    metadata: Annotated[str, Field(
        description="JSON object string of extra fields stored on the work "
                    "item. Invalid JSON is stored as an empty object.")] = "{}",
) -> dict:
    """Add a work item to a session for tracking tasks within sessions.

    Args:
        session_id: Session ID to add work item to
        description: Human-readable description of the work item
        work_type: Type prefix for work item ID (e.g., 'TASK', 'WORK', 'BUG')
        metadata: JSON string of additional fields
    """
    index = _chain_store.load()

    if session_id not in index["sessions"]:
        return {"success": False, "error": f"Session not found: {session_id}"}

    session = index["sessions"][session_id]

    # Generate work item ID
    suffix = "".join(random.choices(string.ascii_uppercase + string.digits, k=4))
    work_id = f"{work_type}-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{suffix}"

    # Parse metadata
    try:
        meta = json.loads(metadata)
    except (json.JSONDecodeError, TypeError):
        meta = {}

    work_item = {
        "work_id": work_id,
        "type": work_type,
        "description": description,
        "started_at": datetime.now().isoformat(),
        "completed_at": None,
        "status": "IN_PROGRESS",
        "metadata": meta
    }

    session.setdefault("work_items", []).append(work_item)
    _chain_store.save(index)

    return {
        "success": True,
        "work_id": work_id,
        "session_id": session_id,
        "description": description,
        "status": "IN_PROGRESS"
    }


@mcp.tool(annotations=ToolAnnotations(
    readOnlyHint=False, destructiveHint=False,
    idempotentHint=True, openWorldHint=False))
@mcp_tool_handler
def session_complete_work_item(
    session_id: Annotated[str, Field(
        description="Session holding the work item.")],
    work_id: Annotated[str, Field(
        description="Work item id returned by session_add_work_item.")],
    status: Annotated[str, Field(
        description="Terminal status: COMPLETED, FAILED, or SKIPPED.")] = "COMPLETED",
) -> dict:
    """Mark a work item as completed.

    Args:
        session_id: Parent session ID
        work_id: Work item ID to complete
        status: Final status (COMPLETED, FAILED, SKIPPED)
    """
    index = _chain_store.load()

    if session_id not in index["sessions"]:
        return {"success": False, "error": f"Session not found: {session_id}"}

    session = index["sessions"][session_id]
    work_items = session.get("work_items", [])

    found = False
    for item in work_items:
        if not isinstance(item, dict):
            continue
        if item.get("work_id") == work_id:
            item["completed_at"] = datetime.now().isoformat()
            item["status"] = status
            found = True
            break

    if not found:
        return {"success": False, "error": f"Work item not found: {work_id}"}

    _chain_store.save(index)

    return {
        "success": True,
        "work_id": work_id,
        "session_id": session_id,
        "status": status,
        "completed_at": datetime.now().isoformat()
    }


if __name__ == "__main__":
    mcp.run(transport="stdio")
