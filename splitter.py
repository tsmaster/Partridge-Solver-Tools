#!/usr/bin/env python3
"""Splits long-running ranges into smaller sub-ranges so a single outlier can't monopolize a
worker indefinitely while everything else races ahead - see the real-world evidence for range #1
(prefix 99999, still running after 24h+ while thousands of others finished) in Claude/TODO.txt.

Runs as its own long-lived process (its own xterm, like worker.py/dashboard.py) - periodically
scans ranges.db for any 'in_progress' range whose claimed_at is older than
--age-threshold-hours, and splits each one it finds:

  1. Atomically flips it from 'in_progress' to 'held_for_split' (a BEGIN IMMEDIATE transaction
     conditioned on the row still being 'in_progress' - if the range finished naturally in the
     meantime this affects 0 rows and the split is abandoned, no harm done). This makes it
     immediately invisible to worker.py's claim_range(), which only ever looks for
     'unsearched'/'in_progress' rows - so unlike those two, 'held_for_split' needs no changes to
     the claim logic at all.
  2. Finds and kills the actual rangesolver child process currently searching that range (matched
     by its --log= argument against the range's own log_file column, via
     dashboard.list_running_rangesolvers() - not the pid column, which is worker.py's own pid, not
     its rangesolver child's).
  3. Waits briefly for the owning worker.py to notice its child exited uncleanly and run its
     existing "did not complete cleanly; leaving it for reclaim" path - release_range(), which
     clears pid and updates range_start to the last known position, but never touches status - so
     the row stays 'held_for_split' throughout. worker.py needs zero changes to support this: it
     can't tell an on-purpose split from any other subprocess crash, and doesn't need to.
  4. Reads back the resulting (possibly deeper) range_start as the freshest known position, and
     enumerates one digit deeper than the range's own end prefix, comparing each of the (up to) 9
     candidate sub-prefixes against that position - using the same "largest tile tried first"
     ordering rangesolver's own --start/--end bounds rely on - to classify each as already fully
     covered (skip it entirely), the one currently in progress (a resumed sub-range from the live
     position), or untouched (a fresh sub-range covering its own whole subtree). Exactly the manual
     procedure worked out by hand for range #1 in TODO.txt, now automatic and general to any range.
  5. Inserts an 'unsearched' row per surviving sub-range, and marks the original row 'split'
     (distinct from 'done' - it was subdivided, not searched to completion as one unit) so
     dashboard.py's status counts stay accurate. A sub-range that itself later runs long enough
     gets split again on a later pass, the same way - no depth limit needed beyond FRAME_WIDTH.
"""
import argparse
import os
import signal
import sqlite3
import subprocess
import sys
import time

import dashboard
import rotate_priority
import solution_db

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(SCRIPT_DIR, "ranges.db")
CLAUDE_DIR = os.path.join(SCRIPT_DIR, "..", "Claude")
FRAME_WIDTH = 45

DEFAULT_AGE_THRESHOLD_HOURS = 2.0
DEFAULT_CHECK_INTERVAL = 300.0  # 5 minutes
KILL_WAIT_TIMEOUT = 30.0
KILL_WAIT_POLL = 0.5


def classify_and_build_subranges(end_prefix, live_position):
    """end_prefix: the range's fixed subtree boundary (its range_end - always a prefix of
    live_position). live_position: the freshest known range_start. Returns a list of
    (sub_prefix, sub_start, sub_end) tuples to insert as new 'unsearched' ranges - one per digit
    1-9 that isn't already fully covered, in the same 9-down-to-1 order the solver visits them.

    A digit d is already fully covered iff d > next_digit: rangesolver always tries the largest
    remaining tile first (see full_solver's `for sz=9; sz>0; --sz`), so by the time the live
    position has moved on to next_digit, every larger digit's whole subtree at this depth has
    necessarily already been exhausted."""
    if len(end_prefix) >= FRAME_WIDTH:
        raise ValueError(f"end prefix {end_prefix!r} is already at FRAME_WIDTH={FRAME_WIDTH}; "
                          f"can't split deeper")
    if not live_position.startswith(end_prefix):
        raise ValueError(f"live position {live_position!r} doesn't start with "
                          f"end prefix {end_prefix!r} - refusing to split")

    depth = len(end_prefix)
    next_digit = int(live_position[depth]) if len(live_position) > depth else None

    subranges = []
    for d in range(9, 0, -1):
        sub_prefix = end_prefix + str(d)
        if next_digit is None:
            subranges.append((sub_prefix, sub_prefix, sub_prefix))  # nothing chosen yet - fresh
        elif d > next_digit:
            continue  # exhausted before the live position was reached - fully covered, skip
        elif d == next_digit:
            subranges.append((sub_prefix, live_position, sub_prefix))  # the live branch - resume
        else:
            subranges.append((sub_prefix, sub_prefix, sub_prefix))  # not reached yet - fresh
    return subranges


def find_rangesolver_pid(log_file):
    return dashboard.list_running_rangesolvers().get(log_file)


def count_solutions_in_log(log_file):
    """Best-effort - only feeds the informational solutions_found column (dashboard.py's
    "tracked" total), never the authoritative count, which solution_db.py derives independently
    straight from the log files regardless of what ranges.db records here."""
    if not log_file:
        return None
    path = os.path.join(CLAUDE_DIR, "Solutions", log_file)
    try:
        with open(path, "rb") as f:
            return sum(1 for line in f if line.rstrip(b"\n") == b"---")
    except OSError:
        return None


def split_one(conn, range_id, prefix, range_end, log_file, claimed_at):
    conn.execute("BEGIN IMMEDIATE;")
    cur = conn.execute(
        "UPDATE ranges SET status='held_for_split' WHERE id=? AND status='in_progress'",
        (range_id,),
    )
    won = cur.rowcount > 0
    conn.execute("COMMIT;")
    if not won:
        print(f"[splitter] range {range_id} no longer in_progress (finished naturally?) - "
              f"skipping.", flush=True)
        return

    print(f"[splitter] range {range_id} (prefix {prefix}) held for split "
          f"(in_progress since {claimed_at}).", flush=True)

    pid = find_rangesolver_pid(log_file) if log_file else None
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"[splitter] sent SIGTERM to rangesolver pid {pid} for range {range_id}.",
                  flush=True)
        except OSError as e:
            print(f"[splitter] couldn't signal pid {pid} for range {range_id}: {e}", flush=True)

        deadline = time.monotonic() + KILL_WAIT_TIMEOUT
        while time.monotonic() < deadline:
            cur_pid, status = conn.execute(
                "SELECT pid, status FROM ranges WHERE id=?", (range_id,)
            ).fetchone()
            if cur_pid is None or status != 'held_for_split':
                break
            time.sleep(KILL_WAIT_POLL)
    else:
        print(f"[splitter] no running rangesolver found for range {range_id} "
              f"(log_file={log_file!r}) - likely already orphaned; proceeding.", flush=True)

    live_start, status = conn.execute(
        "SELECT range_start, status FROM ranges WHERE id=?", (range_id,)
    ).fetchone()

    if status != 'held_for_split':
        print(f"[splitter] range {range_id} status changed to {status!r} while waiting "
              f"(finished naturally?) - abandoning split.", flush=True)
        return

    try:
        subranges = classify_and_build_subranges(range_end, live_start)
    except ValueError as e:
        print(f"[splitter] range {range_id}: {e} - leaving held_for_split for manual review.",
              file=sys.stderr, flush=True)
        return

    solutions_found = count_solutions_in_log(log_file)

    conn.execute("BEGIN IMMEDIATE;")
    for sub_prefix, sub_start, sub_end in subranges:
        conn.execute(
            "INSERT INTO ranges (prefix, range_start, range_end, status) "
            "VALUES (?, ?, ?, 'unsearched')",
            (sub_prefix, sub_start, sub_end),
        )
    conn.execute(
        "UPDATE ranges SET status='split', finished_at=datetime('now'), pid=NULL, "
        "solutions_found=? WHERE id=?",
        (solutions_found, range_id),
    )
    conn.execute("COMMIT;")

    solutions_note = f" ({solutions_found} solutions counted before splitting)" \
        if solutions_found is not None else ""
    print(f"[splitter] range {range_id} (prefix {prefix}) split into {len(subranges)} "
          f"sub-range(s): {', '.join(p for p, _, _ in subranges)}{solutions_note}.", flush=True)


def run_once(conn, age_threshold_hours):
    rows = conn.execute(
        "SELECT id, prefix, range_end, log_file, claimed_at FROM ranges "
        "WHERE status='in_progress' AND claimed_at <= datetime('now', ?) "
        "ORDER BY claimed_at ASC",
        (f"-{age_threshold_hours} hours",),
    ).fetchall()
    for range_id, prefix, range_end, log_file, claimed_at in rows:
        split_one(conn, range_id, prefix, range_end, log_file, claimed_at)


def rotate_priority_running():
    try:
        out = subprocess.check_output(["ps", "-eo", "args"], text=True)
    except (subprocess.CalledProcessError, OSError):
        return False
    return any("rotate_priority.py" in line for line in out.splitlines())


def maybe_refresh_priority(conn):
    """Re-runs rotate_priority.py in the background once the priority pool has run dry (no
    'unsearched' range still flagged priority>0) - splitting a priority-flagged range doesn't
    propagate the flag to its children (a flag means one specific known rotation landed somewhere
    in that range's subtree, not uniformly across all of a split's children), so the pool only
    ever shrinks on its own; this is what refills it. Only bothers if there's actually something
    new to look for (the known-solution count has grown since rotate_priority.py's last full run)
    and only if an instance isn't already running, so this can't pile up redundant 5-minute scans."""
    remaining = conn.execute(
        "SELECT COUNT(*) FROM ranges WHERE priority > 0 AND status='unsearched'"
    ).fetchone()[0]
    if remaining > 0:
        return
    if rotate_priority_running():
        return

    solution_db.ensure_schema(solution_db.DEFAULT_SOLUTIONS_DB)
    current_count = solution_db.ingest_all(solution_db.DEFAULT_SOLUTIONS_DB,
                                            solution_db.DEFAULT_SOLUTION_PATTERNS)
    last_count = rotate_priority.read_last_processed_count()
    if current_count <= last_count:
        return

    print(f"[splitter] priority pool empty and {current_count - last_count} new solution(s) "
          f"found since the last scan - relaunching rotate_priority.py in the background.",
          flush=True)
    subprocess.Popen([sys.executable, os.path.join(SCRIPT_DIR, "rotate_priority.py")])


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default=DEFAULT_DB,
                         help=f"SQLite database to operate on (default: {DEFAULT_DB})")
    parser.add_argument("--age-threshold-hours", type=float, default=DEFAULT_AGE_THRESHOLD_HOURS,
                         help=f"Split any in_progress range claimed longer ago than this "
                              f"(default: {DEFAULT_AGE_THRESHOLD_HOURS}).")
    parser.add_argument("--check-interval", type=float, default=DEFAULT_CHECK_INTERVAL,
                         help=f"Seconds between scans (default: {DEFAULT_CHECK_INTERVAL}).")
    parser.add_argument("--once", action="store_true",
                         help="Run a single scan/split pass and exit, instead of looping forever.")
    args = parser.parse_args()

    if not os.path.exists(args.db):
        print(f"Error: database '{args.db}' does not exist - run bootstrap_ranges.py first.",
              file=sys.stderr)
        sys.exit(1)

    conn = sqlite3.connect(args.db, timeout=30, isolation_level=None)
    conn.execute("PRAGMA journal_mode=WAL;")

    try:
        while True:
            run_once(conn, args.age_threshold_hours)
            maybe_refresh_priority(conn)
            if args.once:
                break
            time.sleep(args.check_interval)
    except KeyboardInterrupt:
        pass
    finally:
        conn.close()


if __name__ == "__main__":
    main()
