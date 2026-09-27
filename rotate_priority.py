#!/usr/bin/env python3
"""Marks ranges.db rows as higher-priority for worker.py to claim, based on rotational symmetry:
every found solution's 90/180/270-degree rotation is a genuinely different, guaranteed-valid
solution (no solution in this puzzle is self-symmetric) living somewhere else in the search tree -
usually in a range that hasn't been searched yet. See Claude/TODO.txt's "Rotational-symmetry range
prioritization" entry for the full design rationale and the tradeoff (this accelerates the
aggregate solutions-found count, not the exhaustive ranges/hour completion - every range still
needs its own full search regardless).

Works entirely from solutions.db's already-deduplicated 45-character hashes (tools/solution_db.py) -
no need to re-parse the raw ASCII solution logs. For each hash:
  1. Reconstruct all 45 (x, y, size) piece placements by replaying the same raster-scan process
     the solvers use (each successive size from the hash goes at the current first free cell) -
     using the same per-row-bitmask technique as rangesolver.cpp's Grid, since this runs once per
     known solution and there are well over a million of them.
  2. Rotate that piece list 90/180/270 degrees (standard axis-aligned-square rotation formulas)
     and re-derive each rotated hash via the same raster-sort get_hash() logic solution.py uses.
  3. Find which still-'unsearched' ranges.db row (the longest-prefix match) would eventually
     discover each rotated hash, and bump its priority.

worker.py's claim_range() prefers priority DESC, range_start DESC among unsearched candidates
(within the per-lineage cap), so a flagged range gets claimed before ordinary backlog ones.
"""
import argparse
import os
import sqlite3
import sys

import solution_db
from piece_geometry import reconstruct_pieces, rotate_pieces, pieces_to_hash

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_RANGES_DB = os.path.join(SCRIPT_DIR, "ranges.db")
STATE_FILE = os.path.join(SCRIPT_DIR, ".rotate_priority_state")


def read_last_processed_count():
    """How many known solutions were processed as of the last full (non-dry-run, unlimited) run -
    used by splitter.py to decide whether re-running is actually worth it (only if the known-
    solution count has grown since), rather than re-scanning on a fixed schedule regardless."""
    try:
        with open(STATE_FILE) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return 0


def write_last_processed_count(count):
    with open(STATE_FILE, "w") as f:
        f.write(str(count))


def rotated_hashes(hash_str):
    pieces = reconstruct_pieces(hash_str)
    return [pieces_to_hash(rotate_pieces(pieces, t)) for t in (1, 2, 3)]


def build_prefix_index(conn):
    """Returns (prefix_set, max_len) for every currently 'unsearched' range - a rotated hash's
    matching range (if any) is found by checking decreasing-length prefixes against this set,
    which is safe because ranges are disjoint: at most one 'unsearched' prefix can ever be a
    prefix of any given 45-digit hash."""
    rows = conn.execute("SELECT prefix FROM ranges WHERE status='unsearched'").fetchall()
    prefixes = {row[0] for row in rows}
    max_len = max((len(p) for p in prefixes), default=0)
    return prefixes, max_len


def find_matching_prefix(full_hash, prefixes, max_len):
    for length in range(max_len, 4, -1):
        candidate = full_hash[:length]
        if candidate in prefixes:
            return candidate
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--ranges-db", default=DEFAULT_RANGES_DB)
    parser.add_argument("--solutions-db", default=solution_db.DEFAULT_SOLUTIONS_DB)
    parser.add_argument("--limit", type=int, default=None,
                         help="Only process this many known solutions (for a quick test run).")
    parser.add_argument("--dry-run", action="store_true",
                         help="Report what would be flagged without writing to ranges.db.")
    args = parser.parse_args()

    ranges_conn = sqlite3.connect(args.ranges_db, timeout=30, isolation_level=None)
    sol_conn = sqlite3.connect(args.solutions_db)

    prefixes, max_len = build_prefix_index(ranges_conn)
    print(f"[rotate_priority] {len(prefixes)} unsearched ranges indexed, max depth {max_len}.")

    query = "SELECT hash FROM solutions"
    if args.limit:
        query += f" LIMIT {int(args.limit)}"
    hashes = [row[0] for row in sol_conn.execute(query).fetchall()]
    print(f"[rotate_priority] processing {len(hashes)} known solutions "
          f"({3 * len(hashes)} rotations)...")

    matched = {}  # prefix -> number of rotated hashes that landed on it
    for i, h in enumerate(hashes):
        for rh in rotated_hashes(h):
            m = find_matching_prefix(rh, prefixes, max_len)
            if m is not None:
                matched[m] = matched.get(m, 0) + 1
        if (i + 1) % 100000 == 0:
            print(f"[rotate_priority] ...{i + 1}/{len(hashes)} processed, "
                  f"{len(matched)} distinct ranges matched so far", file=sys.stderr)

    print(f"[rotate_priority] done: {len(matched)} distinct unsearched range(s) matched by at "
          f"least one rotation.")

    if args.dry_run:
        print("[rotate_priority] --dry-run: not writing to ranges.db.")
        for prefix, count in sorted(matched.items(), key=lambda kv: -kv[1])[:20]:
            print(f"  {prefix}: {count} rotation(s) would land here")
        return

    ranges_conn.execute("BEGIN IMMEDIATE;")
    for prefix in matched:
        ranges_conn.execute(
            "UPDATE ranges SET priority=1 WHERE prefix=? AND status='unsearched'", (prefix,)
        )
    ranges_conn.execute("COMMIT;")
    print(f"[rotate_priority] flagged {len(matched)} range(s) as priority=1.")

    if not args.limit:
        write_last_processed_count(len(hashes))


if __name__ == "__main__":
    main()
