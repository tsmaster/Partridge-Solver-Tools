#!/usr/bin/env python3
"""Classifies every found solution into Matt Parker's "L"/"I"/"other" categories, based on the
definitions worked out in conversation and fully resolved against the completed search (see
Claude/TODO.txt and Claude/RESULTS.txt):

  - "L": the nine 9x9 tiles form a forced two-band border along two adjacent edges (one full
    45-wide/tall edge, 5 tiles; the perpendicular remaining 36-length edge, 4 tiles), with the
    36x36 interior holding exactly an n=8 sub-solution. Fully determined: exactly 4 x 18,656 =
    74,624 - confirmed exactly against the real, complete corpus.
  - "I": the nine 9x9 tiles include a straight line of five spanning the board's full width or
    height - either flush against an edge ("I_edge") or running through the interior
    ("I_interior", the ambiguous case from the source video, confirmed to count).
  - "other": no such line exists at all.

The actual classification logic lives in piece_geometry.py (shared with rotate_priority.py and
make_category_book.py, kept separate from solution_db.py to avoid a circular import even though
solution_db.py stores each solution's category at ingest time using the same functions).

This script's job is specifically to backfill the `category` column in solutions.db for any row
that doesn't have one yet (from before this column existed, or from any gap in normal ingestion),
then report the totals read back from the database - fast after the first run, since a solution's
category never needs recomputing once stored."""
import argparse
import sqlite3
import sys

import piece_geometry
import solution_db

BACKFILL_BATCH = 5000


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--solutions-db", default=solution_db.DEFAULT_SOLUTIONS_DB)
    parser.add_argument("--limit", type=int, default=None,
                         help="Only backfill up to this many missing rows (for a quick test run).")
    args = parser.parse_args()

    solution_db.ensure_schema(args.solutions_db)
    conn = sqlite3.connect(args.solutions_db)

    query = "SELECT hash FROM solutions WHERE category IS NULL"
    if args.limit:
        query += f" LIMIT {int(args.limit)}"
    missing = [row[0] for row in conn.execute(query).fetchall()]

    if missing:
        print(f"[classify] backfilling category for {len(missing)} solution(s)...")
        for i, h in enumerate(missing):
            category = piece_geometry.classify_detailed(h)
            conn.execute("UPDATE solutions SET category=? WHERE hash=?", (category, h))
            if (i + 1) % BACKFILL_BATCH == 0:
                conn.commit()
                print(f"[classify] ...{i + 1}/{len(missing)}", file=sys.stderr)
        conn.commit()
        print(f"[classify] backfill complete.")
    else:
        print("[classify] every solution already has a category - nothing to backfill.")

    counts = dict(conn.execute(
        "SELECT category, COUNT(*) FROM solutions GROUP BY category"
    ).fetchall())
    total = sum(counts.values())
    print(f"\n[classify] {total:,} total solutions")
    for k in ("L", "I_edge", "I_interior", "other"):
        c = counts.get(k, 0)
        pct = f"({100 * c / total:.3f}%)" if total else ""
        print(f"  {k}: {c:,} {pct}")
    still_null = counts.get(None, 0)
    if still_null:
        print(f"  (still uncategorized: {still_null:,})")


if __name__ == "__main__":
    main()
