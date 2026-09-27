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
import random
import re
import signal
import sqlite3
import subprocess
import sys
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(SCRIPT_DIR, "ranges.db")
CLAUDE_DIR = os.path.join(SCRIPT_DIR, "..", "Claude")
RANGESOLVER = os.path.join(CLAUDE_DIR, "rangesolver")

PROGRESS_RE = re.compile(r"\[t=([\d.]+)s\] depth=\s*(\d+) solutions=(\d+) path=(\d+)\s*$")
DONE_RE = re.compile(r"Done\. (\d+) solution")

DB_RETRY_ATTEMPTS = 6
DB_RETRY_BASE_DELAY = 0.05  # seconds; backs off exponentially with jitter between attempts

# Caps how many workers can simultaneously be claimed on ranges sharing the same length-5
# lineage root (the original bootstrapped prefix a range - or any of its split descendants -
# ultimately came from). Without this, a single bushy branch that keeps needing to be split
# recaptures every worker that frees up (its freshly split children still sort first by the
# range_start DESC ordering below, same as their parent did), starving the rest of the backlog
# entirely - observed live 2026-09-23: a whole 6-worker pool sat on one lineage for 4+ hours
# while 41,000+ other ranges made zero progress. This is deliberately not "no more than N
# workers per split, ever" - it's per lineage ROOT, so splitting still helps: it just caps how
# much of the total worker pool any single original branch can monopolize at once.
MAX_WORKERS_PER_LINEAGE = 2

_current_child = None
_shutdown_requested = False
_remote_host = None
_remote_dir = None
_ssh_key = None


def handle_signal(signum, frame):
    global _shutdown_requested
    _shutdown_requested = True
    if _current_child is not None:
        _current_child.terminate()


def with_db_retry(fn, *args, **kwargs):
    """Retries a SQLite operation on a transient 'database is locked'/'database is busy' error,
    with exponential backoff plus jitter. The connection's own busy_timeout (set via
    connect(timeout=...)) already retries internally for a while, but under enough concurrent
    writers that can still not be enough - confirmed live, 2026-09-23: a worker crashed outright
    on update_progress() with "database is locked" right after the per-lineage claim cap started
    spreading work across many more concurrently-active ranges, so more workers hitting ranges.db
    at once than before. Losing a whole worker over one transient lock is worse than a short,
    bounded wait here. Deliberately narrow - only retries this specific error, raising immediately
    for anything else, and re-raises on the final attempt too rather than silently giving up."""
    for attempt in range(DB_RETRY_ATTEMPTS):
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as e:
            msg = str(e).lower()
            if "locked" not in msg and "busy" not in msg:
                raise
            if attempt == DB_RETRY_ATTEMPTS - 1:
                raise
            delay = DB_RETRY_BASE_DELAY * (2 ** attempt) + random.uniform(0, DB_RETRY_BASE_DELAY)
            print(f"[worker {os.getpid()}] db {e} (attempt {attempt + 1}/{DB_RETRY_ATTEMPTS}), "
                  f"retrying in {delay:.2f}s", file=sys.stderr)
            time.sleep(delay)


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
    Among fresh unsearched work, prefers a range whose length-5 lineage root isn't already at
    MAX_WORKERS_PER_LINEAGE active workers, falling back to the plain best candidate if every
    option is already at the cap (so a worker is never left idle while any unsearched range
    exists at all, even a fully-oversubscribed one). Within either tier, a range flagged priority>0
    (see tools/rotate_priority.py - ranges predicted to contain a rotation of an already-found
    solution) is preferred over the normal range_start DESC ordering.
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
            "SELECT id, prefix, range_start, range_end, pid FROM ranges r "
            "WHERE status='unsearched' AND ("
            "  SELECT COUNT(*) FROM ranges "
            "  WHERE status='in_progress' AND SUBSTR(prefix,1,5)=SUBSTR(r.prefix,1,5)"
            ") < ? "
            "ORDER BY priority DESC, range_start DESC LIMIT 1",
            (MAX_WORKERS_PER_LINEAGE,),
        ).fetchone() or conn.execute(
            "SELECT id, prefix, range_start, range_end, pid FROM ranges "
            "WHERE status='unsearched' ORDER BY priority DESC, range_start DESC LIMIT 1"
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


def set_log_file(conn, range_id, log_name):
    conn.execute("UPDATE ranges SET log_file=? WHERE id=?", (log_name, range_id))
    conn.commit()


def run_range(conn, work, use_inplace):
    """Runs one claimed range to completion (or interruption). Returns False if the caller
    should stop entirely (fatal launch failure, or a shutdown was requested), True to keep going.

    rangesolver itself is always run with --status=scroll, regardless of use_inplace: its inplace
    mode uses \\r plus ANSI escapes rather than newlines between updates, which would break the
    line-by-line stdout parsing below. Instead, when use_inplace is set, this function renders its
    own overwriting status line from the progress it already parses - same UX, no parsing risk."""
    global _current_child
    range_id = work["id"]
    log_name = f"soln_{work['prefix']}_{range_id}.txt"
    with_db_retry(set_log_file, conn, range_id, log_name)

    solver_args = [
        f"--start={work['start']}",
        f"--end={work['end']}",
        f"--log=Solutions/{log_name}",
        "--status=scroll",
    ]

    if _remote_host:
        # The remote host mounts our Claude/Solutions/ directory (via SSHFS, onto
        # <_remote_dir>/Solutions) so its rangesolver writes the log straight onto our real
        # filesystem - set_log_file() above already stores just the bare filename, exactly as it
        # would for a local run, so nothing else in this script needs to know or care that the
        # range is being searched remotely.
        remote_cmd = f"cd {_remote_dir} && ./rangesolver " + " ".join(solver_args)
        cmd = ["ssh", "-i", _ssh_key, "-o", "BatchMode=yes", _remote_host, remote_cmd]
        location = f"remote:{_remote_host}"
    else:
        cmd = [os.path.abspath(RANGESOLVER)] + solver_args
        location = "local"

    print(f"[worker {os.getpid()}] claimed range {range_id} (prefix {work['prefix']}, "
          f"{location}): --start={work['start']} --end={work['end']}", flush=True)

    try:
        proc = subprocess.Popen(cmd, cwd=CLAUDE_DIR, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
    except OSError as e:
        print(f"[worker {os.getpid()}] failed to launch rangesolver: {e}", file=sys.stderr)
        with_db_retry(release_range, conn, range_id)
        return False

    _current_child = proc
    last_path = None
    solutions_found = None
    status_line_open = False
    for line in proc.stdout:
        line = line.rstrip()
        m = PROGRESS_RE.search(line)
        if m:
            elapsed, depth, solutions, path = m.groups()
            last_path = path
            with_db_retry(update_progress, conn, range_id, path)
            status = (f"[worker {os.getpid()}] range {range_id} (prefix {work['prefix']}) "
                      f"t={elapsed}s depth={depth} solutions={solutions} path={path}")
            if use_inplace:
                print(f"\r\033[K{status}", end="", flush=True)
                status_line_open = True
            else:
                print(status, flush=True)
            continue

        if status_line_open:
            print()  # move off the in-place status line before printing a normal line
            status_line_open = False
        print(f"[{work['prefix']}] {line}", flush=True)

        m = DONE_RE.search(line)
        if m:
            solutions_found = int(m.group(1))

    if status_line_open:
        print()
    proc.wait()
    _current_child = None

    if _shutdown_requested:
        with_db_retry(release_range, conn, range_id, reset_start=last_path)
        return False

    if proc.returncode == 0 and solutions_found is not None:
        with_db_retry(finish_range, conn, range_id, solutions_found)
        print(f"[worker {os.getpid()}] finished range {range_id}: "
              f"{solutions_found} solution(s).", flush=True)
    else:
        print(f"[worker {os.getpid()}] range {range_id} did not complete cleanly "
              f"(exit={proc.returncode}); leaving it for reclaim.", file=sys.stderr)
        with_db_retry(release_range, conn, range_id, reset_start=last_path)

    return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB,
                         help=f"SQLite database to claim ranges from (default: {DEFAULT_DB})")
    parser.add_argument("--status", choices=["inplace", "scroll"], default=None,
                         help="How this worker prints its own progress line: 'inplace' overwrites "
                              "it in place, 'scroll' prints one line per update. Default: inplace "
                              "when stdout is a terminal, scroll otherwise (mirrors rangesolver's "
                              "own --status, though rangesolver itself is always run in scroll "
                              "mode internally so this script can parse its output).")
    parser.add_argument("--remote-host", default=None,
                         help="Run rangesolver over SSH on this host instead of locally (e.g. "
                              "wheeljack.local). This script still runs locally and does all its "
                              "usual claim/release/finish bookkeeping against --db directly - only "
                              "the CPU-heavy rangesolver subprocess is launched remotely. Requires "
                              "the remote host to have its own native rangesolver build in "
                              "--remote-dir, with that directory's Solutions/ SSHFS-mounted back "
                              "onto this machine's own Claude/Solutions/ so log files land in the "
                              "right place with no extra copy step.")
    parser.add_argument("--remote-dir", default="partridge_remote",
                         help="Directory on the remote host containing its rangesolver build and "
                              "the SSHFS-mounted Solutions/ (default: partridge_remote, i.e. "
                              "~/partridge_remote as the ssh login's home-relative default).")
    parser.add_argument("--ssh-key", default=os.path.expanduser("~/.ssh/partridge_worker_ed25519"),
                         help="Private key for connecting to --remote-host (default: "
                              "~/.ssh/partridge_worker_ed25519).")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"Error: database '{args.db}' does not exist - run bootstrap_ranges.py first.",
              file=sys.stderr)
        sys.exit(1)

    global _remote_host, _remote_dir, _ssh_key
    _remote_host = args.remote_host
    _remote_dir = args.remote_dir
    _ssh_key = args.ssh_key

    use_inplace = (args.status == "inplace") if args.status else sys.stdout.isatty()

    for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, handle_signal)

    conn = sqlite3.connect(args.db, timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")

    while not _shutdown_requested:
        work = with_db_retry(claim_range, conn)
        if work is None:
            print(f"[worker {os.getpid()}] no ranges remain. Exiting.", flush=True)
            break
        if _shutdown_requested:
            with_db_retry(release_range, conn, work["id"])
            break
        if not run_range(conn, work, use_inplace):
            break

    conn.close()


if __name__ == "__main__":
    main()
