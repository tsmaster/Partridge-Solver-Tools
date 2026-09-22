#!/usr/bin/env python3
"""Texts the current unique-solution count (and progress since the last run) via Twilio.

Meant to be run periodically (e.g. via cron, ~4x/day) as a status check-in, independent of
whether the dashboard happens to be running. Reuses solution_db.py for an up-to-date count (fast:
only newly-appended log content gets read, not the whole corpus) and dashboard.py's
solution_counts.db history table for the "X new since last check" delta and rate/ETA - the same
shared state the dashboard itself reads, kept in sync regardless of which one last wrote to it.

Setup: sign up for Twilio (a small free trial credit is enough to test this), get a phone number
from it, and set four environment variables before running - never commit these or paste them
into a chat:
    TWILIO_ACCOUNT_SID   - from the Twilio console
    TWILIO_AUTH_TOKEN    - from the Twilio console
    TWILIO_FROM_NUMBER   - the Twilio number you were assigned, e.g. +15551234567
    TWILIO_TO_NUMBER     - the phone number to text, e.g. your own, +15559876543
"""
import argparse
import base64
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import dashboard
import solution_db

REQUIRED_ENV_VARS = ["TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
                     "TWILIO_FROM_NUMBER", "TWILIO_TO_NUMBER"]


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


def send_sms(account_sid, auth_token, from_number, to_number, body):
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    data = urllib.parse.urlencode({"To": to_number, "From": from_number, "Body": body}).encode()
    credentials = base64.b64encode(f"{account_sid}:{auth_token}".encode()).decode()
    req = urllib.request.Request(url, data=data)
    req.add_header("Authorization", f"Basic {credentials}")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"Twilio returned HTTP {e.code}: {e.read().decode()}") from e


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
        status, _body = send_sms(
            os.environ["TWILIO_ACCOUNT_SID"], os.environ["TWILIO_AUTH_TOKEN"],
            os.environ["TWILIO_FROM_NUMBER"], os.environ["TWILIO_TO_NUMBER"], message,
        )
        print(f"Sent (Twilio HTTP {status}).")

    dashboard.record_solution_count(args.history_db, time.time(), total)


if __name__ == "__main__":
    main()
