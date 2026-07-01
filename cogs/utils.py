"""
cogs/utils.py — Shared utility functions used across all cogs.
"""

def fmt(seconds: float) -> str:
    """Convert seconds to human-readable duration e.g. 1h 30m 5s"""
    s = int(seconds)
    h, r = divmod(s, 3600)
    m, s = divmod(r, 60)
    if h: return f"{h}h {m}m {s}s"
    if m: return f"{m}m {s}s"
    return f"{s}s"


def rank_suffix(n: int) -> str:
    """Convert integer to ordinal string e.g. 1 -> 1st, 2 -> 2nd"""
    if 11 <= (n % 100) <= 13: return f"{n}th"
    return {1: f"{n}st", 2: f"{n}nd", 3: f"{n}rd"}.get(n % 10, f"{n}th")
