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
"""
import argparse
import os
import re
import subprocess
import sys
import time

import solution
import solution_db

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SOLUTIONS_DIR = os.path.join(SCRIPT_DIR, "../Claude/Solutions")

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


def pick_most_recently_started(running):
    """Given {log_basename: (pid, etimes)}, returns (path, basename, pid) for whichever process
    has been running the shortest time - i.e. started most recently - or None if nothing's
    currently running."""
    if not running:
        return None
    basename, (pid, _etimes) = min(running.items(), key=lambda kv: kv[1][1])
    return os.path.join(SOLUTIONS_DIR, basename), basename, pid


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


def render_grid(buf):
    soln = solution.make_solution_from_lines(buf)
    grid = build_color_grid(soln)
    lines = []
    for y in range(0, 45, 2):
        chars = []
        for x in range(45):
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


def draw(is_tty, basename, pid, buf):
    grid_text, hash_str = render_grid(buf)
    status = f"pid {pid}" if pid is not None else "process finished"
    text = f"Watching: {basename} ({status})\nHash: {hash_str}\n\n{grid_text}\n\nCtrl-C to exit"
    if is_tty:
        print("\033[H\033[J", end="")
    print(text)
    sys.stdout.flush()


def draw_message(is_tty, message):
    if is_tty:
        print("\033[H\033[J", end="")
    print(message)
    sys.stdout.flush()


def watch(initial_path, refresh):
    is_tty = sys.stdout.isatty()
    if is_tty:
        print("\033[?25l", end="")

    path = initial_path
    basename = os.path.basename(path) if path else None
    bytes_ingested = 0
    last_buf = None
    grace_deadline = None

    try:
        while True:
            running = list_running_rangesolvers()

            if path is None:
                choice = pick_most_recently_started(running)
                if choice is None:
                    draw_message(is_tty, "Waiting for an active rangesolver process...")
                    time.sleep(refresh)
                    continue
                path, basename, _pid = choice
                bytes_ingested = 0
                last_buf = None
                grace_deadline = None

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

            time.sleep(refresh)
    except KeyboardInterrupt:
        pass
    finally:
        if is_tty:
            print("\033[?25h")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logfile", nargs="?", default=None,
                         help="Solution log filename to watch. A bare filename (no '/') resolves "
                              "against Claude/Solutions/, matching ranges.db's log_file column. "
                              "If omitted, immediately auto-picks whichever rangesolver process's "
                              "log file was most recently created.")
    parser.add_argument("--refresh", type=float, default=POLL_INTERVAL,
                         help=f"Seconds between polls (default: {POLL_INTERVAL})")
    args = parser.parse_args()

    initial_path = resolve_path(args.logfile) if args.logfile else None
    watch(initial_path, args.refresh)


if __name__ == "__main__":
    main()
