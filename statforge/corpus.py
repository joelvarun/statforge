"""Build a real, skewed relational corpus out of the local Python installation.

No downloads: every row comes from parsing source files that ship with this
interpreter and its site-packages. The point of using real source is the skew.
Column value frequencies here span five orders of magnitude (a handful of
import targets and builtin call names dominate, a long tail appears once),
which is exactly the regime where a single average rows-per-value statistic
cannot describe a column.
"""

import ast
import os
import sqlite3
import sysconfig

SCHEMA = [
    """CREATE TABLE files(
         id INTEGER PRIMARY KEY, path TEXT NOT NULL, module TEXT NOT NULL,
         pkg TEXT NOT NULL, nbytes INTEGER NOT NULL, nlines INTEGER NOT NULL)""",
    """CREATE TABLE symbols(
         id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, name TEXT NOT NULL,
         kind TEXT NOT NULL, lineno INTEGER NOT NULL, nargs INTEGER NOT NULL,
         depth INTEGER NOT NULL)""",
    """CREATE TABLE imports(
         id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, target TEXT NOT NULL,
         is_from INTEGER NOT NULL, level INTEGER NOT NULL)""",
    """CREATE TABLE calls(
         id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL, func TEXT NOT NULL,
         nargs INTEGER NOT NULL, lineno INTEGER NOT NULL)""",
]

# A plausible index set: a few candidates per table so the planner has real
# choices to make, and two multi-column indexes so stat1 rows carry more than
# one selectivity estimate.
INDEXES = [
    "CREATE INDEX ix_files_pkg ON files(pkg)",
    "CREATE INDEX ix_files_nlines ON files(nlines)",
    "CREATE INDEX ix_files_module ON files(module)",
    "CREATE INDEX ix_sym_file ON symbols(file_id)",
    "CREATE INDEX ix_sym_name ON symbols(name)",
    "CREATE INDEX ix_sym_kind_name ON symbols(kind, name)",
    "CREATE INDEX ix_sym_nargs ON symbols(nargs)",
    "CREATE INDEX ix_imp_file ON imports(file_id)",
    "CREATE INDEX ix_imp_target ON imports(target)",
    "CREATE INDEX ix_call_file ON calls(file_id)",
    "CREATE INDEX ix_call_func ON calls(func)",
    "CREATE INDEX ix_call_nargs ON calls(nargs)",
]

SKIP_DIRS = {"__pycache__", ".git", "lib2to3", "idle_test"}


def source_roots():
    """Directories scanned for .py files, most important first."""
    paths = sysconfig.get_paths()
    roots = [paths["stdlib"]]
    for key in ("purelib", "platlib"):
        p = paths.get(key)
        if p and os.path.isdir(p) and p not in roots:
            roots.append(p)
    return [r for r in roots if os.path.isdir(r)]


def iter_files(roots):
    """Yield (path, root) for every .py file under roots, in a stable order."""
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for fn in sorted(filenames):
                if fn.endswith(".py"):
                    yield os.path.join(dirpath, fn), root


def _call_name(node):
    """Flat name for a call target: 'len', 'os.path.join' -> 'join'."""
    f = node.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return None


def extract(path, root, file_id):
    """Parse one file into (file_row, symbol_rows, import_rows, call_rows)."""
    with open(path, "rb") as fh:
        raw = fh.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return None
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError, RecursionError):
        return None

    rel = os.path.relpath(path, root).replace(os.sep, "/")
    module = os.path.splitext(os.path.basename(path))[0]
    pkg = rel.split("/")[0] if "/" in rel else "<top>"
    frow = (file_id, rel, module, pkg, len(raw), text.count(chr(10)) + 1)

    syms, imps, calls = [], [], []
    depth = {id(tree): 0}
    for parent in ast.walk(tree):
        d = depth.get(id(parent), 0)
        for child in ast.iter_child_nodes(parent):
            step = 1 if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef,
                                            ast.ClassDef)) else 0
            depth[id(child)] = d + step
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef)):
            kind = "method" if d > 0 else "function"
            n = len(parent.args.posonlyargs) + len(parent.args.args) + \
                len(parent.args.kwonlyargs)
            syms.append((file_id, parent.name, kind, parent.lineno, n, d))
        elif isinstance(parent, ast.ClassDef):
            syms.append((file_id, parent.name, "class", parent.lineno, 0, d))
        elif isinstance(parent, ast.Import):
            for al in parent.names:
                imps.append((file_id, al.name.split(".")[0], 0, 0))
        elif isinstance(parent, ast.ImportFrom):
            imps.append((file_id, (parent.module or "").split(".")[0] or ".",
                         1, parent.level))
        elif isinstance(parent, ast.Call):
            nm = _call_name(parent)
            if nm is not None:
                calls.append((file_id, nm, len(parent.args), parent.lineno))
    return frow, syms, imps, calls


def build(db_path, limit=None, verbose=True):
    """Create db_path and populate it from the local Python install.

    Returns a dict of row counts. Idempotent in the sense that an existing
    file is left alone; delete it to force a rebuild.
    """
    if os.path.exists(db_path):
        return counts(db_path)
    tmp = db_path + ".partial"
    if os.path.exists(tmp):
        os.remove(tmp)
    con = sqlite3.connect(tmp)
    con.execute("PRAGMA journal_mode=OFF")
    con.execute("PRAGMA synchronous=OFF")
    for stmt in SCHEMA:
        con.execute(stmt)

    roots = source_roots()
    fid = 0
    nsym = nimp = ncall = 0
    frows, srows, irows, crows = [], [], [], []
    for path, root in iter_files(roots):
        if limit is not None and fid >= limit:
            break
        got = extract(path, root, fid)
        if got is None:
            continue
        frow, syms, imps, calls = got
        frows.append(frow)
        srows.extend(syms)
        irows.extend(imps)
        crows.extend(calls)
        fid += 1
        if len(srows) > 50000:
            nsym += _flush(con, srows, "symbols",
                           "file_id, name, kind, lineno, nargs, depth")
            ncall += _flush(con, crows, "calls", "file_id, func, nargs, lineno")
            nimp += _flush(con, irows, "imports",
                           "file_id, target, is_from, level")
            con.executemany(
                "INSERT INTO files(id, path, module, pkg, nbytes, nlines)"
                " VALUES(?,?,?,?,?,?)", frows)
            frows = []
    nsym += _flush(con, srows, "symbols",
                   "file_id, name, kind, lineno, nargs, depth")
    ncall += _flush(con, crows, "calls", "file_id, func, nargs, lineno")
    nimp += _flush(con, irows, "imports", "file_id, target, is_from, level")
    con.executemany(
        "INSERT INTO files(id, path, module, pkg, nbytes, nlines)"
        " VALUES(?,?,?,?,?,?)", frows)
    for stmt in INDEXES:
        con.execute(stmt)
    con.commit()
    con.execute("VACUUM")
    con.close()
    os.replace(tmp, db_path)
    out = {"files": fid, "symbols": nsym, "imports": nimp, "calls": ncall}
    if verbose:
        print("corpus built from %d roots: %s" % (len(roots), out))
    return out


def _flush(con, rows, table, cols):
    if not rows:
        return 0
    marks = ",".join("?" * len(cols.split(",")))
    con.executemany("INSERT INTO %s(%s) VALUES(%s)" % (table, cols, marks), rows)
    n = len(rows)
    del rows[:]
    return n


def counts(db_path):
    con = sqlite3.connect(db_path)
    out = {}
    for t in ("files", "symbols", "imports", "calls"):
        out[t] = con.execute("SELECT count(*) FROM %s" % t).fetchone()[0]
    con.close()
    return out


def skew_report(db_path, columns=None):
    """Measured value-frequency skew per column, the thing stat1 averages away."""
    if columns is None:
        columns = [("imports", "target"), ("calls", "func"), ("symbols", "name"),
                   ("symbols", "kind"), ("files", "pkg"), ("symbols", "nargs")]
    con = sqlite3.connect(db_path)
    out = []
    for table, col in columns:
        rows = con.execute(
            "SELECT count(*) AS c FROM %s GROUP BY %s ORDER BY c DESC"
            % (table, col)).fetchall()
        freq = [r[0] for r in rows]
        total = sum(freq)
        out.append({
            "table": table, "column": col, "rows": total,
            "distinct": len(freq),
            "avg_per_value": total / len(freq),
            "max_per_value": freq[0],
            "min_per_value": freq[-1],
            "singletons": sum(1 for f in freq if f == 1),
            "skew_ratio": freq[0] / (total / len(freq)),
        })
    con.close()
    return out
