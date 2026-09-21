#!/usr/bin/env python3
"""Console dashboard for watching worker.py/rangesolver progress in an xterm.

Shows currently active ranges - cross-referencing ranges.db's 'in_progress' rows against actually
running rangesolver processes (matched via each process's --log= argument, not the stored worker
pid, since that's worker.py's own pid, not its rangesolver child's) - so a range whose worker died
without releasing it shows up clearly as ORPHANED rather than silently looking active.

Also periodically recomputes the true deduplicated solution count across the whole corpus. That
scan reads every solution file from scratch (same approach as bootstrap_ranges.py) and gets more
expensive as more per-range log files accumulate, so it runs on its own timer in a background
thread rather than blocking the fast (process list) redraw loop.
"""
import argparse
import glob
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import solution
import splitlog

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(SCRIPT_DIR, "ranges.db")
DEFAULT_SOLUTION_PATTERNS = [
    os.path.join(SCRIPT_DIR, "../CppSolver/Solutions/soln_log.txt"),
    os.path.join(SCRIPT_DIR, "../Claude/Solutions/*.txt"),
]
REFRESH_INTERVAL = 2.0
SOLUTION_SCAN_INTERVAL = 60.0

LOG_ARG_RE = re.compile(r"--log=Solutions/(\S+)")


def pid_alive(pid):
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def list_running_rangesolvers():
    """Returns {log_file_basename: pid} for every currently running rangesolver process, parsed
    from its own --log= argument - this is the process's own pid, distinct from the pid stored in
    ranges.db (which is the worker.py process that launched it, not the rangesolver child itself)."""
    result = {}
    try:
        out = subprocess.check_output(["ps", "-eo", "pid,args"], text=True,
                                       env={**os.environ, "COLUMNS": "2000"})
    except (subprocess.CalledProcessError, OSError):
        return result
    for line in out.splitlines()[1:]:
        line = line.strip()
        if "rangesolver" not in line:
            continue
        pid_str, _, args = line.partition(" ")
        m = LOG_ARG_RE.search(args)
        if not m:
            continue
        try:
            result[m.group(1)] = int(pid_str)
        except ValueError:
            continue
    return result


def fetch_active_ranges(conn):
    return conn.execute(
        "SELECT id, prefix, range_start, pid, claimed_at, log_file "
        "FROM ranges WHERE status='in_progress' ORDER BY id"
    ).fetchall()


def fetch_summary(conn):
    counts = dict(conn.execute("SELECT status, COUNT(*) FROM ranges GROUP BY status").fetchall())
    tracked = conn.execute(
        "SELECT COALESCE(SUM(solutions_found), 0) FROM ranges WHERE status='done'"
    ).fetchone()[0]
    return counts, tracked


def resolve_solution_files(patterns):
    files = []
    for pattern in patterns:
        matched = glob.glob(pattern)
        files.extend(matched if matched else [pattern])
    return files


def compute_total_unique_solutions(patterns):
    hashes = set()
    for fn in resolve_solution_files(patterns):
        try:
            with open(fn) as f:
                lines = f.readlines()
        except OSError:
            continue
        for buf in splitlog.get_raw_buffers(lines):
            hashes.add(solution.make_solution_from_lines(buf).get_hash())
    return len(hashes)


class SolutionCounter:
    """Recomputes the true unique solution count on its own timer, in a background thread, so the
    (much cheaper) process-list redraw never blocks on this expensive full-corpus scan."""

    def __init__(self, patterns, interval):
        self.patterns = patterns
        self.interval = interval
        self.lock = threading.Lock()
        self.total = None
        self.last_updated = None
        self.computing = True
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            total = compute_total_unique_solutions(self.patterns)
            with self.lock:
                self.total = total
                self.last_updated = time.time()
                self.computing = False
            time.sleep(self.interval)
            with self.lock:
                self.computing = True

    def snapshot(self):
        with self.lock:
            return self.total, self.last_updated, self.computing


def format_age(sqlite_datetime_str):
    if not sqlite_datetime_str:
        return "?"
    try:
        claimed = datetime.strptime(sqlite_datetime_str, "%Y-%m-%d %H:%M:%S")
        claimed = claimed.replace(tzinfo=timezone.utc)
    except ValueError:
        return "?"
    seconds = (datetime.now(timezone.utc) - claimed).total_seconds()
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"
    return f"{int(seconds // 3600)}h{int((seconds % 3600) // 60):02d}m"


def render(conn, counter, width):
    running = list_running_rangesolvers()
    active = fetch_active_ranges(conn)
    counts, tracked = fetch_summary(conn)
    total, last_updated, computing = counter.snapshot()

    lines = []
    lines.append("Partridge Puzzle Dashboard".center(width))
    lines.append("=" * width)
    lines.append(f"unsearched={counts.get('unsearched', 0)}  "
                 f"in_progress={counts.get('in_progress', 0)}  "
                 f"done={counts.get('done', 0)}  "
                 f"solutions counted by finished ranges={tracked}")
    if total is None:
        lines.append("unique solutions found: (computing initial total...)")
    else:
        age = f"{int(time.time() - last_updated)}s ago" if last_updated else "?"
        note = " (recomputing now)" if computing else ""
        lines.append(f"unique solutions found: {total}  (as of {age}){note}")
    lines.append("-" * width)
    lines.append(f"{'ID':>5}  {'PREFIX':<16}{'STATUS':<20}{'ELAPSED':>9}  CURRENT POSITION")
    lines.append("-" * width)
    if not active:
        lines.append("  (no ranges currently in progress)")
    for range_id, prefix, range_start, worker_pid, claimed_at, log_file in active:
        proc_pid = running.get(log_file)
        if proc_pid is not None:
            status = f"running (pid {proc_pid})"
        elif pid_alive(worker_pid):
            status = "starting"
        else:
            status = "ORPHANED"
        elapsed = format_age(claimed_at)
        lines.append(f"{range_id:>5}  {prefix:<16}{status:<20}{elapsed:>9}  {range_start}")
    lines.append("=" * width)
    lines.append("Ctrl-C to exit")
    return "\n".join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_DB,
                         help=f"SQLite database to read (default: {DEFAULT_DB})")
    parser.add_argument("--solutions", action="append", default=None,
                         help="Solution log file or glob pattern to scan for the unique-solution "
                              "count; may be repeated. Default: CppSolver/Solutions/soln_log.txt "
                              "and Claude/Solutions/*.txt")
    parser.add_argument("--refresh", type=float, default=REFRESH_INTERVAL,
                         help=f"Seconds between process-list redraws (default: {REFRESH_INTERVAL})")
    parser.add_argument("--scan-interval", type=float, default=SOLUTION_SCAN_INTERVAL,
                         help="Seconds between full-corpus unique-solution rescans "
                              f"(default: {SOLUTION_SCAN_INTERVAL})")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"Error: database '{args.db}' does not exist - run bootstrap_ranges.py first.",
              file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.db, timeout=10)
    counter = SolutionCounter(args.solutions or DEFAULT_SOLUTION_PATTERNS, args.scan_interval)

    is_tty = sys.stdout.isatty()
    if is_tty:
        print("\033[?25l", end="")  # hide cursor
    try:
        while True:
            width = shutil.get_terminal_size((100, 24)).columns
            text = render(conn, counter, width)
            if is_tty:
                print("\033[H\033[J", end="")  # cursor home, clear screen
                print(text)
            else:
                print(text)
                print()
            sys.stdout.flush()
            time.sleep(args.refresh)
    except KeyboardInterrupt:
        pass
    finally:
        if is_tty:
            print("\033[?25h")  # restore cursor
        conn.close()


if __name__ == "__main__":
    main()
