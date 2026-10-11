"""Reading, writing and parameterising the sqlite_stat1 table.

sqlite_stat1 holds one text row per index: the table cardinality followed by
one integer per column prefix giving the average number of rows that match an
equality constraint on that prefix. Without SQLITE_ENABLE_STAT4 (off in the
CPython build, verified via PRAGMA compile_options) those averages are the
entire statistical picture the planner has. There are no histograms, so a
value occurring 44875 times and a value occurring once share one estimate.

A StatVector is that information in a form a search can move: fixed
cardinalities plus a flat integer vector of the selectivity estimates.
"""

import math
import sqlite3


class StatVector:
    """Ordered selectivity estimates for every index in a database."""

    def __init__(self, entries, values):
        # entries: list of (tbl, idx, nrow, ncols); values: flat list of ints
        self.entries = entries
        self.values = list(values)
        self.slices = []
        off = 0
        for _, _, _, ncols in entries:
            self.slices.append((off, off + ncols))
            off += ncols
        assert off == len(self.values), (off, len(self.values))

    def __len__(self):
        return len(self.values)

    def copy(self):
        return StatVector(self.entries, self.values)

    def labels(self):
        out = []
        for (tbl, idx, nrow, ncols), (lo, hi) in zip(self.entries, self.slices):
            for j in range(hi - lo):
                out.append("%s.%s[%d]" % (tbl, idx, j))
        return out

    def owner(self, coord):
        """(tbl, idx, nrow) of the index that coordinate `coord` belongs to."""
        for (tbl, idx, nrow, ncols), (lo, hi) in zip(self.entries, self.slices):
            if lo <= coord < hi:
                return tbl, idx, nrow
        raise IndexError(coord)

    def to_rows(self):
        rows = []
        for (tbl, idx, nrow, ncols), (lo, hi) in zip(self.entries, self.slices):
            parts = [str(nrow)] + [str(max(1, int(v))) for v in self.values[lo:hi]]
            rows.append((tbl, idx, " ".join(parts)))
        return rows

    def distance(self, other):
        """Total LogEst distance, i.e. how big a lie this vector tells.

        Measured in tenths of a bit, the unit SQLite itself stores estimates in,
        so a distance of 10 means one coordinate was moved by a factor of two.
        """
        return sum(abs(logest(a) - logest(b))
                   for a, b in zip(self.values, other.values))

    def __repr__(self):
        return "StatVector(%d coords, %d indexes)" % (len(self.values),
                                                      len(self.entries))


def logest(x):
    """SQLite stores estimates as 10*log2(x) in a 16 bit int. Same scale here."""
    return int(round(10.0 * math.log2(max(1.0, float(x)))))


def logest_grid(nrow):
    """Smallest integer for each distinct LogEst value in 1..nrow.

    Two estimates that round to the same LogEst are indistinguishable to the
    planner, so this grid is the full set of candidate values for a coordinate
    up to that quantisation. It is deduplicated again by observed plan later,
    because this is a model of SQLite's representation, not a copy of it.
    """
    out, seen = [], set()
    v = 1
    while v <= nrow:
        le = logest(v)
        if le not in seen:
            seen.add(le)
            out.append(v)
        v = max(v + 1, int(v * 1.07) + 1)
    if out[-1] != nrow and nrow >= 1:
        out.append(nrow)
    return out


def index_shapes(con):
    """[(tbl, idx, ncols)] for every ordinary index, in a stable order."""
    out = []
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for t in tables:
        for row in con.execute("PRAGMA index_list(%s)" % t):
            name = row[1]
            if name.startswith("sqlite_autoindex"):
                continue
            ncols = len(list(con.execute("PRAGMA index_info(%s)" % name)))
            out.append((t, name, ncols))
    return sorted(out)


def analyze_vector(db_path):
    """Run ANALYZE on the real database and return the resulting StatVector."""
    con = sqlite3.connect(db_path)
    con.execute("ANALYZE")
    con.commit()
    shapes = index_shapes(con)
    raw = dict(((t, i), s) for (t, i, s) in
               con.execute("SELECT tbl, idx, stat FROM sqlite_stat1"
                           " WHERE idx IS NOT NULL"))
    entries, values = [], []
    for tbl, idx, ncols in shapes:
        parts = raw[(tbl, idx)].split()
        nrow = int(parts[0])
        eq = [int(p) for p in parts[1:1 + ncols]]
        while len(eq) < ncols:
            eq.append(eq[-1] if eq else 1)
        entries.append((tbl, idx, nrow, ncols))
        values.extend(eq)
    con.close()
    return StatVector(entries, values)


def flat_vector(base, factor=1):
    """Every estimate set to nrow*factor: the 'index is useless' extreme."""
    vals = []
    for (tbl, idx, nrow, ncols), (lo, hi) in zip(base.entries, base.slices):
        vals.extend([max(1, int(nrow * factor))] * (hi - lo))
    return StatVector(base.entries, vals)


def scaled_vector(base, factor):
    """One global multiplier on every estimate: the single-knob straw man."""
    vals = []
    for (tbl, idx, nrow, ncols), (lo, hi) in zip(base.entries, base.slices):
        for v in base.values[lo:hi]:
            vals.append(max(1, min(nrow, int(round(v * factor)))))
    return StatVector(base.entries, vals)


def _ensure_stat1(con):
    """Make sure sqlite_stat1 exists without re-analysing half a million rows.

    Only ANALYZE can create the table, and on this corpus a full ANALYZE costs
    about half a second. Installing a statistics vector happens dozens of times
    in a single comparison run, so the scan is paid once and then skipped.
    """
    row = con.execute("SELECT name FROM sqlite_master WHERE type='table'"
                      " AND name='sqlite_stat1'").fetchone()
    if row is None:
        con.execute("ANALYZE")


def write_stats(db_path, vector):
    """Install a StatVector into the real database file, or clear it."""
    con = sqlite3.connect(db_path)
    _ensure_stat1(con)
    con.execute("DELETE FROM sqlite_stat1")
    if vector is not None:
        con.executemany(
            "INSERT INTO sqlite_stat1(tbl, idx, stat) VALUES(?,?,?)",
            vector.to_rows())
    con.commit()
    con.close()


def drop_stats(db_path):
    """Remove all statistics: what the planner sees before anyone runs ANALYZE."""
    con = sqlite3.connect(db_path)
    _ensure_stat1(con)
    con.execute("DELETE FROM sqlite_stat1")
    con.commit()
    con.close()
