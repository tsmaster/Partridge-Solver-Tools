#!/usr/bin/env python3
"""Prettier live console viewer for a rangesolver solution log.

Seeks to the end of the given log file, renders its most recent solution as a colored grid (ANSI
truecolor half-block characters - two grid rows per terminal row, giving a roughly square,
reasonably compact picture without needing curses or any extra dependency), then polls for new
solutions and re-renders as they appear - like `tail -f`, but showing a picture instead of raw
text.

If the given file's rangesolver process is no longer running, waits a short grace period, then
automatically switches to whichever currently-running rangesolver process has the most recently
created log file - so the tool keeps following live work instead of sitting on a finished range.

Multiple independent copies of this script (one per xterm window) coordinate through a small
shared database (watchers.db) recording which log file each is currently following, so an
auto-switch prefers a job nobody else is already watching - avoiding several windows collapsing
onto the same job when their previous ones happen to finish around the same time. Press space at
any time to manually cycle forward to the next available job, same preference applied.
"""
import argparse
import os
import re
import select
import signal
import sqlite3
import subprocess
import sys
import termios
import time
import tty

import solution
import solution_db

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SOLUTIONS_DIR = os.path.join(SCRIPT_DIR, "../Claude/Solutions")
DEFAULT_WATCHERS_DB = os.path.join(SCRIPT_DIR, "watchers.db")

POLL_INTERVAL = 1.0
GRACE_PERIOD = 5.0  # seconds to wait after a process disappears before switching files

LOG_ARG_RE = re.compile(r"--log=Solutions/(\S+)")

# Same palette as solution.py's save_to_png(), so every view of a solution agrees visually.
COLORS = {
    1: (128, 128, 128), 2: (80, 64, 0), 3: (92, 0, 92), 4: (0, 0, 192), 5: (0, 192, 0),
    6: (192, 192, 0), 7: (192, 80, 0), 8: (192, 0, 0), 9: (192, 192, 192),
}
RESET = "\033[0m"
UPPER_HALF_BLOCK = "▀"


def resolve_path(filename):
    if os.sep in filename or filename in (".", ".."):
        return filename
    return os.path.join(SOLUTIONS_DIR, filename)


def list_running_rangesolvers():
    """Returns {log_file_basename: (pid, etimes)} for every currently running rangesolver
    process, parsed from its own --log= argument. etimes is the process's own elapsed running
    time in seconds (from `ps`'s etimes field) - used to tell which one started most recently.
    Deliberately not based on the log file's own mtime/ctime: those update on every solution
    appended, not just at creation, so they reflect "most recently written to," not "most
    recently started" (confirmed the hard way - an early version of this used file ctime and
    picked a process that had actually been running for hours over one active for under 2 minutes)."""
    result = {}
    try:
        out = subprocess.check_output(["ps", "-eo", "pid,etimes,args"], text=True,
                                       env={**os.environ, "COLUMNS": "2000"})
    except (subprocess.CalledProcessError, OSError):
        return result
    for line in out.splitlines()[1:]:
        line = line.strip()
        if "rangesolver" not in line:
            continue
        parts = line.split(None, 2)
        if len(parts) < 3:
            continue
        pid_str, etimes_str, args = parts
        m = LOG_ARG_RE.search(args)
        if not m:
            continue
        try:
            result[m.group(1)] = (int(pid_str), int(etimes_str))
        except ValueError:
            continue
    return result


def pid_alive(pid):
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def ensure_watchers_schema(db_path):
    conn = sqlite3.connect(db_path)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS watchers (
            watcher_pid INTEGER PRIMARY KEY,
            log_file TEXT NOT NULL,
            updated_at REAL NOT NULL
        );
    """)
    conn.commit()
    conn.close()


def claim_watcher(db_path, watcher_pid, log_file):
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute(
        "INSERT INTO watchers (watcher_pid, log_file, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(watcher_pid) DO UPDATE SET "
        "log_file=excluded.log_file, updated_at=excluded.updated_at",
        (watcher_pid, log_file, time.time()),
    )
    conn.commit()
    conn.close()


def release_watcher(db_path, watcher_pid):
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute("DELETE FROM watchers WHERE watcher_pid=?", (watcher_pid,))
    conn.commit()
    conn.close()


def other_claimed_files(db_path, own_pid):
    """Returns the set of log-file basenames currently claimed by other watch_solution.py
    processes that are actually still alive, opportunistically cleaning up any stale claims left
    behind by one that wasn't (e.g. killed without a chance to release its own claim)."""
    conn = sqlite3.connect(db_path, timeout=5)
    rows = conn.execute(
        "SELECT watcher_pid, log_file FROM watchers WHERE watcher_pid != ?", (own_pid,)
    ).fetchall()
    conn.close()
    claimed = set()
    stale_pids = []
    for watcher_pid, log_file in rows:
        if pid_alive(watcher_pid):
            claimed.add(log_file)
        else:
            stale_pids.append(watcher_pid)
    if stale_pids:
        conn = sqlite3.connect(db_path, timeout=5)
        conn.executemany("DELETE FROM watchers WHERE watcher_pid=?",
                          [(p,) for p in stale_pids])
        conn.commit()
        conn.close()
    return claimed


def most_recently_started(pool):
    """pool: {log_basename: (pid, etimes)}. Returns (path, basename, pid) for whichever has been
    running the shortest time - i.e. started most recently - or None if pool is empty."""
    if not pool:
        return None
    basename, (pid, _etimes) = min(pool.items(), key=lambda kv: kv[1][1])
    return os.path.join(SOLUTIONS_DIR, basename), basename, pid


def pick_unclaimed_or_most_recent(running, watchers_db, own_pid):
    """Prefers whichever currently-running job no other live watcher is already following (most
    recently started among those); falls back to the overall most-recently-started job if every
    running one is already claimed (unavoidable once there are more watchers than active jobs)."""
    claimed = other_claimed_files(watchers_db, own_pid)
    unclaimed_pool = {b: v for b, v in running.items() if b not in claimed}
    return most_recently_started(unclaimed_pool or running)


def cycle_forward(running, watchers_db, own_pid, current_basename):
    """Returns the next candidate after `current_basename` in a stable sorted order, preferring
    ones no other live watcher is already following, and wrapping around at the end. None if
    nothing is currently running at all."""
    if not running:
        return None
    claimed = other_claimed_files(watchers_db, own_pid)
    unclaimed = sorted(b for b in running if b not in claimed)
    pool = unclaimed or sorted(running.keys())
    next_index = (pool.index(current_basename) + 1) % len(pool) if current_basename in pool else 0
    basename = pool[next_index]
    pid, _etimes = running[basename]
    return os.path.join(SOLUTIONS_DIR, basename), basename, pid


def wait_for_key(timeout, stdin_is_tty):
    """Waits up to `timeout` seconds for a single keypress on stdin (assumed already in cbreak
    mode), returning it, or None if nothing was pressed. Falls back to a plain sleep when stdin
    isn't a real terminal (e.g. redirected), since reading from it wouldn't mean what it should."""
    if not stdin_is_tty:
        time.sleep(timeout)
        return None
    ready, _, _ = select.select([sys.stdin], [], [], timeout)
    if ready:
        return sys.stdin.read(1)
    return None


# How much darker a tile's outer ring of cells is than its own interior fill - purely a matter
# of taste, easy to tune: closer to 1.0 is a subtler seam, closer to 0.0 is a bolder outline.
BORDER_DARKEN_FACTOR = 0.6


def darken(color):
    return tuple(max(0, int(c * BORDER_DARKEN_FACTOR)) for c in color)


def build_color_grid(soln):
    """Returns a 45x45 grid of RGB colors: each tile's outer ring of cells is darkened relative
    to its own interior fill, so two adjacent tiles of the same size - and thus the same base
    color - still show a visible seam between them instead of blending into one blob."""
    grid = [[None] * 45 for _ in range(45)]
    for loc in soln.piece_locations:
        fill = COLORS[loc.sz]
        border = darken(fill)
        for dx in range(loc.sz):
            for dy in range(loc.sz):
                on_edge = dx in (0, loc.sz - 1) or dy in (0, loc.sz - 1)
                grid[loc.y + dy][loc.x + dx] = border if on_edge else fill
    return grid


# Tiles smaller than this have no sensible interior to put a readable digit in (a 2x2 or 3x3
# tile is entirely its own "border" ring already - see build_color_grid's on_edge check).
DIGIT_MIN_SIZE = 4


def brightness(color):
    r, g, b = color
    return 0.299 * r + 0.587 * g + 0.114 * b


def text_color_for(background):
    return (0, 0, 0) if brightness(background) > 140 else (255, 255, 255)


def build_digit_positions(soln):
    """Returns {(row_pair_start_y, x): (digit_char, text_color, tile_fill_color)} for each tile
    big enough to sensibly show its own size as a single character near its center. The text
    color is chosen for contrast against that tile's own fill color, not a fixed black or white,
    so it stays readable across the whole (fairly varied) palette."""
    positions = {}
    for loc in soln.piece_locations:
        if loc.sz < DIGIT_MIN_SIZE:
            continue
        center_x = loc.x + loc.sz // 2
        center_y = loc.y + loc.sz // 2
        y_pair = center_y - (center_y % 2)
        fill = COLORS[loc.sz]
        positions[(y_pair, center_x)] = (str(loc.sz), text_color_for(fill), fill)
    return positions


def render_grid(buf):
    soln = solution.make_solution_from_lines(buf)
    grid = build_color_grid(soln)
    digit_positions = build_digit_positions(soln)
    lines = []
    for y in range(0, 45, 2):
        chars = []
        for x in range(45):
            digit_info = digit_positions.get((y, x))
            if digit_info is not None:
                digit, fg, bg = digit_info
                chars.append(f"\033[1m\033[38;2;{fg[0]};{fg[1]};{fg[2]}m"
                             f"\033[48;2;{bg[0]};{bg[1]};{bg[2]}m{digit}\033[22m")
                continue
            top = grid[y][x]
            if y + 1 < 45:
                bottom = grid[y + 1][x]
                chars.append(f"\033[38;2;{top[0]};{top[1]};{top[2]}m"
                             f"\033[48;2;{bottom[0]};{bottom[1]};{bottom[2]}m{UPPER_HALF_BLOCK}")
            else:
                chars.append(f"\033[38;2;{top[0]};{top[1]};{top[2]}m{UPPER_HALF_BLOCK}")
        chars.append(RESET)
        lines.append("".join(chars))
    return "\n".join(lines), soln.get_hash()


FOOTER = "Space to switch, Ctrl-C to exit"


def draw(is_tty, basename, pid, buf):
    grid_text, hash_str = render_grid(buf)
    status = f"pid {pid}" if pid is not None else "process finished"
    text = f"Watching: {basename} ({status})\nHash: {hash_str}\n\n{grid_text}\n\n{FOOTER}"
    if is_tty:
        print("\033[H\033[J", end="")
    print(text)
    sys.stdout.flush()


def draw_message(is_tty, message):
    if is_tty:
        print("\033[H\033[J", end="")
    print(f"{message}\n\n{FOOTER}")
    sys.stdout.flush()


def _raise_keyboard_interrupt(signum, frame):
    raise KeyboardInterrupt


def watch(initial_path, refresh, watchers_db):
    is_tty = sys.stdout.isatty()
    stdin_is_tty = sys.stdin.isatty()
    own_pid = os.getpid()

    # Python only converts SIGINT (Ctrl-C) to KeyboardInterrupt by default; SIGTERM (e.g. from
    # `kill` or `timeout`) and SIGHUP (a closed terminal window) would otherwise terminate the
    # process immediately, skipping the finally block below and leaving a stale claim in
    # watchers.db until another watcher's staleness check eventually notices the dead pid.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _raise_keyboard_interrupt)

    if is_tty:
        print("\033[?25l", end="")  # hide cursor
    old_termios = None
    if stdin_is_tty:
        old_termios = termios.tcgetattr(sys.stdin)
        tty.setcbreak(sys.stdin.fileno())  # deliver each keypress immediately, no Enter needed

    ensure_watchers_schema(watchers_db)

    path = initial_path
    basename = os.path.basename(path) if path else None
    bytes_ingested = 0
    last_buf = None
    grace_deadline = None
    if basename:
        claim_watcher(watchers_db, own_pid, basename)

    def switch_to(choice):
        nonlocal path, basename, bytes_ingested, last_buf, grace_deadline
        path, basename, _pid = choice
        bytes_ingested = 0
        last_buf = None
        grace_deadline = None
        claim_watcher(watchers_db, own_pid, basename)

    try:
        while True:
            running = list_running_rangesolvers()

            if path is None:
                choice = pick_unclaimed_or_most_recent(running, watchers_db, own_pid)
                if choice is None:
                    draw_message(is_tty, "Waiting for an active rangesolver process...")
                    wait_for_key(refresh, stdin_is_tty)
                    continue
                switch_to(choice)

            pid, _etimes = running.get(basename, (None, None))

            new_buffers, bytes_ingested = solution_db.scan_new_blocks(path, bytes_ingested)
            if new_buffers:
                last_buf = new_buffers[-1]

            if last_buf is not None:
                draw(is_tty, basename, pid, last_buf)
            else:
                status = f"pid {pid}" if pid is not None else "process finished"
                draw_message(is_tty, f"Watching: {basename} ({status})\n"
                                      "Waiting for the first solution in this range...")

            if pid is None:
                if grace_deadline is None:
                    grace_deadline = time.time() + GRACE_PERIOD
                elif time.time() >= grace_deadline:
                    path = None  # triggers auto-pick on the next iteration
            else:
                grace_deadline = None

            key = wait_for_key(refresh, stdin_is_tty)
            if key == " ":
                choice = cycle_forward(list_running_rangesolvers(), watchers_db, own_pid,
                                        basename)
                if choice is not None:
                    switch_to(choice)
    except KeyboardInterrupt:
        pass
    finally:
        release_watcher(watchers_db, own_pid)
        if stdin_is_tty and old_termios is not None:
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_termios)
        if is_tty:
            print("\033[?25h")  # restore cursor


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logfile", nargs="?", default=None,
                         help="Solution log filename to watch. A bare filename (no '/') resolves "
                              "against Claude/Solutions/, matching ranges.db's log_file column. "
                              "If omitted, immediately auto-picks whichever rangesolver process's "
                              "log file was most recently created.")
    parser.add_argument("--refresh", type=float, default=POLL_INTERVAL,
                         help=f"Seconds between polls (default: {POLL_INTERVAL})")
    parser.add_argument("--watchers-db", default=DEFAULT_WATCHERS_DB,
                         help="SQLite database this instance and others coordinate through, to "
                              f"avoid picking the same job (default: {DEFAULT_WATCHERS_DB})")
    args = parser.parse_args()

    initial_path = resolve_path(args.logfile) if args.logfile else None
    watch(initial_path, args.refresh, args.watchers_db)


if __name__ == "__main__":
    main()
