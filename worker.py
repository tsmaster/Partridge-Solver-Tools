#!/usr/bin/env python3
"""Claims and searches ranges from the database bootstrap_ranges.py seeds, running rangesolver
as a subprocess for each until nothing remains.

Multiple independent copies of this script - one per xterm window, started and stopped freely -
coordinate purely through the shared SQLite database; there is no central manager process. Range
claims use a SQLite BEGIN IMMEDIATE transaction so two workers can never claim the same range.

Resilience against interruption (deliberate or not - a killed worker, a closed window, a power
outage) works two ways: a graceful stop (Ctrl-C, SIGTERM, or the window closing) releases the
current range back for reclaim, recording whatever progress was made; an ungraceful one (kill -9,
power loss) leaves the range claimed under a now-dead pid, which the next worker to look for work
detects (the pid no longer exists) and reclaims automatically - no separate cleanup step needed.
"""
import argparse
import os
import re
import signal
import sqlite3
import subprocess
import sys

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(SCRIPT_DIR, "ranges.db")
CLAUDE_DIR = os.path.join(SCRIPT_DIR, "..", "Claude")
RANGESOLVER = os.path.join(CLAUDE_DIR, "rangesolver")

PATH_RE = re.compile(r"path=(\d+)\s*$")
DONE_RE = re.compile(r"Done\. (\d+) solution")

_current_child = None
_shutdown_requested = False


def handle_signal(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    if _current_child is not None:
        _current_child.terminate()


def pid_alive(pid):
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def claim_range(conn):
    """Atomically claims one range: prefers reclaiming an orphaned in_progress row (dead or
    already-cleared pid) over starting fresh work, so interrupted ranges get finished first.
    Returns a dict describing the claimed range, or None if nothing is available."""
    conn.execute("BEGIN IMMEDIATE;")
    try:
        orphaned = None
        for row in conn.execute(
            "SELECT id, prefix, range_start, range_end, pid FROM ranges "
            "WHERE status='in_progress' ORDER BY range_start DESC"
        ).fetchall():
            if not pid_alive(row[4]):
                orphaned = row
                break

        target = orphaned or conn.execute(
            "SELECT id, prefix, range_start, range_end, pid FROM ranges "
            "WHERE status='unsearched' ORDER BY range_start DESC LIMIT 1"
        ).fetchone()

        if target is None:
            conn.execute("COMMIT;")
            return None

        range_id, prefix, range_start, range_end, _old_pid = target
        conn.execute(
            "UPDATE ranges SET status='in_progress', pid=?, claimed_at=datetime('now') "
            "WHERE id=?",
            (os.getpid(), range_id),
        )
        conn.execute("COMMIT;")
        return {"id": range_id, "prefix": prefix, "start": range_start, "end": range_end}
    except Exception:
        conn.execute("ROLLBACK;")
        raise


def release_range(conn, range_id, reset_start=None):
    """Leaves a range reclaimable again: clears the pid, and if we know how far it actually got,
    updates the resume point so the next attempt doesn't repeat completed work."""
    if reset_start:
        conn.execute("UPDATE ranges SET pid=NULL, range_start=? WHERE id=?",
                      (reset_start, range_id))
    else:
        conn.execute("UPDATE ranges SET pid=NULL WHERE id=?", (range_id,))
    conn.commit()


def finish_range(conn, range_id, solutions_found):
    conn.execute(
        "UPDATE ranges SET status='done', finished_at=datetime('now'), pid=NULL, "
        "solutions_found=? WHERE id=?",
        (solutions_found, range_id),
    )
    conn.commit()


def update_progress(conn, range_id, path):
    conn.execute("UPDATE ranges SET range_start=? WHERE id=?", (path, range_id))
    conn.commit()


def run_range(conn, work):
    """Runs one claimed range to completion (or interruption). Returns False if the caller
    should stop entirely (fatal launch failure, or a shutdown was requested), True to keep going."""
    global _current_child
    range_id = work["id"]
    log_name = f"soln_{work['prefix']}_{range_id}.txt"
    conn.execute("UPDATE ranges SET log_file=? WHERE id=?", (log_name, range_id))
    conn.commit()

    cmd = [
        os.path.abspath(RANGESOLVER),
        f"--start={work['start']}",
        f"--end={work['end']}",
        f"--log=Solutions/{log_name}",
        "--status=scroll",
    ]
    print(f"[worker {os.getpid()}] claimed range {range_id} (prefix {work['prefix']}): "
          f"--start={work['start']} --end={work['end']}", flush=True)

    try:
        proc = subprocess.Popen(cmd, cwd=CLAUDE_DIR, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
    except OSError as e:
        print(f"[worker {os.getpid()}] failed to launch rangesolver: {e}", file=sys.stderr)
        release_range(conn, range_id)
        return False

    _current_child = proc
    last_path = None
    solutions_found = None
    for line in proc.stdout:
        line = line.rstrip()
        print(f"[{work['prefix']}] {line}", flush=True)
        m = PATH_RE.search(line)
        if m:
            last_path = m.group(1)
            update_progress(conn, range_id, last_path)
            continue
        m = DONE_RE.search(line)
        if m:
            solutions_found = int(m.group(1))

    proc.wait()
    _current_child = None

    if _shutdown_requested:
        release_range(conn, range_id, reset_start=last_path)
        return False

    if proc.returncode == 0 and solutions_found is not None:
        finish_range(conn, range_id, solutions_found)
        print(f"[worker {os.getpid()}] finished range {range_id}: "
              f"{solutions_found} solution(s).", flush=True)
    else:
        print(f"[worker {os.getpid()}] range {range_id} did not complete cleanly "
              f"(exit={proc.returncode}); leaving it for reclaim.", file=sys.stderr)
        release_range(conn, range_id, reset_start=last_path)

    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB,
                         help=f"SQLite database to claim ranges from (default: {DEFAULT_DB})")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"Error: database '{args.db}' does not exist - run bootstrap_ranges.py first.",
              file=sys.stderr)
        sys.exit(1)

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, handle_signal)

    conn = sqlite3.connect(args.db, timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")

    while not _shutdown_requested:
        work = claim_range(conn)
        if work is None:
            print(f"[worker {os.getpid()}] no ranges remain. Exiting.", flush=True)
            break
        if _shutdown_requested:
            release_range(conn, work["id"])
            break
        if not run_range(conn, work):
            break

    conn.close()


if __name__ == "__main__":
    main()
