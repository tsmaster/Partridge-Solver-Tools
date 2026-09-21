#!/usr/bin/env python3
"""Seeds a SQLite range-tracking database (for worker.py) from a depth-N prefix file (as produced
by rangesolver's --enumerate-depth mode) and whatever solutions have already been found.

For each existing solution file, the file's own (max, min) hash defines a provably-covered
interval: since one file comes from one continuous sweep, everything between its first and last
solution (in traversal order) was necessarily visited, whether or not it happened to be a
solution. This deliberately under-claims coverage at the edges (safe: a little redundant
re-search rather than ever silently skipping a real gap) rather than trusting a single "high
water mark" number, which assumes - unsafely - that all past work forms one contiguous sweep.

Every depth-N prefix is then checked against the merged set of these intervals; whatever isn't
covered becomes one or more 'unsearched' rows for worker.py to claim.
"""
import argparse
import glob
import os
import sqlite3
import sys

import solution
import splitlog

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_DB = os.path.join(SCRIPT_DIR, "ranges.db")
DEFAULT_SOLUTION_PATTERNS = [
    os.path.join(SCRIPT_DIR, "../CppSolver/Solutions/soln_log.txt"),
    os.path.join(SCRIPT_DIR, "../Claude/Solutions/*.txt"),
]


def resolve_solution_files(patterns):
    files = []
    for pattern in patterns:
        matched = glob.glob(pattern)
        files.extend(matched if matched else [pattern])
    return files


def file_interval(filename):
    """Returns (max_hash, min_hash) covered by one solution file, or None if it has none."""
    with open(filename) as f:
        lines = f.readlines()
    hashes = [solution.make_solution_from_lines(buf).get_hash()
              for buf in splitlog.get_raw_buffers(lines)]
    if not hashes:
        return None
    return (max(hashes), min(hashes))


def merge_intervals(intervals):
    """intervals: list of (hi, lo) string pairs. Returns them sorted descending by hi, with any
    overlapping or touching intervals combined - same-length digit strings compare correctly as
    plain Python strings, matching the search's own traversal order."""
    if not intervals:
        return []
    ordered = sorted(intervals, key=lambda iv: iv[0], reverse=True)
    merged = [list(ordered[0])]
    for hi, lo in ordered[1:]:
        if hi >= merged[-1][1]:
            merged[-1][1] = min(merged[-1][1], lo)
        else:
            merged.append([hi, lo])
    return [tuple(iv) for iv in merged]


def gaps_for_prefix(prefix, merged_intervals):
    """Returns the (range_start, range_end) pairs covering whatever part of `prefix`'s subtree
    isn't proven-covered by merged_intervals. Empty list means fully covered already. Allows a
    possible sliver of overlap at each boundary (the safe direction to be imprecise in) rather
    than needing to compute prefix's exact bottom leaf, which depends on piece availability."""
    depth = len(prefix)
    relevant = [(hi, lo) for hi, lo in merged_intervals
                if hi[:depth] >= prefix and lo[:depth] <= prefix]
    if not relevant:
        return [(prefix, prefix)]

    gaps = []
    cursor = prefix
    reached_floor = False
    for hi, lo in relevant:
        top_bound = hi if hi[:depth] == prefix else prefix
        if top_bound < cursor:
            gaps.append((cursor, top_bound))
        if lo[:depth] < prefix:
            reached_floor = True
            break
        cursor = lo
    if not reached_floor:
        gaps.append((cursor, prefix))
    return gaps


def create_schema(conn):
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ranges (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            prefix TEXT NOT NULL,
            range_start TEXT NOT NULL,
            range_end TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'unsearched',
            pid INTEGER,
            claimed_at TEXT,
            finished_at TEXT,
            solutions_found INTEGER,
            log_file TEXT
        );
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_ranges_status ON ranges(status);")
    conn.commit()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefixes", required=True,
                         help="File of depth-N prefixes from rangesolver --enumerate-depth")
    parser.add_argument("--db", default=DEFAULT_DB,
                         help=f"SQLite database to create/populate (default: {DEFAULT_DB})")
    parser.add_argument("--solutions", action="append", default=None,
                         help="Solution log file or glob pattern to scan for coverage; may be "
                              "repeated. Default: CppSolver/Solutions/soln_log.txt and "
                              "Claude/Solutions/*.txt")
    parser.add_argument("--force", action="store_true",
                         help="Wipe and reseed an existing non-empty database instead of refusing")
    args = parser.parse_args()

    with open(args.prefixes) as f:
        prefixes = [line.strip() for line in f if line.strip()]
    print(f"Loaded {len(prefixes)} prefixes from {args.prefixes}")

    solution_files = resolve_solution_files(args.solutions or DEFAULT_SOLUTION_PATTERNS)
    intervals = []
    for fn in solution_files:
        iv = file_interval(fn)
        if iv:
            intervals.append(iv)
            print(f"  {fn}: covers {iv[0]} down to {iv[1]}")
        else:
            print(f"  {fn}: no solutions found, skipping")

    merged = merge_intervals(intervals)
    print(f"Merged into {len(merged)} disjoint covered interval(s).")

    conn = sqlite3.connect(args.db)
    create_schema(conn)
    existing = conn.execute("SELECT COUNT(*) FROM ranges").fetchone()[0]
    if existing > 0:
        if not args.force:
            print(f"Error: {args.db} already has {existing} range(s). "
                  "Use --force to wipe and reseed.", file=sys.stderr)
            conn.close()
            sys.exit(1)
        conn.execute("DELETE FROM ranges;")
        conn.commit()

    inserted = 0
    fully_covered = 0
    for prefix in prefixes:
        gaps = gaps_for_prefix(prefix, merged)
        if not gaps:
            fully_covered += 1
            continue
        for start, end in gaps:
            conn.execute(
                "INSERT INTO ranges (prefix, range_start, range_end, status) "
                "VALUES (?, ?, ?, 'unsearched')",
                (prefix, start, end),
            )
            inserted += 1
    conn.commit()
    conn.close()

    print(f"{fully_covered} prefix(es) already fully covered, skipped.")
    print(f"{inserted} range(s) queued as unsearched "
          f"across {len(prefixes) - fully_covered} prefix(es).")
    print(f"Database ready: {args.db}")


if __name__ == "__main__":
    main()
