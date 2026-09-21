#!/usr/bin/env python3
"""Console dashboard for watching worker.py/rangesolver progress in an xterm.

Shows currently active ranges - cross-referencing ranges.db's 'in_progress' rows against actually
running rangesolver processes (matched via each process's --log= argument, not the stored worker
pid, since that's worker.py's own pid, not its rangesolver child's) - so a range whose worker died
without releasing it shows up clearly as ORPHANED rather than silently looking active.

Also periodically kicks off solution_db.py's incremental ingest (new solutions from the ASCII
logs into solutions.db) and reads the resulting total back. That keeps the per-cycle cost down to
whatever's newly appended since the last check, rather than re-parsing the whole corpus from
scratch every time - but ingestion itself still runs in a background thread, since even the
incremental case can take a moment right after startup (first run for a file) or after a burst of
new solutions, and the process-list redraw shouldn't wait on it.
"""
import argparse
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import solution_db

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(SCRIPT_DIR, "ranges.db")
DEFAULT_HISTORY_DB = os.path.join(SCRIPT_DIR, "solution_counts.db")
DEFAULT_SOLUTIONS_DB = solution_db.DEFAULT_SOLUTIONS_DB
DEFAULT_SOLUTION_PATTERNS = solution_db.DEFAULT_SOLUTION_PATTERNS
REFRESH_INTERVAL = 2.0
SOLUTION_SCAN_INTERVAL = 60.0

# Matt Parker's cited total (see ../notes.txt): 352,072 "L" + 1,303,584 "I" + 74,624 other.
# Flagged there as unverified - our own nsolver run on the smaller n=8 case didn't match his
# claimed counts - so this is a rough finish line, not a confirmed authoritative total.
TARGET_SOLUTIONS = 1_730_280

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


def ensure_history_schema(history_db_path):
    conn = sqlite3.connect(history_db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS solution_counts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            timestamp REAL NOT NULL,
            count INTEGER NOT NULL
        );
    """)
    conn.commit()
    conn.close()


def record_solution_count(history_db_path, timestamp, count):
    conn = sqlite3.connect(history_db_path, timeout=10)
    conn.execute("INSERT INTO solution_counts (timestamp, count) VALUES (?, ?)",
                 (timestamp, count))
    conn.commit()
    conn.close()


def compute_average_rate(history_db_path):
    """Average solutions/sec between the earliest and latest recorded samples. Returns
    (rate, sample_count, span_seconds), or None if there isn't enough history yet."""
    conn = sqlite3.connect(history_db_path, timeout=10)
    try:
        n = conn.execute("SELECT COUNT(*) FROM solution_counts").fetchone()[0]
        if n < 2:
            return None
        earliest = conn.execute(
            "SELECT timestamp, count FROM solution_counts ORDER BY timestamp ASC LIMIT 1"
        ).fetchone()
        latest = conn.execute(
            "SELECT timestamp, count FROM solution_counts ORDER BY timestamp DESC LIMIT 1"
        ).fetchone()
    finally:
        conn.close()
    span = latest[0] - earliest[0]
    if span <= 0:
        return None
    rate = (latest[1] - earliest[1]) / span
    return rate, n, span


class SolutionCounter:
    """Kicks off solution_db's incremental ingest on its own timer, in a background thread, so the
    (much cheaper) process-list redraw never blocks on it. Each fresh total also gets logged to
    the history database with a timestamp, for the average-rate display."""

    def __init__(self, patterns, interval, solutions_db_path, history_db_path):
        self.patterns = patterns
        self.interval = interval
        self.solutions_db_path = solutions_db_path
        self.history_db_path = history_db_path
        self.lock = threading.Lock()
        self.total = None
        self.last_updated = None
        self.computing = True
        solution_db.ensure_schema(solutions_db_path)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        while True:
            total = solution_db.ingest_all(self.solutions_db_path, self.patterns)
            now = time.time()
            with self.lock:
                self.total = total
                self.last_updated = now
                self.computing = False
            record_solution_count(self.history_db_path, now, total)
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


def format_rate(rate_info):
    if rate_info is None:
        return "average rate: (not enough history yet)"
    rate, n, span = rate_info
    if span < 3600:
        span_str = f"{int(span // 60)}m{int(span % 60):02d}s"
    else:
        span_str = f"{int(span // 3600)}h{int((span % 3600) // 60):02d}m"
    return f"average rate: {rate:.3f} solutions/sec (over {span_str}, {n} samples)"


def format_duration(seconds):
    if seconds < 60:
        return f"{seconds:.0f}s"
    minutes = seconds / 60
    if minutes < 60:
        return f"{minutes:.1f}m"
    hours = minutes / 60
    if hours < 24:
        return f"{hours:.1f}h"
    return f"{hours / 24:.1f}d"


def format_eta(total, rate_info):
    label = f"ETA (vs. unverified target of {TARGET_SOLUTIONS})"
    if total is None:
        return f"{label}: waiting on initial solution count..."
    if rate_info is None:
        return f"{label}: not enough rate history yet"
    rate, _n, _span = rate_info
    remaining = TARGET_SOLUTIONS - total
    if remaining <= 0:
        return f"{label}: target already reached"
    if rate <= 0:
        return f"{label}: unknown (current rate is zero)"
    eta_seconds = remaining / rate
    completion = datetime.now(timezone.utc) + timedelta(seconds=eta_seconds)
    return (f"{label}: {format_duration(eta_seconds)} remaining, "
            f"{remaining} solutions to go - est. completion "
            f"{completion.strftime('%Y-%m-%d %H:%M UTC')}")


def render(conn, counter, width, history_db_path):
    running = list_running_rangesolvers()
    active = fetch_active_ranges(conn)
    counts, tracked = fetch_summary(conn)
    total, last_updated, computing = counter.snapshot()
    rate_info = compute_average_rate(history_db_path)

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
    lines.append(format_rate(rate_info))
    lines.append(format_eta(total, rate_info))
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
    parser.add_argument("--history-db", default=DEFAULT_HISTORY_DB,
                         help="SQLite database to log (timestamp, unique-solution-count) samples "
                              f"to, for the average-rate display (default: {DEFAULT_HISTORY_DB})")
    parser.add_argument("--solutions-db", default=DEFAULT_SOLUTIONS_DB,
                         help="SQLite database that solution_db.py ingests solutions into "
                              f"(default: {DEFAULT_SOLUTIONS_DB})")
    parser.add_argument("--solutions", action="append", default=None,
                         help="Solution log file or glob pattern to ingest; may be repeated. "
                              "Default: CppSolver/Solutions/soln_log.txt and "
                              "Claude/Solutions/*.txt")
    parser.add_argument("--refresh", type=float, default=REFRESH_INTERVAL,
                         help=f"Seconds between process-list redraws (default: {REFRESH_INTERVAL})")
    parser.add_argument("--scan-interval", type=float, default=SOLUTION_SCAN_INTERVAL,
                         help="Seconds between solution_db ingest passes "
                              f"(default: {SOLUTION_SCAN_INTERVAL})")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"Error: database '{args.db}' does not exist - run bootstrap_ranges.py first.",
              file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.db, timeout=10)
    ensure_history_schema(args.history_db)
    counter = SolutionCounter(args.solutions or DEFAULT_SOLUTION_PATTERNS, args.scan_interval,
                               args.solutions_db, args.history_db)

    is_tty = sys.stdout.isatty()
    if is_tty:
        print("\033[?25l", end="")  # hide cursor
    try:
        while True:
            width = shutil.get_terminal_size((100, 24)).columns
            text = render(conn, counter, width, args.history_db)
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
