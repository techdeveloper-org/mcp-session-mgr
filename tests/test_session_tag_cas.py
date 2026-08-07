"""Cross-process concurrency test for the session_tag compare-and-swap migration.

Models ``mcp-base/tests/test_persistence_cas.py``: launches real OS
subprocesses, not threads, because the defect under test is cross-process --
separate MCP tool invocations and workflow-engine hooks write the same
``.chain-index.json`` file from separate processes, and a thread-only test
would pass against an implementation that used nothing more than a
``threading.Lock``.

``session_tag`` is the site the migration report calls out as the worst of
the converted tools: it scans every session in the chain index on each call
(the auto-relate step), which widens the read-modify-write window compared
to the other converted sites and makes it the sharpest case to prove the
compare-and-swap migration against.
"""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent

_GUARDED_WORKER = textwrap.dedent(
    """
    import sys, os
    sys.path.insert(0, sys.argv[1])
    os.environ["CLAUDE_INSIGHT_DATA_DIR"] = sys.argv[2]
    import server

    session_id = sys.argv[3]
    worker_id = sys.argv[4]
    iterations = int(sys.argv[5])

    for i in range(iterations):
        server.session_tag(session_id=session_id, tags="w{0}-t{1}".format(worker_id, i))
    """
)

_UNGUARDED_WORKER = textwrap.dedent(
    """
    import sys, os, time
    sys.path.insert(0, sys.argv[1])
    os.environ["CLAUDE_INSIGHT_DATA_DIR"] = sys.argv[2]
    from base.persistence import AtomicJsonStore
    from utils.path_resolver import get_config_dir

    session_id = sys.argv[3]
    worker_id = sys.argv[4]
    iterations = int(sys.argv[5])

    chain_index_file = get_config_dir() / ".chain-index.json"
    store = AtomicJsonStore(
        chain_index_file,
        default_factory=lambda: {"version": "1.0.0", "sessions": {}, "tag_index": {}},
    )

    for i in range(iterations):
        tag = "w{0}-t{1}".format(worker_id, i)
        index = store.load()
        session = index["sessions"].setdefault(session_id, {
            "parent": None, "children": [], "related": [],
            "tags": [], "project": "", "skill": "", "task_type": "",
            "summary": "", "created_at": "", "last_prompt": "",
        })
        existing = set(session.get("tags", []))
        existing.add(tag)
        session["tags"] = sorted(existing)
        time.sleep(0.001)
        try:
            store.save(index)
        except OSError:
            pass
    """
)


def _run_workers(script, data_dir, session_id, workers, iterations):
    """Launch worker subprocesses tagging the same session and wait for all.

    Args:
        script: Worker source, one of ``_GUARDED_WORKER`` or ``_UNGUARDED_WORKER``.
        data_dir: Temp directory each worker resolves its config dir under.
        session_id: Shared session id every worker tags concurrently.
        workers: Number of subprocesses to launch.
        iterations: Tag calls each subprocess makes.

    Returns:
        List of completed ``subprocess.Popen`` handles, one per worker.
    """
    procs = [
        subprocess.Popen(
            [
                sys.executable, "-c", script,
                str(_REPO_ROOT), str(data_dir), session_id, str(w), str(iterations),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        for w in range(workers)
    ]
    for proc in procs:
        out, err = proc.communicate(timeout=180)
        assert proc.returncode == 0, (
            "worker process failed: stdout={0!r} stderr={1!r}".format(out, err)
        )
    return procs


def _expected_tags(workers, iterations):
    """Return the full set of tags every worker should have added."""
    return {"w{0}-t{1}".format(w, i) for w in range(workers) for i in range(iterations)}


def _final_session_tags(data_dir, session_id):
    """Read the persisted chain index and return the session's tag set."""
    chain_index_file = Path(data_dir) / "config" / ".chain-index.json"
    index = json.loads(chain_index_file.read_text(encoding="utf-8"))
    return set(index["sessions"][session_id].get("tags", []))


class TestSessionTagCrossProcessConcurrency:
    """Regression coverage for the modify() migration of session_tag."""

    WORKERS = 6
    ITERATIONS = 10

    def test_unguarded_cycle_loses_tags(self, tmp_path):
        """Control. If this passes without loss, the guarded test below proves nothing.

        Reproduces the pre-migration load-modify-save cycle directly against
        ``AtomicJsonStore.load()``/``.save()``, bypassing ``modify()``, under
        the same worker and iteration counts used below.
        """
        session_id = "SESSION-CONTROL"
        _run_workers(_UNGUARDED_WORKER, tmp_path, session_id, self.WORKERS, self.ITERATIONS)

        final_tags = _final_session_tags(tmp_path, session_id)
        expected = _expected_tags(self.WORKERS, self.ITERATIONS)
        assert final_tags < expected, (
            "unguarded session tagging did not lose any tags under this "
            "contention level; the concurrency guarantee below is untested"
        )

    def test_modify_loses_no_tags_across_processes(self, tmp_path):
        """Every tag from every worker process must survive in session_tag."""
        session_id = "SESSION-GUARDED"
        _run_workers(_GUARDED_WORKER, tmp_path, session_id, self.WORKERS, self.ITERATIONS)

        final_tags = _final_session_tags(tmp_path, session_id)
        expected = _expected_tags(self.WORKERS, self.ITERATIONS)
        assert final_tags == expected

        chain_index_file = Path(tmp_path) / "config" / ".chain-index.json"
        index = json.loads(chain_index_file.read_text(encoding="utf-8"))
        tag_index = index.get("tag_index", {})
        for tag in expected:
            assert session_id in tag_index.get(tag, []), (
                "tag_index missing session_id for tag {0!r}".format(tag)
            )

        leftovers = [
            p.name for p in (Path(tmp_path) / "config").iterdir()
            if p.name not in (".chain-index.json",)
        ]
        assert leftovers == [], "temp or claim files were left behind: {0}".format(leftovers)
