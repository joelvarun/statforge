"""Searching for the statistics that make the planner fastest.

The planner is a deterministic function of sqlite_stat1, so the statistics are
treatable as a control input rather than as a measurement. Two facts make the
search cheap:

* Every plan's real runtime is measured once into a bank, so scoring a
  candidate statistics vector costs one EXPLAIN QUERY PLAN per query (tens of
  microseconds) plus a dictionary lookup, not a query execution.
* Changing one index's estimate can only change the plan of a query that
  mentions that index's table, so a coordinate sweep re-plans a handful of
  queries instead of the whole workload.

One sqlite_stat1 serves every query in the workload. That is what makes this a
joint problem and not fifteen independent ones: there is no per-query knob, so
repairing one query's plan can spoil another's. The uncoupled ablation in
`greedy_uncoupled` measures that damage.

The search only ever selects among plans whose runtime has been measured. A
statistics vector that steers a query onto an unmeasured plan is charged the
deadline, so the search rejects it rather than gambling. Whether that
restriction cost anything is checked at the end by installing the chosen
statistics in the real database and timing the workload for real.
"""

from . import bench, plans, stats
from .oracle import PlannerOracle, plan_signature


class PlanBank:
    """Measured runtime per (query, plan), from forced plan enumeration."""

    def __init__(self, db_path, queries, cap=1.0):
        self.db_path = db_path
        self.queries = {q.name: q for q in queries}
        self.order = [q.name for q in queries]
        self.natural = dict((q.name, plans.natural_sql(q)) for q in queries)
        self.cap = cap
        self.times = {}        # (qname, sig) -> seconds, or None if censored
        self.forced = {}       # qname -> {sig: sql}
        self.meta = {}
        self.unknown = {}      # (qname, sig) -> times charged the cap

    def build(self, verbose=True):
        base = stats.analyze_vector(self.db_path)
        stats.write_stats(self.db_path, base)
        oracle = PlannerOracle(self.db_path)
        oracle.set_vector(base)
        for qname in self.order:
            q = self.queries[qname]
            found, meta = plans.enumerate_forced(self.db_path, q)
            chosen = oracle.plan(self.natural[qname])
            first = [chosen] if chosen in found else []
            times, values = bench.measure_variants(
                self.db_path, found, cap=self.cap, first=first)
            for sig, secs in times.items():
                self.times[(qname, sig)] = secs
            self.forced[qname] = found
            live = [s for s in times.values() if s is not None]
            self.meta[qname] = {
                "plans": len(found), "timed": len(live),
                "censored": len(found) - len(live),
                "best_ms": (min(live) * 1000.0) if live else None,
                "worst_timed_ms": (max(live) * 1000.0) if live else None,
                "chosen_was_forceable": chosen in found,
                "answers": sorted(set(v for v in values.values()
                                      if v is not None)),
                "enumeration": meta,
            }
            if verbose:
                m = self.meta[qname]
                print("  %-20s plans=%-4d timed=%-3d censored=%-3d"
                      " best=%8.2f ms worst=%9.2f ms"
                      % (qname, m["plans"], m["timed"], m["censored"],
                         m["best_ms"] if m["best_ms"] else -1,
                         m["worst_timed_ms"] if m["worst_timed_ms"] else -1))
        oracle.close()
        return self

    def cost(self, qname, sig):
        """Seconds for this query under this plan.

        Censored plans and plans the enumeration never produced are both
        charged the deadline, which is a lower bound on a censored plan's real
        cost. A baseline that picks one is therefore reported as faster than it
        is, never slower.
        """
        key = (qname, sig)
        if key in self.times:
            t = self.times[key]
            return self.cap if t is None else t
        self.unknown[key] = self.unknown.get(key, 0) + 1
        return self.cap

    def known(self, qname, sig):
        return (qname, sig) in self.times

    def oracle_best(self):
        """Per query minimum over all measured plans: the achievable floor."""
        out = {}
        for qname, found in self.forced.items():
            live = sorted((self.times[(qname, s)], s) for s in found
                          if self.times.get((qname, s)) is not None)
            out[qname] = live[0] if live else (self.cap, None)
        return out

    def answer_consistency(self):
        """Every timed plan for one query must return the same answer."""
        return [(q, m["answers"]) for q, m in self.meta.items()
                if len(m["answers"]) > 1]


class Scorer:
    """Total workload cost of a statistics vector, via the bank."""

    def __init__(self, bank, weights=None):
        self.bank = bank
        self.oracle = PlannerOracle(bank.db_path)
        self.names = list(bank.order)
        self.sqls = [bank.natural[n] for n in self.names]
        self.weights = weights or dict((n, 1.0) for n in self.names)
        # Which queries can a given table's statistics possibly affect.
        self.touch = {}
        for n in self.names:
            for table, _alias in bank.queries[n].tables:
                self.touch.setdefault(table, set()).add(n)
        self.evals = 0

    def plan_map(self, vector):
        self.oracle.set_vector(vector)
        self.evals += 1
        return dict((n, self.oracle.plan(s))
                    for n, s in zip(self.names, self.sqls))

    def total(self, vector):
        pm = self.plan_map(vector)
        return sum(self.weights[n] * self.bank.cost(n, pm[n])
                   for n in self.names), pm

    def partial(self, vector, subset):
        self.oracle.set_vector(vector)
        self.evals += 1
        out = {}
        for n in subset:
            out[n] = self.oracle.plan(self.bank.natural[n])
        return out

    def close(self):
        self.oracle.close()


def _subset_cost(scorer, pm, subset):
    return sum(scorer.weights[n] * scorer.bank.cost(n, pm[n]) for n in subset)


def coordinate_descent(scorer, start, reference, sweeps=8, verbose=True):
    """Exhaustive sweeps over one coordinate at a time on the LogEst grid.

    Each coordinate is a selectivity estimate for one index column prefix, and
    the candidate set is every value that SQLite's fixed point representation
    can tell apart, so a sweep is exact for that coordinate rather than a step
    in some direction. Ties are broken toward the value ANALYZE measured, so
    the result only departs from the true statistics where departing buys time.
    """
    cur = start.copy()
    total, pm = scorer.total(cur)
    trail = [total]
    for sweep in range(sweeps):
        moved = 0
        for c in range(len(cur)):
            tbl, idx, nrow = cur.owner(c)
            subset = sorted(scorer.touch.get(tbl, ()))
            if not subset:
                continue
            pm_now = scorer.partial(cur, subset)
            best_cost = _subset_cost(scorer, pm_now, subset)
            best_v = cur.values[c]
            best_dist = abs(stats.logest(best_v)
                            - stats.logest(reference.values[c]))
            grid = stats.logest_grid(nrow)
            if cur.values[c] not in grid:
                grid = sorted(set(grid + [cur.values[c]]))
            trial = cur.copy()
            for v in grid:
                if v == cur.values[c]:
                    continue
                trial.values[c] = v
                sub = _subset_cost(scorer, scorer.partial(trial, subset), subset)
                dist = abs(stats.logest(v) - stats.logest(reference.values[c]))
                if sub < best_cost - 1e-9 or (abs(sub - best_cost) <= 1e-9
                                              and dist < best_dist):
                    best_cost, best_v, best_dist = sub, v, dist
            if best_v != cur.values[c]:
                cur.values[c] = best_v
                moved += 1
        total, pm = scorer.total(cur)
        trail.append(total)
        if verbose:
            print("  sweep %d: %d coordinates moved, total %.1f ms"
                  % (sweep + 1, moved, total * 1000.0))
        if moved == 0:
            break
    return cur, total, pm, trail


def greedy_uncoupled(scorer, start, reference, cheapest_first=False,
                     verbose=True):
    """Ablation: repair queries one at a time, ignoring the rest.

    This is what tuning by hand looks like. Each query is optimised over the
    coordinates that affect it and the change is kept. Nothing checks whether
    the change spoiled an earlier query, because there is no separate knob to
    protect it with: the whole workload shares one sqlite_stat1.

    The order the queries are visited in is a free parameter with no defensible
    setting, which is itself the point. Visiting the expensive ones first means
    the cheap ones get the last word and can undo the repair that mattered.
    """
    cur = start.copy()
    total, pm = scorer.total(cur)
    ranked = sorted(scorer.names, key=lambda n: -scorer.bank.cost(n, pm[n]))
    if cheapest_first:
        ranked = ranked[::-1]
    for qname in ranked:
        tables = [t for t, _a in scorer.bank.queries[qname].tables]
        coords = [c for c in range(len(cur)) if cur.owner(c)[0] in tables]
        for c in coords:
            tbl, idx, nrow = cur.owner(c)
            best = scorer.bank.cost(qname, scorer.partial(cur, [qname])[qname])
            best_v = cur.values[c]
            trial = cur.copy()
            for v in stats.logest_grid(nrow):
                trial.values[c] = v
                sig = scorer.partial(trial, [qname])[qname]
                cost = scorer.bank.cost(qname, sig)
                if cost < best - 1e-9:
                    best, best_v = cost, v
            cur.values[c] = best_v
    total, pm = scorer.total(cur)
    if verbose:
        print("  uncoupled greedy total %.1f ms" % (total * 1000.0))
    return cur, total, pm


def scaled_sweep(scorer, base, factors=None):
    """Baseline: one global multiplier on every estimate."""
    if factors is None:
        factors = [0.02, 0.05, 0.1, 0.25, 0.5, 1.0, 2.0, 4.0, 10.0, 25.0,
                   100.0, 400.0, 1600.0]
    best = None
    for f in factors:
        vec = stats.scaled_vector(base, f)
        total, pm = scorer.total(vec)
        if best is None or total < best[1]:
            best = (vec, total, pm, f)
    return best


def evaluate(scorer, vector):
    total, pm = scorer.total(vector)
    per = dict((n, scorer.bank.cost(n, pm[n])) for n in scorer.names)
    return total, pm, per


def save_bank(bank, path):
    """Persist measured plan times so a rerun can skip the timing pass."""
    import json
    payload = {
        "cap": bank.cap,
        "order": bank.order,
        "times": [[q, s, t] for (q, s), t in bank.times.items()],
        "forced": bank.forced,
        "meta": bank.meta,
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh)
    return path


def load_bank(db_path, queries, path):
    """Rebuild a PlanBank from a saved timing pass, or return None."""
    import json
    import os
    if not os.path.exists(path):
        return None
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    names = [q.name for q in queries]
    if payload.get("order") != names:
        return None
    bank = PlanBank(db_path, queries, cap=payload["cap"])
    bank.times = dict(((q, s), t) for q, s, t in payload["times"])
    bank.forced = payload["forced"]
    bank.meta = payload["meta"]
    return bank


def multi_start(scorer, base, verbose=True, tol=0.01):
    """Coordinate descent from several starts, then the least invented winner.

    Among starts whose totals land within `tol` of the best, the winner is the
    one that changes the fewest coordinates, with total LogEst distance as the
    tie break. The objective cannot separate them, and preferring the smallest
    lie keeps the result interpretable: a forged vector that differs from the
    measured truth in three coordinates says something about which estimates
    were load bearing, while one that differs in all thirteen does not. Fewest
    coordinates comes before smallest total distance deliberately, because the
    count is what identifies the responsible estimate.
    """
    starts = [("ANALYZE", base),
              ("all estimates = nrow", stats.flat_vector(base)),
              ("all estimates = 1", stats.scaled_vector(base, 0.0)),
              ("ANALYZE scaled 0.1", stats.scaled_vector(base, 0.1)),
              ("ANALYZE scaled 10", stats.scaled_vector(base, 10.0))]
    results = []
    for label, start in starts:
        vec, total, pm, trail = coordinate_descent(scorer, start, base,
                                                   verbose=False)
        results.append({"start": label, "vector": vec, "total": total,
                        "plan_map": pm, "sweeps": len(trail) - 1,
                        "distance": vec.distance(base),
                        "moved": sum(1 for a, b in zip(vec.values, base.values)
                                     if a != b)})
        if verbose:
            print("  start %-22s -> %8.1f ms  distance %4d  coords moved %2d"
                  % (label, total * 1000.0, results[-1]["distance"],
                     results[-1]["moved"]))
    floor = min(r["total"] for r in results)
    near = [r for r in results if r["total"] <= floor * (1.0 + tol)]
    near.sort(key=lambda r: (r["moved"], r["distance"], r["total"]))
    winner = near[0]
    if verbose:
        print("  chosen: start %s, %.1f ms, %d of %d coordinates changed"
              % (winner["start"], winner["total"] * 1000.0, winner["moved"],
                 len(base)))
    return winner, results
