#!/usr/bin/env python3
"""Texts the current unique-solution count (and progress since the last run) via TextBelt.

Meant to be run periodically (e.g. via cron, ~4x/day) as a status check-in, independent of
whether the dashboard happens to be running. Reuses solution_db.py for an up-to-date count (fast:
only newly-appended log content gets read, not the whole corpus) and dashboard.py's
solution_counts.db history table for the "X new since last check" delta and rate/ETA - the same
shared state the dashboard itself reads, kept in sync regardless of which one last wrote to it.

Setup: get a key from textbelt.com (no business verification - either buy a small quota, a few
cents per text, or use the literal key "textbelt" for a shared free tier limited to 1 text/day,
fine for a first test but too limited for real ~4x/day use), and create a file called .env right
next to this script (never commit it - it's already in .gitignore) with two lines:
    TEXTBELT_API_KEY=your_key_or_textbelt
    TEXTBELT_TO_NUMBER=+15559876543
A .env file (loaded via python-dotenv) is used instead of just `export`ing these in your shell
because cron jobs start with a minimal environment that doesn't inherit your interactive shell's
exported variables at all - a file loaded by the script itself works the same way regardless of
what invokes it. Setting real environment variables still works too and takes precedence.
"""
import argparse
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

from dotenv import load_dotenv

import dashboard
import solution_db

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
load_dotenv(os.path.join(SCRIPT_DIR, ".env"))

REQUIRED_ENV_VARS = ["TEXTBELT_API_KEY", "TEXTBELT_TO_NUMBER"]


def most_recent_sample(history_db_path):
    """Returns (timestamp, count) for the last recorded sample - from dashboard.py's own
    periodic recompute, or a previous run of this script, whichever is more recent - or None."""
    conn = sqlite3.connect(history_db_path, timeout=10)
    row = conn.execute(
        "SELECT timestamp, count FROM solution_counts ORDER BY timestamp DESC LIMIT 1"
    ).fetchone()
    conn.close()
    return row


def compose_message(total, previous):
    lines = [f"Partridge puzzle: {total:,} unique solutions found."]
    if previous is not None:
        prev_ts, prev_count = previous
        delta = total - prev_count
        elapsed = time.time() - prev_ts
        lines.append(f"+{delta:,} since last check ({dashboard.format_duration(elapsed)} ago).")
        if elapsed > 0 and delta > 0:
            rate = delta / elapsed
            remaining = dashboard.TARGET_SOLUTIONS - total
            if remaining > 0:
                eta = dashboard.format_duration(remaining / rate)
                lines.append(f"~{eta} to unverified target of {dashboard.TARGET_SOLUTIONS:,}.")
    return " ".join(lines)


def send_sms(api_key, to_number, message):
    data = urllib.parse.urlencode(
        {"phone": to_number, "message": message, "key": api_key}
    ).encode()
    req = urllib.request.Request("https://textbelt.com/text", data=data)
    try:
        with urllib.request.urlopen(req) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"TextBelt returned HTTP {e.code}: {e.read().decode()}") from e
    if not body.get("success"):
        raise RuntimeError(f"TextBelt error: {body.get('error', 'unknown error')}")
    return body


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--solutions-db", default=solution_db.DEFAULT_SOLUTIONS_DB)
    parser.add_argument("--history-db", default=dashboard.DEFAULT_HISTORY_DB)
    parser.add_argument("--solutions", action="append", default=None,
                         help="Solution log file or glob pattern to ingest; may be repeated. "
                              "Default: same as solution_db.py's own defaults.")
    parser.add_argument("--only-if-changed", action="store_true",
                         help="Skip sending if the count hasn't changed since the last check "
                              "(avoids repeated identical texts during a slow stretch).")
    parser.add_argument("--dry-run", action="store_true",
                         help="Print the message that would be sent instead of sending it.")
    args = parser.parse_args()

    missing = [v for v in REQUIRED_ENV_VARS if not os.environ.get(v)]
    if missing and not args.dry_run:
        print(f"Error: missing environment variable(s): {', '.join(missing)}", file=sys.stderr)
        print(__doc__, file=sys.stderr)
        sys.exit(1)

    solution_db.ensure_schema(args.solutions_db)
    patterns = args.solutions or solution_db.DEFAULT_SOLUTION_PATTERNS
    total = solution_db.ingest_all(args.solutions_db, patterns)

    dashboard.ensure_history_schema(args.history_db)
    previous = most_recent_sample(args.history_db)

    if args.only_if_changed and previous is not None and previous[1] == total:
        print(f"No change since last check ({total:,} solutions) - not sending.")
        return

    message = compose_message(total, previous)
    print(message)

    if args.dry_run:
        print("(--dry-run: not actually sending)")
    else:
        result = send_sms(os.environ["TEXTBELT_API_KEY"], os.environ["TEXTBELT_TO_NUMBER"],
                           message)
        quota = result.get("quotaRemaining")
        print(f"Sent (TextBelt textId={result.get('textId')}"
              f"{f', quota remaining={quota}' if quota is not None else ''}).")

    dashboard.record_solution_count(args.history_db, time.time(), total)


if __name__ == "__main__":
    main()
