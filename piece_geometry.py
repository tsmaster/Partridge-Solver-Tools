#!/usr/bin/env python3
"""Pure piece-geometry helpers for working with a solution purely from its 45-character hash - no
dependency on solution_db.py (deliberately: solution_db.py itself needs to call classify_detailed()
at ingest time, and rotate_priority.py/classify_li_other.py both need a default solutions.db path
from solution_db.py for their own CLIs - keeping the actual geometry logic here, with zero imports
of solution_db, avoids a circular import between the two).

Used by rotate_priority.py (rotation-based range prioritization), classify_li_other.py and
make_category_book.py (the L/I/other structural classification - see Claude/TODO.txt and
Claude/RESULTS.txt for the full investigation this came out of), and solution_db.py (which stores
each solution's category at ingest time so it never needs recomputing)."""

FRAME_WIDTH = 45
WIDTH_MASK = (1 << FRAME_WIDTH) - 1

# Canonical (x, y) positions a complete edge-band's five 9x9 tiles must occupy.
EDGE_BANDS = {
    "top": frozenset((x, 0) for x in (0, 9, 18, 27, 36)),
    "bottom": frozenset((x, 36) for x in (0, 9, 18, 27, 36)),
    "left": frozenset((0, y) for y in (0, 9, 18, 27, 36)),
    "right": frozenset((36, y) for y in (0, 9, 18, 27, 36)),
}


def reconstruct_pieces(hash_str):
    """Given a 45-char hash (tile size at each successive raster placement, in the order
    solution.py's get_hash() produces), replays the raster-scan process to recover every piece's
    (x, y, sz) - the hash alone doesn't carry positions, but the placement order and "always the
    first free cell" rule together determine them uniquely."""
    occ = [0] * FRAME_WIDTH
    hint_x, hint_y = 0, 0
    pieces = []
    for ch in hash_str:
        sz = int(ch)
        free_bits = (~occ[hint_y]) & WIDTH_MASK & (~0 << hint_x)
        if free_bits:
            y = hint_y
        else:
            y = hint_y + 1
            while True:
                free_bits = (~occ[y]) & WIDTH_MASK
                if free_bits:
                    break
                y += 1
        x = (free_bits & -free_bits).bit_length() - 1
        pieces.append((x, y, sz))
        mask = ((1 << sz) - 1) << x
        for row in range(y, y + sz):
            occ[row] |= mask
        hint_x, hint_y = x, y
    return pieces


def rotate_pieces(pieces, turns):
    """turns: 1 = 90 CW, 2 = 180, 3 = 270 CW (= 90 CCW). Standard rotation formulas for an
    axis-aligned sz-by-sz square with top-left (x, y) inside an N-wide grid."""
    n = FRAME_WIDTH
    out = []
    for x, y, sz in pieces:
        if turns == 1:
            nx, ny = n - sz - y, x
        elif turns == 2:
            nx, ny = n - sz - x, n - sz - y
        elif turns == 3:
            nx, ny = y, n - sz - x
        else:
            raise ValueError(turns)
        out.append((nx, ny, sz))
    return out


def pieces_to_hash(pieces):
    return "".join(str(sz) for _x, _y, sz in sorted(pieces, key=lambda p: (p[1], p[0])))


def has_interior_line(nine_positions):
    """True if there's a straight line of five 9x9 tiles spanning the board's full width or
    height, NOT flush against an edge (x/y in 1..35 rather than 0/36) - the ambiguous "interior
    column" case from the L/I/other investigation: Parker's "I" turned out to include this, not
    just edge-flush lines."""
    for x in range(1, 36):
        if frozenset((x, y) for y in (0, 9, 18, 27, 36)) <= nine_positions:
            return True
    for y in range(1, 36):
        if frozenset((x, y) for x in (0, 9, 18, 27, 36)) <= nine_positions:
            return True
    return False


def classify(hash_str):
    """L/I/other only - see classify_detailed() for the edge-vs-interior split within "I"."""
    detailed = classify_detailed(hash_str)
    return "I" if detailed.startswith("I") else detailed


def classify_detailed(hash_str):
    """Splits "I" into "I_edge" (a line flush against the board's border) vs "I_interior" (a line
    running through the middle, not touching any edge)."""
    pieces = reconstruct_pieces(hash_str)
    nine_positions = frozenset((x, y) for x, y, sz in pieces if sz == 9)
    bands_present = [name for name, band in EDGE_BANDS.items() if band <= nine_positions]
    if len(bands_present) == 2:
        return "L"
    elif len(bands_present) == 1:
        return "I_edge"
    elif has_interior_line(nine_positions):
        return "I_interior"
    else:
        return "other"
