#!/usr/bin/env python3
"""Makes a PDF book of solutions sampled from the entire corpus, aiming for a good spread across
the four structural categories worked out in the L/I/other investigation (see Claude/TODO.txt and
Claude/RESULTS.txt): L, I with the five-tile line flush against an edge, I with the line running
through the interior, and other. Adapted from make_45_book.py, but drawing from solutions.db (the
full deduplicated corpus, all 1,730,280 solutions once the search is complete) instead of a single
raw log file, and classifying/labeling each page instead of just sampling blindly.

The four categories are wildly different sizes (I_interior is only ~0.2% of all solutions), so the
requested --count is split evenly across all four rather than proportionally to their natural
frequency - the point of this book is to show a representative example of each kind, not to
reproduce the overall distribution in miniature. Within a category, solutions are sampled evenly
spaced through that category's own list (same "every step-th one" idea make_45_book.py used) rather
than clustered together or purely random.
"""
import argparse
import os
import sqlite3
import sys

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.lib.units import inch

import piece_geometry
import solution
import solution_db

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_COUNT = 90
CATEGORIES = ["L", "I_edge", "I_interior", "other"]
CATEGORY_LABELS = {
    "L": "L (n=8 core + forced two-edge 9x9 border)",
    "I_edge": "I - edge-flush (line of five 9x9s along a border)",
    "I_interior": "I - interior (line of five 9x9s through the middle)",
    "other": "Other (no L or I structure among the 9x9s)",
}
IMAGE_DIR = os.path.join(SCRIPT_DIR, "CategoryBookImages")

IMAGE_BOTTOM = 2.25 * inch
IMAGE_SIZE = 6 * inch
IMAGE_TOP = IMAGE_BOTTOM + IMAGE_SIZE

LABEL_FONT_SIZE = 14
LABEL_LINE_HEIGHT = LABEL_FONT_SIZE * 1.2  # typical single-line leading, in points
LABEL_Y = IMAGE_TOP + 1.5 * LABEL_LINE_HEIGHT  # 1.5 lines of clearance above the image

# Courier's fixed character width is exactly 0.6em - solving for the font size that makes a
# 45-character hash span the same width as the square solution image above it.
HASH_LENGTH = 45
HASH_TARGET_WIDTH = IMAGE_SIZE
HASH_FONT_SIZE = HASH_TARGET_WIDTH / (HASH_LENGTH * 0.6)
HASH_Y = 2.0 * inch


def split_count(total, n_buckets):
    """Divides `total` as evenly as possible across n_buckets, distributing the remainder to the
    first buckets - e.g. split_count(90, 4) -> [23, 23, 22, 22]."""
    base, remainder = divmod(total, n_buckets)
    return [base + (1 if i < remainder else 0) for i in range(n_buckets)]


def sample_evenly(items, count):
    """Picks `count` items evenly spaced through the list, same idea as make_45_book.py's
    `step = num_solns // 45`. If the category has fewer items than requested, returns all of it."""
    if count >= len(items):
        return list(items)
    step = len(items) / count
    return [items[int(i * step)] for i in range(count)]


def build_solution(hash_str):
    pieces = piece_geometry.reconstruct_pieces(hash_str)
    s = solution.Solution()
    s.piece_locations = [solution.PieceLocation(x, y, sz) for x, y, sz in pieces]
    s.hash = hash_str
    return s


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                      formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("count", nargs="?", type=int, default=DEFAULT_COUNT,
                         help=f"Total number of solutions in the book, split evenly across the "
                              f"four categories (default: {DEFAULT_COUNT}).")
    parser.add_argument("--solutions-db", default=solution_db.DEFAULT_SOLUTIONS_DB)
    parser.add_argument("--out", default=os.path.join(SCRIPT_DIR, "category_book.pdf"))
    args = parser.parse_args()

    conn = sqlite3.connect(args.solutions_db)
    missing = conn.execute(
        "SELECT COUNT(*) FROM solutions WHERE category IS NULL"
    ).fetchone()[0]
    if missing:
        print(f"[book] {missing:,} solution(s) have no stored category yet - run "
              f"classify_li_other.py first to backfill them (fast after that, since categories "
              f"are read straight from the database from then on).", file=sys.stderr)
        sys.exit(1)

    buckets = {c: [] for c in CATEGORIES}
    for category, h in conn.execute("SELECT category, hash FROM solutions"):
        buckets[category].append(h)

    for c in CATEGORIES:
        print(f"[book] {c}: {len(buckets[c]):,} available")

    targets = dict(zip(CATEGORIES, split_count(args.count, len(CATEGORIES))))

    os.makedirs(IMAGE_DIR, exist_ok=True)
    pages = []  # (image_path, category, hash)
    for category in CATEGORIES:
        available = buckets[category]
        wanted = targets[category]
        if wanted > len(available):
            print(f"[book] WARNING: only {len(available)} '{category}' solutions exist, "
                  f"wanted {wanted} - using all of them.", file=sys.stderr)
        chosen = sample_evenly(available, wanted)
        for h in chosen:
            s = build_solution(h)
            img_path = os.path.join(IMAGE_DIR, f"{category}_{h}.png")
            s.save_to_png(img_path)
            pages.append((img_path, category, h))

    print(f"[book] rendered {len(pages)} solution(s) - writing {args.out}")
    c = canvas.Canvas(args.out, pagesize=letter)
    page_width, page_height = letter
    for img_path, category, h in pages:
        c.drawImage(img_path, 1.25 * inch, IMAGE_BOTTOM, width=IMAGE_SIZE, height=IMAGE_SIZE)
        c.setFont("Helvetica-Bold", LABEL_FONT_SIZE)
        c.drawCentredString(page_width / 2, LABEL_Y, CATEGORY_LABELS[category])
        c.setFont("Courier", HASH_FONT_SIZE)
        c.drawCentredString(page_width / 2, HASH_Y, h)
        c.showPage()
    c.save()
    print(f"[book] done: {args.out}")


if __name__ == "__main__":
    main()
