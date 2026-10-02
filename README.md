# Partridge Solver Tools

Python tooling that coordinates a decentralized, multi-worker, multi-machine
search of the "Partridge Puzzle" (tiling a 45x45 square exactly with one
1x1, two 2x2, ... up to nine 9x9 squares). This repo is the coordination
layer; the actual C++ search engine it drives lives in the companion
repository, [Partridge-Puzzle-Solver (w/ Claude Code)](https://github.com/tsmaster/Partridge-Puzzle-Solver--w-Claude-Code-).

The search this tooling ran is complete: **1,730,280 unique solutions**,
an exact match to Matt Parker's cited figure. See that repo's
[`RESULTS.txt`](https://github.com/tsmaster/Partridge-Puzzle-Solver--w-Claude-Code-/blob/main/RESULTS.txt)
for the full findings, and [`TODO.txt`](https://github.com/tsmaster/Partridge-Puzzle-Solver--w-Claude-Code-/blob/main/TODO.txt)
for the complete development log both repos share.

## Design

No central manager process - every tool coordinates purely through a
shared SQLite database (`ranges.db`), using `BEGIN IMMEDIATE` transactions
so concurrent workers never claim the same unit of work. Workers can be
started and stopped freely, on this machine or others (over SSH), without
any of them needing to know about each other directly.

## The pieces

**Core distributed search:**
- `bootstrap_ranges.py` - seeds `ranges.db` with the initial partition of
  work (one row per depth-5 prefix) from an `--enumerate-depth` run.
- `worker.py` - claims a range, runs `rangesolver` against it (locally or
  over SSH on a remote machine via `--remote-host`), records the result,
  and repeats until nothing remains. Resilient to being killed at any
  point, deliberately or not - an interrupted range is automatically
  reclaimed by another worker.
- `splitter.py` - watches for any range that's been running too long and
  automatically subdivides it, so one disproportionately large unit of
  work can't monopolize a worker indefinitely.
- `dashboard.py` - a live console view of every worker's progress, overall
  completion status, and two independent ETAs (aggregate solution count
  vs. actual exhaustive completion - they measure different things and can
  diverge significantly).
- `watch_solution.py` - a prettier live terminal view of one worker's
  solution log, with ANSI color rendering of the grid.
- `sms_notify.py` - periodic SMS status check-ins via TextBelt.

**Solution storage and analysis:**
- `solution.py` - parses/represents a solution (as a list of tile
  placements) and renders it to a PNG, with a size label on every tile.
- `solution_db.py` - incrementally scrapes newly-found solutions out of
  the raw text logs into a SQLite database, deduplicating by hash.
- `piece_geometry.py` - reconstructs a solution's tile positions from its
  hash alone (no need to re-parse the original text), and classifies it
  into the "L"/"I"/"other" structural categories from the video this
  puzzle comes from.
- `classify_li_other.py` - backfills/reports that classification against
  the whole solution database.
- `rotate_priority.py` - an optional optimization: flags ranges as
  higher-priority to claim when a known solution's rotation predicts one
  lives there.
- `make_category_book.py` - generates a PDF of evenly-sampled solutions
  across all four structural categories.
- `splitlog.py`, `bootstrap_ranges.py` - shared log-parsing helpers.

## Typical workflow

```
python3 bootstrap_ranges.py          # one-time: seed ranges.db
python3 worker.py                    # run one per CPU core, each in its own terminal
python3 dashboard.py                 # watch overall progress
python3 splitter.py                  # keep running alongside the workers
```

Secrets (API keys, phone numbers for `sms_notify.py`) are loaded from a
local `.env` file via `python-dotenv` - never committed; see
`sms_notify.py`'s own docstring for the two lines it expects.

## License

MIT - see [`LICENSE`](LICENSE).
