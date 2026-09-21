#!/usr/bin/env python3
"""Ingests solutions from the ASCII soln_*.txt log files into a SQLite database (solutions.db),
incrementally: each file's already-ingested byte offset is tracked, so a re-scan only reads
newly-appended content instead of re-parsing the whole file every time.

rangesolver itself is untouched - it keeps writing plain text logs exactly as it always has. This
is a separate step that scrapes those logs into a database, so that things like counting unique
solutions become a fast indexed query instead of an ever-more-expensive full-corpus text rescan as
more per-range log files accumulate.

Can be run standalone (one-shot ingest + report), or imported and driven periodically by another
tool - dashboard.py uses it this way, kicking off a scan on the same timer it already used for its
old from-scratch rescan.
"""
import argparse
import glob
import os
import sqlite3
import time

import solution
import splitlog

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_SOLUTIONS_DB = os.path.join(SCRIPT_DIR, "solutions.db")
DEFAULT_SOLUTION_PATTERNS = [
    os.path.join(SCRIPT_DIR, "../CppSolver/Solutions/soln_log.txt"),
    os.path.join(SCRIPT_DIR, "../Claude/Solutions/*.txt"),
]


def ensure_schema(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL;")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS solutions (
            hash TEXT PRIMARY KEY,
            source_file TEXT NOT NULL,
            ingested_at REAL NOT NULL
        );
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS ingested_files (
            filename TEXT PRIMARY KEY,
            bytes_ingested INTEGER NOT NULL,
            last_scanned REAL NOT NULL
        );
    """)
    conn.commit()
    conn.close()


def resolve_solution_files(patterns):
    files = []
    for pattern in patterns:
        matched = glob.glob(pattern)
        files.extend(matched if matched else [pattern])
    return files


def scan_new_blocks(filename, prior_bytes):
    """Reads whatever has been appended to `filename` since `prior_bytes`, returning
    (hashes, new_bytes_ingested). Only counts complete 45-line blocks - a block still mid-write,
    or even just a trailing partial line, is left alone for the next scan rather than guessed at.
    Safe by construction: rangesolver only fflushes a block once its full 45 lines plus the "---"
    delimiter are written, so a reader never observes a genuinely half-written block."""
    try:
        size = os.path.getsize(filename)
    except OSError:
        return [], prior_bytes
    if size < prior_bytes:
        prior_bytes = 0  # file was replaced/rotated/truncated - rescan it from scratch
    if size <= prior_bytes:
        return [], prior_bytes

    with open(filename, "rb") as f:
        f.seek(prior_bytes)
        chunk = f.read()

    lines = chunk.decode("ascii", errors="replace").splitlines(keepends=True)
    if lines and not lines[-1].endswith("\n"):
        lines = lines[:-1]  # a line still being written - not safe to parse yet

    hashes = []
    consumed_lines = 0
    i, n = 0, len(lines)
    while i < n:
        if splitlog.buffer_starts_at_line(i, lines):
            buf = lines[i:i + 45]
            hashes.append(solution.make_solution_from_lines(buf).get_hash())
            i += 45
            consumed_lines = i
        else:
            i += 1

    new_bytes = sum(len(l) for l in lines[:consumed_lines])
    return hashes, prior_bytes + new_bytes


def ingest_all(db_path, patterns):
    """Scans every file matching `patterns` for newly-appended solutions since the last call,
    inserts any new hashes into the database, and returns the total unique count afterward."""
    conn = sqlite3.connect(db_path, timeout=10)
    now = time.time()
    for filename in resolve_solution_files(patterns):
        abspath = os.path.abspath(filename)
        row = conn.execute(
            "SELECT bytes_ingested FROM ingested_files WHERE filename=?", (abspath,)
        ).fetchone()
        prior_bytes = row[0] if row else 0
        hashes, new_bytes = scan_new_blocks(filename, prior_bytes)
        for h in hashes:
            conn.execute(
                "INSERT OR IGNORE INTO solutions (hash, source_file, ingested_at) "
                "VALUES (?, ?, ?)",
                (h, abspath, now),
            )
        conn.execute(
            "INSERT INTO ingested_files (filename, bytes_ingested, last_scanned) "
            "VALUES (?, ?, ?) "
            "ON CONFLICT(filename) DO UPDATE SET "
            "bytes_ingested=excluded.bytes_ingested, last_scanned=excluded.last_scanned",
            (abspath, new_bytes, now),
        )
    conn.commit()
    total = conn.execute("SELECT COUNT(*) FROM solutions").fetchone()[0]
    conn.close()
    return total


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default=DEFAULT_SOLUTIONS_DB,
                         help=f"SQLite database to ingest into (default: {DEFAULT_SOLUTIONS_DB})")
    parser.add_argument("--solutions", action="append", default=None,
                         help="Solution log file or glob pattern to ingest; may be repeated. "
                              "Default: CppSolver/Solutions/soln_log.txt and "
                              "Claude/Solutions/*.txt")
    args = parser.parse_args()
    ensure_schema(args.db)
    total = ingest_all(args.db, args.solutions or DEFAULT_SOLUTION_PATTERNS)
    print(f"Total unique solutions in {args.db}: {total}")


if __name__ == "__main__":
    main()
