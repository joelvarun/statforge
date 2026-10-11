"""End to end run: build the corpus, time the plans, search, verify, chart.

Everything printed and everything written to docs/ comes from a measurement
made during this run. Nothing is a constant carried over from a previous one.
"""

import argparse
import json
import os
import platform
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from statforge import (bench, chart, corpus, oracle, plans, search, stats,
                       workload)

ROOT = os.path.dirname(os.path.abspath(__file__))


def rule(title):
    print()
    print(title)
    print("-" * len(title))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--quick", action="store_true",
                    help="small corpus and fewer rounds, for a smoke test;"
                         " writes separate output files")
    ap.add_argument("--reuse-bank", action="store_true",
                    help="reuse a cached plan timing pass if one exists")
    ap.add_argument("--rounds", type=int, default=0,
                    help="timing rounds for the end to end comparison")
    ap.add_argument("--cap", type=float, default=12.0,
                    help="per query deadline in seconds for the end to end run")
    ap.add_argument("--hard-cap", type=float, default=400.0,
                    help="deadline for the single re-run of each censored"
                         " query, so no reported total is a lower bound")
    args = ap.parse_args(argv)

    tag = "_quick" if args.quick else ""
    rounds = args.rounds or (2 if args.quick else 5)
    cache = os.path.join(ROOT, ".cache")
    docs = os.path.join(ROOT, "docs")
    os.makedirs(cache, exist_ok=True)
    os.makedirs(docs, exist_ok=True)
    db = os.path.join(cache, "corpus%s.db" % tag)
    bank_path = os.path.join(cache, "bank%s.json" % tag)
    t_start = time.perf_counter()

    rule("1. corpus")
    counts = corpus.build(db, limit=350 if args.quick else None)
    total_rows = sum(counts.values())
    print("rows: %s (total %d), %.1f MB"
          % (counts, total_rows, os.path.getsize(db) / 1e6))
    skew = corpus.skew_report(db)
    print("%-10s %-8s %9s %9s %10s %10s %8s"
          % ("table", "column", "rows", "distinct", "avg/value",
             "max/value", "skew"))
    for r in skew:
        print("%-10s %-8s %9d %9d %10.2f %10d %7.0fx"
              % (r["table"], r["column"], r["rows"], r["distinct"],
                 r["avg_per_value"], r["max_per_value"], r["skew_ratio"]))

    rule("2. workload")
    queries, picked = workload.build(db)
    natural = dict((q.name, plans.natural_sql(q)) for q in queries)
    base = stats.analyze_vector(db)
    print("%d queries over %d indexes, %d statistics coordinates"
          % (len(queries), len(base.entries), len(base)))
    for key in [("calls", "func"), ("imports", "target"), ("symbols", "name")]:
        d = picked[key]
        print("  %s.%s commonest %r appears %d times, the rare value %r once;"
              " ANALYZE reports one average for both"
              % (key[0], key[1], d["top"][0], d["top"][1], d["rare"][0]))

    rule("3. planner oracle fidelity")
    probe_vectors = [base, None, stats.flat_vector(base),
                     stats.scaled_vector(base, 0.02),
                     stats.scaled_vector(base, 0.2),
                     stats.scaled_vector(base, 5.0),
                     stats.scaled_vector(base, 100.0)]
    checked, mismatches, plan_sets = oracle.verify_matches_file(
        db, [natural[q.name] for q in queries], probe_vectors, stats)
    print("schema-only oracle vs the real database: %d (vector, query) pairs,"
          " %d mismatches, %d distinct plan sets"
          % (checked, len(mismatches), plan_sets))
    if mismatches:
        for sql, want, got in mismatches[:3]:
            print("  MISMATCH", sql)
            print("    oracle:", want)
            print("    real  :", got)
        raise SystemExit("oracle does not reproduce the real planner")
    if plan_sets < 2:
        raise SystemExit("statistics never changed a plan; nothing to search")

    rule("4. plan bank")
    bank = search.load_bank(db, queries, bank_path) if args.reuse_bank else None
    if bank is None:
        t0 = time.perf_counter()
        bank = search.PlanBank(db, queries, cap=1.0).build()
        search.save_bank(bank, bank_path)
        print("timed %d (query, plan) pairs in %.1f s"
              % (len(bank.times), time.perf_counter() - t0))
    else:
        print("reused %d measured (query, plan) pairs" % len(bank.times))
    bad = bank.answer_consistency()
    if bad:
        raise SystemExit("plans for one query disagreed on the answer: %r" % bad)
    print("every timed plan for a query returns the same answer")
    dead = [q for q, m in bank.meta.items() if m["timed"] == 0]
    if dead:
        print("queries with no plan under the deadline:", dead)

    rule("5. search over statistics")
    scorer = search.Scorer(bank)
    t0 = time.perf_counter()
    winner, restarts = search.multi_start(scorer, base)
    search_s = time.perf_counter() - t0
    forged = winner["vector"]
    print("search took %.1f s, %d planner calls, %d statistics reloads"
          % (search_s, scorer.oracle.calls, scorer.oracle.reloads))
    vec_scaled, tot_scaled, _pm, factor = search.scaled_sweep(scorer, base)
    print("best single global rescale: x%g" % factor)
    unc_hi, tot_unc_hi, _ = search.greedy_uncoupled(scorer, base, base,
                                                    verbose=False)
    unc_lo, tot_unc_lo, _ = search.greedy_uncoupled(scorer, base, base,
                                                    cheapest_first=True,
                                                    verbose=False)
    print("uncoupled greedy: %.1f ms expensive first, %.1f ms cheap first,"
          " against %.1f ms for doing nothing"
          % (tot_unc_hi * 1000, tot_unc_lo * 1000,
             scorer.total(base)[0] * 1000))
    print("unknown plans charged the deadline during the search: %d"
          % len(bank.unknown))
    print()
    print("what the search changed:")
    floor_sig = bank.oracle_best()
    for (tbl, idx, nrow, ncols), (lo, hi) in zip(base.entries, base.slices):
        a, f = base.values[lo:hi], forged.values[lo:hi]
        if a != f:
            print("  %-8s %-18s nrow %-7d ANALYZE %-14s forged %-14s"
                  " (%+d tenths of a bit)"
                  % (tbl, idx, nrow, a, f,
                     sum(stats.logest(y) - stats.logest(x)
                         for x, y in zip(a, f))))
    print("  %d of %d coordinates changed, total LogEst distance %d"
          % (winner["moved"], len(base), winner["distance"]))

    rule("6. end to end measurement")
    hint_sqls = {}
    for q in queries:
        sig = floor_sig[q.name][1]
        hint_sqls[q.name] = (bank.forced[q.name][sig] if sig
                             else natural[q.name])
    configs = [
        ("no statistics", None, natural),
        ("ANALYZE", base, natural),
        ("best global rescale", vec_scaled, natural),
        ("uncoupled greedy, expensive first", unc_hi, natural),
        ("uncoupled greedy, cheap first", unc_lo, natural),
        ("statforge", forged, natural),
        ("per-query hints (rewrites SQL)", base, hint_sqls),
    ]
    t0 = time.perf_counter()
    measured = bench.run_configs(db, queries, configs, stats, rounds=rounds,
                                 cap=args.cap)
    print("measured %d configurations over %d rounds in %.1f s"
          % (len(configs), rounds, time.perf_counter() - t0))
    refixed = bench.remeasure_censored(db, queries, configs, stats, measured,
                                       hard_cap=args.hard_cap)
    if refixed:
        print("replaced %d deadline-censored lower bounds with real timings"
              % len(refixed))

    answers = {}
    for label, _v, _s in configs:
        for q in queries:
            rec = measured[label]["per_query"][q.name]
            if not rec["censored"]:
                answers.setdefault(q.name, set()).add(rec["answer"])
    disagree = dict((n, v) for n, v in answers.items() if len(v) > 1)
    if disagree:
        raise SystemExit("configurations disagreed on an answer: %r" % disagree)
    print("every configuration returns identical answers for all %d queries"
          % len(queries))

    print()
    print("%-32s %12s %10s %9s" % ("configuration", "total", "vs ANALYZE",
                                   "censored"))
    base_total = measured["ANALYZE"]["total"]
    for label, _v, _s in configs:
        m = measured[label]
        ncens = sum(1 for r in m["per_query"].values() if r["censored"])
        print("%-32s %9.1f ms %9.2fx %9d"
              % (label, m["total"] * 1000, base_total / m["total"], ncens))
    forged_total = measured["statforge"]["total"]
    hint_total = measured["per-query hints (rewrites SQL)"]["total"]
    print()
    print("predicted from the plan bank: ANALYZE %.1f ms, statforge %.1f ms"
          % (scorer.total(base)[0] * 1000, winner["total"] * 1000))
    print("measured end to end:          ANALYZE %.1f ms, statforge %.1f ms"
          % (base_total * 1000, forged_total * 1000))

    print()
    print("%-20s %12s %12s %9s  %s" % ("query", "ANALYZE", "statforge",
                                       "change", "plan changed"))
    rows = []
    for q in queries:
        a = measured["ANALYZE"]["per_query"][q.name]
        f = measured["statforge"]["per_query"][q.name]
        rows.append((q.name, a, f))
    rows.sort(key=lambda r: -r[1]["seconds"])
    for name, a, f in rows:
        ratio = a["seconds"] / f["seconds"] if f["seconds"] else 0.0
        print("%-20s %9.2f ms %9.2f ms %8.1fx  %s"
              % (name, a["seconds"] * 1000, f["seconds"] * 1000, ratio,
                 "yes" if a["plan"] != f["plan"] else "no"))

    rule("7. artifacts")
    totals = []
    for label, _v, _s in configs:
        if label == "statforge":
            kind = "statforge"
        elif label.startswith("per-query hints"):
            kind = "rewrites_sql"
        else:
            kind = "baseline"
        totals.append((label, measured[label]["total"] * 1000.0, kind))
    per_query = [(n, a["seconds"] * 1000.0, f["seconds"] * 1000.0)
                 for n, a, f in rows]
    subtitle = ("%d rows parsed out of the local Python install, %d queries,"
                " %d indexes, %d statistics coordinates, SQLite %s,"
                " min of %d rounds, no deadline-censored values"
                % (total_rows, len(queries), len(base.entries), len(base),
                   sqlite3.sqlite_version, rounds))
    png = os.path.join(docs, "before_after%s.png" % tag)
    chart.draw(png, totals, per_query, subtitle)
    print("wrote", os.path.relpath(png, ROOT))

    payload = {
        "generated_by": "demo.py",
        "elapsed_seconds": round(time.perf_counter() - t_start, 1),
        "environment": {
            "python": platform.python_version(),
            "sqlite": sqlite3.sqlite_version,
            "platform": platform.platform(),
            "stat4_enabled": "ENABLE_STAT4" in " ".join(
                r[0] for r in sqlite3.connect(":memory:").execute(
                    "PRAGMA compile_options")),
        },
        "corpus": {"rows": counts, "total_rows": total_rows,
                   "bytes": os.path.getsize(db), "skew": skew},
        "workload": {
            "queries": [{"name": q.name, "note": q.note,
                         "tables": [t for t, _a in q.tables],
                         "sql": natural[q.name]} for q in queries],
        },
        "oracle_check": {"pairs": checked, "mismatches": len(mismatches),
                         "distinct_plan_sets": plan_sets},
        "plan_bank": {"cap_seconds": bank.cap, "per_query": bank.meta,
                      "unknown_plans_charged_cap": len(bank.unknown)},
        "search": {
            "seconds": round(search_s, 2),
            "planner_calls": scorer.oracle.calls,
            "statistics_reloads": scorer.oracle.reloads,
            "chosen_start": winner["start"],
            "coordinates": len(base),
            "coordinates_changed": winner["moved"],
            "logest_distance": winner["distance"],
            "restarts": [{"start": r["start"],
                          "predicted_ms": round(r["total"] * 1000, 2),
                          "distance": r["distance"], "moved": r["moved"]}
                         for r in restarts],
            "analyze_vector": dict(
                ("%s.%s" % (e[0], e[1]), base.values[lo:hi])
                for e, (lo, hi) in zip(base.entries, base.slices)),
            "forged_vector": dict(
                ("%s.%s" % (e[0], e[1]), forged.values[lo:hi])
                for e, (lo, hi) in zip(forged.entries, forged.slices)),
            "predicted_ms": {
                "no statistics": round(scorer.total(None)[0] * 1000, 2),
                "ANALYZE": round(scorer.total(base)[0] * 1000, 2),
                "best global rescale": round(tot_scaled * 1000, 2),
                "global rescale factor": factor,
                "uncoupled greedy expensive first":
                    round(tot_unc_hi * 1000, 2),
                "uncoupled greedy cheap first": round(tot_unc_lo * 1000, 2),
                "statforge": round(winner["total"] * 1000, 2),
            },
        },
        "measured": dict(
            (label, {
                "total_ms": round(measured[label]["total"] * 1000, 2),
                "speedup_vs_analyze": round(
                    base_total / measured[label]["total"], 3),
                "censored_queries": sorted(
                    n for n, r in measured[label]["per_query"].items()
                    if r["censored"]),
                "remeasured_without_deadline": sorted(
                    n for n, r in measured[label]["per_query"].items()
                    if r.get("remeasured")),
                "per_query_ms": dict(
                    (n, round(r["seconds"] * 1000, 3))
                    for n, r in measured[label]["per_query"].items()),
                "plans": dict((n, r["plan"])
                              for n, r in measured[label]["per_query"].items()),
            }) for label, _v, _s in configs),
        "headline": {
            "metric": "total measured time for the 15 query workload",
            "baseline_label": "ANALYZE",
            "baseline_ms": round(base_total * 1000, 2),
            "result_label": "statforge",
            "result_ms": round(forged_total * 1000, 2),
            "speedup": round(base_total / forged_total, 2),
            "per_query_hint_floor_ms": round(hint_total * 1000, 2),
            "rounds": rounds,
            "deadline_seconds": args.cap,
        },
    }
    mpath = os.path.join(docs, "metrics%s.json" % tag)
    with open(mpath, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=1, sort_keys=False)
    print("wrote", os.path.relpath(mpath, ROOT))
    stats.write_stats(db, base)  # leave the database in its honest state
    scorer.close()

    rule("headline")
    print("ANALYZE %.1f ms -> statforge %.1f ms (%.2fx) on the same SQL,"
          % (base_total * 1000, forged_total * 1000,
             base_total / forged_total))
    print("the same indexes and the same data. Only sqlite_stat1 differs.")
    print("Rewriting every query with explicit index hints reaches %.1f ms."
          % (hint_total * 1000))
    print("Total run time %.1f s." % (time.perf_counter() - t_start))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
