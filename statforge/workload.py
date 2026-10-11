"""A workload of real analytic queries over the source-code corpus.

Constants are not hand-picked: they are drawn from measured value frequencies
in the corpus, so each template is instantiated once with a very common value
and once with a rare one. That pairing is the whole point. Both queries hit
the same column, so they share one sqlite_stat1 estimate, and no single
average is right for both. A planner with per-value histograms would not have
this problem; the default CPython SQLite build has none.
"""

import sqlite3


class Query:
    """One workload query, kept in pieces so join order can be forced."""

    def __init__(self, name, select, tables, where, weight=1.0, note=""):
        self.name = name
        self.select = select
        self.tables = tables          # [(table, alias)]
        self.where = where
        self.weight = weight
        self.note = note

    def sql(self, order=None, hints=None):
        """Render SQL. order pins the join order, hints pins index choice.

        With order=None the tables are comma joined, which leaves the planner
        free to reorder. With an order given, CROSS JOIN is used, which SQLite
        documents as suppressing reordering, so the plan is forced.
        """
        tables = self.tables if order is None else [self.tables[i] for i in order]
        parts = []
        for table, alias in tables:
            frag = "%s AS %s" % (table, alias)
            if hints is not None:
                ix = hints.get(alias, "keep")
                if ix is None:
                    frag += " NOT INDEXED"
                elif ix != "keep":
                    frag += " INDEXED BY " + ix
            parts.append(frag)
        sep = ", " if order is None else " CROSS JOIN "
        return "SELECT %s FROM %s WHERE %s" % (self.select, sep.join(parts),
                                               self.where)

    def aliases(self):
        return [a for _, a in self.tables]

    def __repr__(self):
        return "Query(%s)" % self.name


def _ranked(con, table, col, where=""):
    rows = con.execute(
        "SELECT %s, count(*) c FROM %s %s GROUP BY %s ORDER BY c DESC, %s"
        % (col, table, where, col, col)).fetchall()
    return rows


def pick_values(db_path):
    """Measured common and rare values for each skewed column."""
    con = sqlite3.connect(db_path)
    out = {}
    for table, col in [("calls", "func"), ("imports", "target"),
                       ("symbols", "name"), ("files", "pkg")]:
        rows = _ranked(con, table, col)
        rows = [r for r in rows if r[0] is not None and r[0] != ""]
        out[(table, col)] = {
            "top": rows[0],
            "mid": rows[len(rows) // 12],
            "rare": [r for r in rows if r[1] == 1][len(
                [r for r in rows if r[1] == 1]) // 2],
            "distinct": len(rows),
        }
    out["nlines_median"] = con.execute(
        "SELECT nlines FROM files ORDER BY nlines LIMIT 1"
        " OFFSET (SELECT count(*)/2 FROM files)").fetchone()[0]
    con.close()
    return out


def quote(v):
    if isinstance(v, str):
        return "'" + v.replace("'", "''") + "'"
    return str(v)


def build(db_path):
    """The workload: six templates, each instantiated at several frequencies."""
    v = pick_values(db_path)
    func_top = v[("calls", "func")]["top"][0]
    func_mid = v[("calls", "func")]["mid"][0]
    func_rare = v[("calls", "func")]["rare"][0]
    imp_top = v[("imports", "target")]["top"][0]
    imp_mid = v[("imports", "target")]["mid"][0]
    imp_rare = v[("imports", "target")]["rare"][0]
    sym_top = v[("symbols", "name")]["top"][0]
    sym_rare = v[("symbols", "name")]["rare"][0]
    pkg_top = v[("files", "pkg")]["top"][0]
    nl = v["nlines_median"]

    qs = []

    def add(name, select, tables, where, note):
        qs.append(Query(name, select, tables, where, note=note))

    # T1 call sites of one function name, joined to the file that holds them.
    for tag, fn in (("hot", func_top), ("mid", func_mid), ("rare", func_rare)):
        add("t1_callfile_" + tag, "count(*)",
            [("calls", "c"), ("files", "f")],
            "c.file_id = f.id AND c.func = %s AND f.nlines > %d" % (quote(fn), nl),
            "equality on calls.func (%s) plus a range on files.nlines" % tag)

    # T2 a symbol definition and a call of the same shape in one file.
    for tag, fn, sym in (("hot", func_top, sym_top), ("rare", func_rare, sym_rare)):
        add("t2_symcall_" + tag, "count(*)",
            [("symbols", "s"), ("calls", "c")],
            "c.file_id = s.file_id AND s.name = %s AND c.func = %s"
            % (quote(sym), quote(fn)),
            "two equality predicates on skewed text columns (%s)" % tag)

    # T3 files importing one module, filtered by size.
    for tag, tgt in (("hot", imp_top), ("mid", imp_mid), ("rare", imp_rare)):
        add("t3_impfile_" + tag, "count(*)",
            [("imports", "i"), ("files", "f")],
            "i.file_id = f.id AND i.target = %s AND f.pkg = %s"
            % (quote(tgt), quote(pkg_top)),
            "equality on imports.target (%s) plus files.pkg" % tag)

    # T4 three way join: definitions, their file, and what that file imports.
    for tag, tgt in (("hot", imp_top), ("rare", imp_rare)):
        add("t4_three_" + tag, "count(*)",
            [("symbols", "s"), ("files", "f"), ("imports", "i")],
            "s.file_id = f.id AND i.file_id = f.id AND s.kind = 'method'"
            " AND s.nargs = 1 AND i.target = %s" % quote(tgt),
            "three way join, equality on imports.target (%s)" % tag)

    # T5 calls and imports co-occurring in a file: two large tables, no small one.
    for tag, fn, tgt in (("hot", func_top, imp_top), ("rare", func_rare, imp_rare),
                         ("mixed", func_top, imp_rare)):
        add("t5_callimp_" + tag, "count(*)",
            [("calls", "c"), ("imports", "i")],
            "c.file_id = i.file_id AND c.func = %s AND i.target = %s"
            % (quote(fn), quote(tgt)),
            "two large tables joined on file_id (%s)" % tag)

    # T6 three way join across the two biggest tables.
    for tag, fn in (("mid", func_mid), ("rare", func_rare)):
        add("t6_three_" + tag, "count(*)",
            [("calls", "c"), ("files", "f"), ("symbols", "s")],
            "c.file_id = f.id AND s.file_id = f.id AND c.func = %s"
            " AND s.kind = 'class' AND f.nlines > %d" % (quote(fn), nl),
            "three way join over calls and symbols (%s)" % tag)

    return qs, v
