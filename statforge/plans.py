"""Enumerating the plans a query can actually be made to run.

SQLite has no plan hint for join order, but CROSS JOIN is documented to
suppress reordering and INDEXED BY pins the index for one table reference.
Together they turn the plan space into something enumerable: every
permutation of the tables crossed with every per-table index choice.

Enumeration is done against the real database file because INDEXED BY is
rejected outright when the named index cannot serve the query, and that
rejection is part of the answer.
"""

import itertools
import sqlite3

from .oracle import plan_signature


def index_firstcols(con):
    """{table: [(index_name, first_column)]} for ordinary indexes."""
    out = {}
    tables = [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name NOT LIKE 'sqlite_%'")]
    for t in tables:
        lst = []
        for row in con.execute("PRAGMA index_list(%s)" % t):
            name = row[1]
            if name.startswith("sqlite_autoindex"):
                continue
            cols = list(con.execute("PRAGMA index_info(%s)" % name))
            lst.append((name, cols[0][2]))
        out[t] = sorted(lst)
    return out


def candidate_indexes(firstcols, query, alias, table):
    """Indexes worth trying for one table reference, plus the no-index option.

    An index whose leading column is never constrained cannot help, so it is
    dropped before timing rather than measured and discarded.
    """
    text = query.where
    keep = [None]
    for name, col in firstcols.get(table, []):
        if ("%s.%s" % (alias, col)) in text:
            keep.append(name)
    return keep


def enumerate_forced(db_path, query, max_variants=400):
    """{plan_signature: sql} over forced orders and index choices.

    Also returns the count of hint combinations SQLite rejected, which is a
    measured fact about how constrained the plan space really is.
    """
    con = sqlite3.connect(db_path)
    firstcols = index_firstcols(con)
    aliases = query.aliases()
    choices = []
    for table, alias in query.tables:
        choices.append(candidate_indexes(firstcols, query, alias, table))
    found, rejected, tried = {}, 0, 0
    n = len(query.tables)
    for order in itertools.permutations(range(n)):
        for combo in itertools.product(*choices):
            if tried >= max_variants:
                break
            tried += 1
            hints = dict(zip(aliases, combo))
            sql = query.sql(order=order, hints=hints)
            try:
                sig = plan_signature(con, sql)
            except sqlite3.OperationalError:
                rejected += 1
                continue
            found.setdefault(sig, sql)
    con.close()
    return found, {"tried": tried, "rejected": rejected, "distinct": len(found)}


def natural_sql(query):
    return query.sql(order=None, hints=None)
