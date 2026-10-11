"""A cheap, exact stand-in for the SQLite query planner.

EXPLAIN QUERY PLAN needs the schema and sqlite_stat1, never the rows, so a
schema-only in-memory database can answer "which plan would be chosen under
these statistics" without touching a 36 MB file. That is what makes searching
over statistics affordable: plans are timed for real once, and the search
itself only ever asks the planner.

The mechanism that makes this correct is not obvious, and three plausible
versions of it are simply wrong. SQLite binds the contents of sqlite_stat1 to
its in-memory schema when a connection first reads that schema. Editing the
table afterwards updates the rows and changes nothing about planning. Measured
on this corpus, against fresh-connection ground truth over 60 statistics
changes and 15 queries:

    ANALYZE sqlite_master, legacy transactions      52 of 60 wrong
    ANALYZE sqlite_master, autocommit               52 of 60 wrong
    commit, then ANALYZE sqlite_master              52 of 60 wrong
    schema cookie bump (create then drop a table)   54 of 60 wrong

Deserialising an image and then inserting statistics fails the same way, for
the same reason: the schema was already read. The version that works puts the
statistics in the database *before* any connection reads it. A writer
connection holds the schema, takes the new statistics and serialises itself;
a fresh reader deserialises that image and is asked for plans. Verified at
zero mismatches over 150 (vector, query) pairs against the real file database,
reproducing all six of its distinct plan sets, at 722 microseconds per vector
for a 15 query workload.
"""

import sqlite3

_UNSET = object()


def schema_sql(db_path):
    """CREATE statements for every user table and index, in creation order."""
    con = sqlite3.connect(db_path)
    rows = [r[0] for r in con.execute(
        "SELECT sql FROM sqlite_master WHERE sql IS NOT NULL"
        " AND name NOT LIKE 'sqlite_%' ORDER BY rowid")]
    con.close()
    return rows


def plan_signature(con, sql):
    """Stable text form of a plan, including the tree shape."""
    rows = list(con.execute("EXPLAIN QUERY PLAN " + sql))
    return " ; ".join("%d<%d %s" % (r[0], r[1], r[3]) for r in rows)


class PlannerOracle:
    """Answers plan questions for arbitrary statistics with no data present."""

    def __init__(self, db_path):
        self.db_path = db_path
        self.writer = sqlite3.connect(":memory:", isolation_level=None)
        self.writer.execute("PRAGMA page_size=512")
        for stmt in schema_sql(db_path):
            self.writer.execute(stmt)
        self.writer.execute("ANALYZE")  # creates an empty sqlite_stat1
        self.reader = None
        self._installed = _UNSET
        self.calls = 0
        self.reloads = 0

    def set_vector(self, vector):
        rows = [] if vector is None else vector.to_rows()
        if rows == self._installed and self.reader is not None:
            return
        self.writer.execute("DELETE FROM sqlite_stat1")
        if rows:
            self.writer.executemany(
                "INSERT INTO sqlite_stat1(tbl, idx, stat) VALUES(?,?,?)", rows)
        image = self.writer.serialize()
        if self.reader is not None:
            self.reader.close()
        self.reader = sqlite3.connect(":memory:")
        self.reader.deserialize(image)
        self._installed = rows
        self.reloads += 1

    def plan(self, sql):
        self.calls += 1
        return plan_signature(self.reader, sql)

    def plans(self, vector, sqls):
        self.set_vector(vector)
        return tuple(self.plan(s) for s in sqls)

    def close(self):
        if self.reader is not None:
            self.reader.close()
        self.writer.close()


def verify_matches_file(db_path, sqls, vectors, stats_mod):
    """Check the in-memory oracle against the real file database.

    The oracle is only trustworthy if a planner with no rows in front of it
    chooses exactly what the planner with half a million rows chooses, for the
    same statistics. Checked over several vectors, since agreeing on one
    proves nothing: an oracle that ignores statistics entirely also agrees on
    one. Returns (pairs_checked, mismatches, distinct_plan_sets).
    """
    oracle = PlannerOracle(db_path)
    mismatches = []
    checked = 0
    seen = set()
    for vec in vectors:
        if vec is None:
            stats_mod.drop_stats(db_path)
        else:
            stats_mod.write_stats(db_path, vec)
        real = sqlite3.connect(db_path)
        oracle.set_vector(vec)
        got = []
        for sql in sqls:
            r = plan_signature(real, sql)
            w = oracle.plan(sql)
            got.append(r)
            checked += 1
            if r != w:
                mismatches.append((sql, w, r))
        real.close()
        seen.add(tuple(got))
    oracle.close()
    return checked, mismatches, len(seen)
