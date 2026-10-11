"""Timing plans for real, with a deadline so a catastrophic plan cannot hang.

The plan space contains plans that are five orders of magnitude slower than
the best one (an unindexed nested loop over 396850 x 88056 rows), so every
measurement is run under a progress handler that aborts the statement once a
deadline passes. An aborted plan is recorded as censored at the deadline, and
censored values are only ever used in a direction that understates the result.

Timing protocol: round robin across the candidates, one window each per round,
and keep the minimum over rounds. Running all repeats of one candidate before
moving on puts each candidate in its own patch of machine time, which inverts
orderings when the machine is doing anything else.
"""

import sqlite3
import time

CENSORED = None


def connect(db_path, cache_mb=96):
    con = sqlite3.connect(db_path)
    con.execute("PRAGMA cache_size=%d" % (-1024 * cache_mb))
    con.execute("PRAGMA temp_store=MEMORY")
    return con


def timed(con, sql, deadline):
    """(seconds, value) or (None, None) if the deadline aborted the statement."""
    t0 = time.perf_counter()

    def handler():
        return 1 if time.perf_counter() - t0 > deadline else 0

    con.set_progress_handler(handler, 4000)
    try:
        row = con.execute(sql).fetchone()
        return time.perf_counter() - t0, (row[0] if row else None)
    except sqlite3.OperationalError:
        return None, None
    finally:
        con.set_progress_handler(None, 0)


def measure_variants(db_path, sqls, rounds=3, cap=0.6, slack=4.0, first=()):
    """Time each sql in sqls (a dict key -> sql). Returns (times, values).

    times[key] is seconds, or None if censored. The deadline tracks the best
    time seen so far across all candidates, updated continuously rather than
    once per round, because a single unindexed nested loop over this corpus
    runs for minutes and a three way join's plan space holds dozens of them.
    Candidates named in `first` are measured before the rest; passing the plan
    the planner would have chosen anyway establishes a tight deadline on the
    very first measurement instead of paying the full cap for it.
    """
    con = connect(db_path)
    keys = [k for k in first if k in sqls]
    keys += [k for k in sqls if k not in set(keys)]
    best = dict((k, float("inf")) for k in keys)
    values = {}
    censored = set()
    running = float("inf")
    for rnd in range(rounds):
        for k in keys:
            if k in censored:
                continue
            if running == float("inf"):
                dl = cap
            else:
                dl = min(cap, max(slack * running, 0.01))
            dt, val = timed(con, sqls[k], dl)
            if dt is None:
                censored.add(k)
                continue
            if dt < best[k]:
                best[k] = dt
            if dt < running:
                running = dt
            values[k] = val
    con.close()
    times = {}
    for k in keys:
        times[k] = None if k in censored or best[k] == float("inf") else best[k]
    return times, values


def run_workload(db_path, queries, sqls, rounds=4, cap=1.0):
    """End to end measurement of the workload under whatever stats are installed.

    A fresh connection is opened so no stale prepared statement can survive a
    statistics change, then every query is run once to warm the page cache
    before any timing round counts.
    """
    con = connect(db_path)
    from .oracle import plan_signature
    out = {}
    for q in queries:
        out[q.name] = {"seconds": float("inf"), "censored": False,
                       "value": None,
                       "plan": plan_signature(con, sqls[q.name])}
    for q in queries:  # warm up
        timed(con, sqls[q.name], cap)
    for rnd in range(rounds):
        for q in queries:
            rec = out[q.name]
            if rec["censored"]:
                continue
            dt, val = timed(con, sqls[q.name], cap)
            if dt is None:
                rec["censored"] = True
                rec["seconds"] = cap
                continue
            rec["seconds"] = min(rec["seconds"], dt)
            rec["value"] = val
    con.close()
    return out


def run_configs(db_path, queries, configs, stats_mod, rounds=5, cap=10.0,
                verbose=True):
    """Time the whole workload under several statistics configurations.

    Configurations are visited round robin, one window each per round, and the
    minimum per (configuration, query) is kept. Running one configuration to
    completion before starting the next would put each of them in its own
    patch of machine time, which is enough to invert the ordering when
    anything else is running.

    configs: [(label, vector_or_None, sqls_dict)]. The sqls dict lets a
    configuration supply rewritten SQL per query, which is how the per-query
    hint baseline is measured: it is the only configuration that changes the
    application's SQL rather than the statistics.
    """
    best = {}
    plansig = {}
    answers = {}
    for label, vec, sqls in configs:
        for q in queries:
            best[(label, q.name)] = float("inf")
    for rnd in range(rounds + 1):
        for label, vec, sqls in configs:
            if vec is None:
                stats_mod.drop_stats(db_path)
            else:
                stats_mod.write_stats(db_path, vec)
            con = connect(db_path)
            for q in queries:
                sql = sqls[q.name]
                if rnd == 0:
                    plansig[(label, q.name)] = _sig(con, sql)
                dt, val = timed(con, sql, cap)
                if rnd == 0:
                    continue  # warm up round, not counted
                if dt is None:
                    best[(label, q.name)] = min(best[(label, q.name)], cap)
                    answers.setdefault((label, q.name), "censored")
                else:
                    best[(label, q.name)] = min(best[(label, q.name)], dt)
                    answers[(label, q.name)] = val
            con.close()
        if verbose and rnd > 0:
            print("    round %d of %d done" % (rnd, rounds), flush=True)
    out = {}
    for label, vec, sqls in configs:
        per = {}
        for q in queries:
            t = best[(label, q.name)]
            per[q.name] = {
                "seconds": t,
                "censored": answers.get((label, q.name)) == "censored",
                "answer": answers.get((label, q.name)),
                "plan": plansig[(label, q.name)],
            }
        out[label] = {"per_query": per,
                      "total": sum(v["seconds"] for v in per.values())}
    return out


def _sig(con, sql):
    from .oracle import plan_signature
    return plan_signature(con, sql)


def remeasure_censored(db_path, queries, configs, stats_mod, measured,
                       hard_cap=400.0, verbose=True):
    """Replace deadline-censored entries with one real, undeadlined timing.

    A censored entry is only a lower bound, so a total containing one
    understates that configuration. For a baseline this makes the comparison
    look better than it is, which is the wrong direction to be wrong in when
    the baseline is what you are claiming to beat. Each censored
    (configuration, query) is therefore run once more with a deadline large
    enough to finish, and the real number replaces the bound. Once, not per
    round: these are the plans that take minutes.
    """
    byname = dict((q.name, q) for q in queries)
    fixed = []
    for label, vec, sqls in configs:
        todo = [n for n, r in measured[label]["per_query"].items()
                if r["censored"]]
        if not todo:
            continue
        if vec is None:
            stats_mod.drop_stats(db_path)
        else:
            stats_mod.write_stats(db_path, vec)
        con = connect(db_path)
        for name in todo:
            if verbose:
                print("    re-measuring %s under %s with no useful deadline"
                      % (name, label), flush=True)
            dt, val = timed(con, sqls[name], hard_cap)
            rec = measured[label]["per_query"][name]
            if dt is None:
                rec["hit_hard_cap"] = True
                rec["seconds"] = hard_cap
            else:
                rec["censored"] = False
                rec["remeasured"] = True
                rec["seconds"] = dt
                rec["answer"] = val
                fixed.append((label, name, dt))
            if verbose:
                print("      %s: %.1f ms" % (name, rec["seconds"] * 1000),
                      flush=True)
        con.close()
        measured[label]["total"] = sum(
            r["seconds"] for r in measured[label]["per_query"].values())
    return fixed
