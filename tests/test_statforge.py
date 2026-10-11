"""Tests for statforge.

The two tests that matter most are oracle fidelity and plan equivalence:
everything downstream is meaningless if the schema-only planner disagrees with
the real one, or if a forced plan computes a different answer.
"""

import os
import random
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from statforge import bench, oracle, plans, search, stats
from statforge.workload import Query

FIX = None


def build_fixture(path, n_left=4000, n_right=12000, seed=11):
    con = sqlite3.connect(path)
    con.executescript(
        "CREATE TABLE lft(id INTEGER PRIMARY KEY, grp TEXT NOT NULL,"
        " n INTEGER NOT NULL);"
        "CREATE TABLE rgt(id INTEGER PRIMARY KEY, lid INTEGER NOT NULL,"
        " tag TEXT NOT NULL, m INTEGER NOT NULL);")
    rng = random.Random(seed)
    groups = ["g%d" % i for i in range(20)]
    con.executemany("INSERT INTO lft(id, grp, n) VALUES(?,?,?)",
                    [(i, rng.choice(groups), rng.randrange(500))
                     for i in range(n_left)])
    # Deliberately skewed: one tag dominates, many appear once.
    tags = ["hot"] * (n_right // 2) + ["t%d" % i for i in range(n_right // 2)]
    rng.shuffle(tags)
    con.executemany("INSERT INTO rgt(id, lid, tag, m) VALUES(?,?,?,?)",
                    [(i, rng.randrange(n_left), tags[i], rng.randrange(500))
                     for i in range(n_right)])
    con.executescript(
        "CREATE INDEX ix_l_grp ON lft(grp);"
        "CREATE INDEX ix_l_n ON lft(n);"
        "CREATE INDEX ix_r_lid ON rgt(lid);"
        "CREATE INDEX ix_r_tag ON rgt(tag);")
    con.commit()
    con.close()
    return path


def fixture():
    global FIX
    if FIX is None:
        d = tempfile.mkdtemp(prefix="statforge_test_")
        FIX = build_fixture(os.path.join(d, "fix.db"))
    return FIX


def hot_query():
    return Query("hot", "count(*)", [("rgt", "r"), ("lft", "l")],
                 "r.lid = l.id AND r.tag = 'hot' AND l.grp = 'g3'")


def rare_query():
    return Query("rare", "count(*)", [("rgt", "r"), ("lft", "l")],
                 "r.lid = l.id AND r.tag = 't7' AND l.grp = 'g3'")


class TestLogEst(unittest.TestCase):

    def test_logest_is_tenths_of_a_bit(self):
        self.assertEqual(stats.logest(1), 0)
        self.assertEqual(stats.logest(2), 10)
        self.assertEqual(stats.logest(1024), 100)

    def test_grid_values_are_distinct_under_quantisation(self):
        grid = stats.logest_grid(100000)
        seen = [stats.logest(v) for v in grid[:-1]]
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(grid[0], 1)
        self.assertEqual(grid[-1], 100000)
        self.assertTrue(all(a < b for a, b in zip(grid, grid[1:])))

    def test_grid_is_small_because_the_representation_is_coarse(self):
        # Roughly 10*log2(n) distinct values, not n of them. This is why an
        # exhaustive sweep per coordinate is affordable at all.
        self.assertLess(len(stats.logest_grid(400000)), 220)


class TestStatVector(unittest.TestCase):

    def test_round_trip_and_distance(self):
        base = stats.analyze_vector(fixture())
        self.assertEqual(base.distance(base), 0)
        rows = base.to_rows()
        self.assertEqual(len(rows), len(base.entries))
        for (tbl, idx, nrow, ncols), (t2, i2, text) in zip(base.entries, rows):
            self.assertEqual((tbl, idx), (t2, i2))
            self.assertEqual(len(text.split()), 1 + ncols)
            self.assertEqual(int(text.split()[0]), nrow)

    def test_distance_counts_factors_of_two(self):
        base = stats.analyze_vector(fixture())
        other = base.copy()
        other.values[0] = max(1, base.values[0]) * 4
        self.assertEqual(other.distance(base), 20)

    def test_flat_vector_claims_every_index_is_useless(self):
        base = stats.analyze_vector(fixture())
        flat = stats.flat_vector(base)
        for (tbl, idx, nrow, ncols), (lo, hi) in zip(flat.entries, flat.slices):
            for v in flat.values[lo:hi]:
                self.assertEqual(v, nrow)


class TestOracleFidelity(unittest.TestCase):
    """The schema-only oracle must be the real planner, not an approximation."""

    def setUp(self):
        self.db = fixture()
        self.base = stats.analyze_vector(self.db)
        self.sqls = [plans.natural_sql(hot_query()),
                     plans.natural_sql(rare_query())]

    def test_agrees_with_the_file_database_over_several_vectors(self):
        vectors = [self.base, stats.flat_vector(self.base),
                   stats.scaled_vector(self.base, 0.01),
                   stats.scaled_vector(self.base, 50.0), None]
        checked, bad, sets = oracle.verify_matches_file(
            self.db, self.sqls, vectors, stats)
        self.assertEqual(bad, [], "oracle disagreed with the real planner")
        self.assertEqual(checked, len(vectors) * len(self.sqls))
        self.assertGreater(sets, 1, "no vector moved the real planner either,"
                                    " so agreement proves nothing")

    def test_statistics_changes_are_actually_observed(self):
        """Guards the stale prepared statement trap.

        A connection that hands back a cached statement keeps the plan it was
        compiled with, so the oracle silently reports the same plan for every
        vector. This asserts at least one vector moves the plan, which is the
        cheapest possible detector for that failure.
        """
        orc = oracle.PlannerOracle(self.db)
        seen = set()
        for vec in (self.base, stats.flat_vector(self.base),
                    stats.scaled_vector(self.base, 0.0), None, self.base):
            seen.add(orc.plans(vec, self.sqls))
        orc.close()
        self.assertGreater(len(seen), 1,
                           "no statistics vector changed any plan")

    def test_set_vector_is_idempotent(self):
        orc = oracle.PlannerOracle(self.db)
        a = orc.plans(self.base, self.sqls)
        b = orc.plans(self.base, self.sqls)
        orc.set_vector(stats.flat_vector(self.base))
        c = orc.plans(self.base, self.sqls)
        orc.close()
        self.assertEqual(a, b)
        self.assertEqual(a, c)


class TestForcedPlans(unittest.TestCase):

    def test_every_forced_plan_returns_the_same_answer(self):
        db = fixture()
        stats.write_stats(db, stats.analyze_vector(db))
        q = hot_query()
        found, meta = plans.enumerate_forced(db, q)
        self.assertGreater(meta["distinct"], 3)
        con = bench.connect(db)
        truth = con.execute(plans.natural_sql(q)).fetchone()[0]
        for sig, sql in found.items():
            got, _ = bench.timed(con, sql, 30.0)
            self.assertIsNotNone(got, "a forced plan did not finish: " + sig)
            self.assertEqual(con.execute(sql).fetchone()[0], truth, sig)
        con.close()

    def test_forcing_actually_changes_the_plan(self):
        db = fixture()
        q = hot_query()
        found, _ = plans.enumerate_forced(db, q)
        orders = set()
        for sig in found:
            orders.add(sig.split(";")[0].strip()[:40])
        self.assertGreater(len(orders), 1,
                           "CROSS JOIN did not pin a different outer table")

    def test_index_hints_are_validated_by_sqlite_not_by_us(self):
        """The enumeration relies on SQLite rejecting hints it cannot honour.

        Note what it does NOT reject: INDEXED BY on a column the query never
        constrains is accepted, because a full scan of the index is still a
        use of it. So rejection cannot be used to prune candidates, and the
        enumeration prunes on the leading column itself instead.
        """
        db = fixture()
        con = sqlite3.connect(db)
        with self.assertRaises(sqlite3.OperationalError):
            list(con.execute("EXPLAIN QUERY PLAN SELECT count(*) FROM lft"
                             " INDEXED BY nonexistent WHERE grp = 'g3'"))
        unconstrained = hot_query().sql(order=(0, 1),
                                        hints={"r": "ix_r_tag", "l": "ix_l_n"})
        list(con.execute("EXPLAIN QUERY PLAN " + unconstrained))
        con.close()


class TestDeadline(unittest.TestCase):

    def test_the_deadline_aborts_a_long_statement_and_leaves_short_ones(self):
        """The progress handler must cut a running statement off mid flight.

        A full cartesian product of the fixture is 48 million rows, which takes
        about half a second here, so a 50 ms deadline has to interrupt it. The
        same query without a deadline must still complete and return the right
        count, which proves the handler is not simply breaking the connection.
        """
        db = fixture()
        heavy = "SELECT count(*) FROM rgt, lft"
        con = bench.connect(db)
        dt_capped, val_capped = bench.timed(con, heavy, 0.05)
        dt_free, val_free = bench.timed(con, heavy, 60.0)
        light, val_light = bench.timed(
            con, "SELECT count(*) FROM lft WHERE grp = 'g3'", 60.0)
        con.close()
        self.assertIsNone(dt_capped, "the deadline did not abort the product")
        self.assertIsNone(val_capped)
        self.assertIsNotNone(dt_free)
        self.assertEqual(val_free, 4000 * 12000)
        self.assertIsNotNone(light)

    def test_censoring_does_not_lose_the_fast_plans(self):
        db = fixture()
        q = hot_query()
        found, _ = plans.enumerate_forced(db, q)
        times, values = bench.measure_variants(db, found, rounds=2, cap=0.25)
        live = [t for t in times.values() if t is not None]
        self.assertGreater(len(live), 0)
        self.assertEqual(len(set(v for v in values.values()
                                 if v is not None)), 1)


def small_bank():
    db = fixture()
    qs = [hot_query(), rare_query()]
    return search.PlanBank(db, qs, cap=0.3).build(verbose=False), qs


class TestScorer(unittest.TestCase):

    def setUp(self):
        self.bank, self.qs = small_bank()
        self.sc = search.Scorer(self.bank)
        self.base = stats.analyze_vector(self.bank.db_path)

    def tearDown(self):
        self.sc.close()

    def test_score_depends_on_the_vector_and_is_reproducible(self):
        """Regression test for statistics that silently never take effect.

        Before the oracle was rewritten, every vector after the second scored
        identically because SQLite had bound the first statistics it read and
        never reloaded. Both halves matter: totals must differ across vectors,
        and re-scoring a vector must return what it returned the first time.
        """
        flat = stats.flat_vector(self.base)
        ones = stats.scaled_vector(self.base, 0.0)
        first = [self.sc.total(v)[0] for v in (self.base, flat, ones)]
        again = [self.sc.total(v)[0] for v in (self.base, flat, ones)]
        self.assertEqual(first, again, "scoring is order dependent")
        self.assertGreater(len(set(first)), 1,
                           "no vector changed the workload cost")

    def test_partial_agrees_with_total(self):
        pm = self.sc.plan_map(self.base)
        sub = self.sc.partial(self.base, self.sc.names)
        self.assertEqual(pm, sub)

    def test_censored_plans_are_charged_the_cap(self):
        q = self.qs[0]
        for sig, secs in list(self.bank.times.items()):
            if secs is None:
                self.assertEqual(self.bank.cost(sig[0], sig[1]), self.bank.cap)
                return
        self.skipTest("no plan was censored on this machine")

    def test_unknown_plans_are_charged_the_cap_and_counted(self):
        before = len(self.bank.unknown)
        self.assertEqual(self.bank.cost(self.qs[0].name, "not a real plan"),
                         self.bank.cap)
        self.assertEqual(len(self.bank.unknown), before + 1)


class TestSearch(unittest.TestCase):

    def setUp(self):
        self.bank, self.qs = small_bank()
        self.sc = search.Scorer(self.bank)
        self.base = stats.analyze_vector(self.bank.db_path)

    def tearDown(self):
        self.sc.close()

    def test_descent_never_returns_worse_than_its_start(self):
        start_total = self.sc.total(self.base)[0]
        vec, total, pm, trail = search.coordinate_descent(
            self.sc, self.base, self.base, verbose=False)
        self.assertLessEqual(total, start_total + 1e-9)
        self.assertTrue(all(b <= a + 1e-9 for a, b in zip(trail, trail[1:])),
                        "the descent trail is not monotone: %r" % (trail,))

    def test_descent_result_survives_rescoring(self):
        vec, total, pm, _ = search.coordinate_descent(
            self.sc, self.base, self.base, verbose=False)
        self.assertAlmostEqual(self.sc.total(vec)[0], total, places=9)

    def test_floor_is_a_lower_bound_on_every_configuration(self):
        ob = self.bank.oracle_best()
        floor = sum(ob[n][0] for n in self.sc.names)
        for vec in (self.base, None, stats.flat_vector(self.base)):
            self.assertGreaterEqual(self.sc.total(vec)[0], floor - 1e-9)

    def test_bank_round_trips_through_disk(self):
        d = tempfile.mkdtemp(prefix="statforge_bank_")
        path = os.path.join(d, "bank.json")
        search.save_bank(self.bank, path)
        back = search.load_bank(self.bank.db_path, self.qs, path)
        self.assertIsNotNone(back)
        self.assertEqual(back.times, self.bank.times)
        self.assertEqual(back.cap, self.bank.cap)


if __name__ == "__main__":
    unittest.main(verbosity=2)
