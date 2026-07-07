#!/usr/bin/env python3
"""
Tests for the Phase 3 sequencing engine.

Runs with pytest, or standalone with no dependencies:

    python test_sequencer.py
"""

import itertools
import random

import sequencer as sq


def _roll(lot, *segments):
    return {
        "navision_lot": lot,
        "roll_qty": 1,
        "segments": [{"color_code": c, "width_in": w} for c, w in segments],
    }


def _brute_force(matrix):
    """Reference optimum: try every ordering (only for tiny n)."""
    n = len(matrix)
    best_order, best_cost = None, float("inf")
    for perm in itertools.permutations(range(n)):
        cost = sq.path_cost(matrix, list(perm))
        if cost < best_cost:
            best_order, best_cost = list(perm), cost
    return best_order, best_cost


def _random_symmetric_matrix(n, rng, hi=200):
    m = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            v = rng.randint(0, hi)
            m[i][j] = m[j][i] = v
    return m


# --- path_cost ------------------------------------------------------------
def test_path_cost_open_path_no_wraparound():
    m = [[0, 5, 9], [5, 0, 4], [9, 4, 0]]
    # order 0,1,2 -> 5 + 4 = 9 (does NOT add the 2->0 edge)
    assert sq.path_cost(m, [0, 1, 2]) == 9


# --- exact solver ---------------------------------------------------------
def test_exact_trivial_sizes():
    assert sq.solve_exact([]) == ([], 0)
    assert sq.solve_exact([[0]]) == ([0], 0)


def test_exact_matches_brute_force_small():
    rng = random.Random(1234)
    for n in range(2, 8):
        for _ in range(20):
            m = _random_symmetric_matrix(n, rng)
            _, exact_cost = sq.solve_exact(m)
            _, bf_cost = _brute_force(m)
            assert exact_cost == bf_cost, (n, m)


def test_exact_returns_valid_permutation():
    rng = random.Random(7)
    m = _random_symmetric_matrix(6, rng)
    order, _ = sq.solve_exact(m)
    assert sorted(order) == list(range(6))


def test_exact_free_start_and_end():
    # A cheap open path threads three "chains"; the optimum is an open path
    # that need not start at node 0. 0-1 cost 1, 1-2 cost 1, everything else
    # expensive: best path is 0-1-2 (or reverse) at cost 2.
    m = [
        [0, 1, 50],
        [1, 0, 1],
        [50, 1, 0],
    ]
    order, cost = sq.solve_exact(m)
    assert cost == 2
    assert order in ([0, 1, 2], [2, 1, 0])


# --- heuristic solver -----------------------------------------------------
def test_heuristic_valid_permutation():
    rng = random.Random(99)
    m = _random_symmetric_matrix(30, rng)
    order, _ = sq.solve_heuristic(m)
    assert sorted(order) == list(range(30))


def test_heuristic_matches_exact_on_small():
    # On instances small enough to solve exactly, the heuristic should very
    # often reach the true optimum and never beat it.
    rng = random.Random(2024)
    hits = 0
    trials = 40
    for _ in range(trials):
        m = _random_symmetric_matrix(7, rng)
        _, exact_cost = sq.solve_exact(m)
        _, heur_cost = sq.solve_heuristic(m)
        assert heur_cost >= exact_cost - 1e-9
        if abs(heur_cost - exact_cost) <= 1e-9:
            hits += 1
    # Expect the heuristic to nail the optimum on the large majority.
    assert hits >= int(trials * 0.8), f"only {hits}/{trials} optimal"


# --- optimise (end to end) ------------------------------------------------
def _sample_rolls():
    return [
        _roll("L1", ("FG", 182)),
        _roll("L2", ("FG", 177), ("WHI", 5)),
        _roll("L3", ("FG", 177), ("WHI", 5)),
        _roll("L4", ("WHI", 182)),
    ]


def test_optimise_conserves_rolls():
    rolls = _sample_rolls()
    result = sq.optimise(rolls)
    assert result["roll_count"] == len(rolls)
    assert sorted(r["navision_lot"] for r in result["sequence"]) == \
        ["L1", "L2", "L3", "L4"]


def test_optimise_uses_exact_for_small_orders():
    result = sq.optimise(_sample_rolls())
    assert result["method"] == "held-karp"
    assert result["optimal"] is True


def test_optimise_orders_by_cost():
    # Distances: FG(0)-FGWHI(1)=5, FG(0)-WHI(2)=182, FGWHI(1)-WHI(2)=177.
    # Best open path over the 3 distinct layouts is 2-1-0 (or 0-1-2):
    # 177 + 5 = 182, versus e.g. 0-2-1 = 182 + 177 = 359.
    result = sq.optimise(_sample_rolls())
    assert result["cost"] == 182
    assert result["layout_order"] in ([0, 1, 2], [2, 1, 0])


def test_optimise_never_worse_than_as_extracted():
    from roll_sequencing import sequence_cost
    rolls = _sample_rolls()
    result = sq.optimise(rolls)
    assert result["cost"] <= sequence_cost(rolls) + 1e-9


def test_optimise_places_identical_layouts_adjacent():
    # The two FG+WHI rolls (L2, L3) share a layout, so they must end up next
    # to each other in the expanded sequence (zero-cost transition between).
    result = sq.optimise(_sample_rolls())
    lots = [r["navision_lot"] for r in result["sequence"]]
    assert abs(lots.index("L2") - lots.index("L3")) == 1


def test_optimise_single_and_empty():
    empty = sq.optimise([])
    assert empty["sequence"] == [] and empty["cost"] == 0
    one = sq.optimise([_roll("only", ("FG", 182))])
    assert one["roll_count"] == 1 and one["cost"] == 0


def test_optimise_heuristic_path_for_large_orders():
    # Force the heuristic branch with a low exact threshold and many distinct
    # layouts; result must still be a conserving, valid sequence.
    rolls = [_roll(f"L{i}", ("FG", 182 - i), ("WHI", i)) for i in range(1, 12)]
    result = sq.optimise(rolls, exact_max_layouts=5)
    assert result["optimal"] is False
    assert result["method"].startswith("nearest-neighbour")
    assert sorted(r["navision_lot"] for r in result["sequence"]) == \
        sorted(r["navision_lot"] for r in rolls)


def test_optimise_matches_exact_when_forced_heuristic_small():
    # Same small order, once exact and once forced heuristic: costs must agree
    # (the heuristic reaches the optimum on an instance this small).
    rolls = _sample_rolls()
    exact = sq.optimise(rolls, exact_max_layouts=100)
    heur = sq.optimise(rolls, exact_max_layouts=1)
    assert heur["cost"] == exact["cost"]


# --- Or-opt incremental delta ----------------------------------------------
def test_or_opt_incremental_matches_full_recompute():
    # The incremental O(1) delta accepts exactly the same moves as the old
    # full-recompute form, so on any instance the two must return an
    # *identical* order — not merely one of equal cost.
    def _or_opt_reference(order, matrix):
        order = list(order)
        n = len(order)
        improved = True
        while improved:
            improved = False
            for seg_len in (1, 2, 3):
                if seg_len >= n:
                    continue
                for i in range(0, n - seg_len + 1):
                    segment = order[i:i + seg_len]
                    rest = order[:i] + order[i + seg_len:]
                    for pos in range(len(rest) + 1):
                        if pos == i:
                            continue
                        candidate = rest[:pos] + segment + rest[pos:]
                        if sq.path_cost(matrix, candidate) + 1e-9 < sq.path_cost(matrix, order):
                            order = candidate
                            improved = True
                            break
                    if improved:
                        break
                if improved:
                    break
        return order

    rng = random.Random(4242)
    for _ in range(30):
        n = rng.randint(2, 9)
        m = _random_symmetric_matrix(n, rng)
        identity = list(range(n))
        shuffled = identity[:]
        rng.shuffle(shuffled)
        for start in (identity, shuffled):
            got = sq._or_opt(list(start), m)
            want = _or_opt_reference(start, m)
            assert got == want, (n, start, m)


# --- choose_cuts ------------------------------------------------------------
def test_choose_cuts_k_picks_largest_with_earlier_tie():
    # k schedules need k-1 cuts at the largest costs; the 9-vs-9 tie breaks
    # toward the earlier transition so the result is deterministic.
    assert sq.choose_cuts([5, 9, 9, 3], k=2) == [1]
    assert sq.choose_cuts([5, 9, 9, 3], k=3) == [1, 2]
    assert sq.choose_cuts([5, 9, 9, 3], k=4) == [0, 1, 2]


def test_choose_cuts_k_one_means_no_cuts():
    assert sq.choose_cuts([5, 9, 3], k=1) == []


def test_choose_cuts_k_clamps_to_one_schedule_per_roll():
    # k beyond the sequence length just cuts every transition, no error.
    assert sq.choose_cuts([5, 9, 3], k=99) == [0, 1, 2]


def test_choose_cuts_threshold_is_strict():
    costs = [5, 9, 0, 9]
    assert sq.choose_cuts(costs, threshold=8) == [1, 3]
    # cost == threshold is NOT cut — "strictly exceeds".
    assert sq.choose_cuts(costs, threshold=9) == []
    # threshold 0 cuts every positive transition but not the zero one.
    assert sq.choose_cuts(costs, threshold=0) == [0, 1, 3]


def test_choose_cuts_argument_validation():
    for bad_call in (lambda: sq.choose_cuts([1, 2], k=2, threshold=1),
                     lambda: sq.choose_cuts([1, 2]),
                     lambda: sq.choose_cuts([1, 2], k=0)):
        try:
            bad_call()
        except ValueError:
            pass
        else:
            assert False, "expected ValueError"


# --- split_guidance ---------------------------------------------------------
def test_split_guidance_rows():
    rows = sq.split_guidance([5, 0, 77, 3])
    # Default max_k = positive transitions + 1 = 4.
    assert [r["k"] for r in rows] == [1, 2, 3, 4]
    # Row k's total is sum(costs) minus the k-1 largest costs...
    assert [r["total_cost_in"] for r in rows] == [85, 8, 3, 0]
    # ...and the marginal savings are the costs in descending order.
    assert [r["marginal_saving_in"] for r in rows] == [77, 5, 3, 0]


def test_split_guidance_max_k_clamps_and_pads_with_zero():
    rows = sq.split_guidance([5, 0, 77, 3], max_k=99)
    # Clamped to len(costs) + 1 = 5; savings hit 0 once costs are exhausted.
    assert [r["k"] for r in rows] == [1, 2, 3, 4, 5]
    assert [r["marginal_saving_in"] for r in rows] == [77, 5, 3, 0, 0]
    assert rows[-1]["total_cost_in"] == 0


def test_split_guidance_consistent_with_choose_cuts():
    # Guidance row k must equal what actually cutting with choose_cuts(k)
    # achieves: the summed per-schedule costs — one story, two functions.
    rng = random.Random(42)
    for _ in range(25):
        n = rng.randint(1, 12)
        costs = [rng.choice([0, rng.randint(1, 50)]) for _ in range(n)]
        rows = sq.split_guidance(costs, max_k=n + 1)
        totals = [r["total_cost_in"] for r in rows]
        assert totals == sorted(totals, reverse=True)  # non-increasing
        for row in rows:
            cuts = sq.choose_cuts(costs, k=row["k"])
            boundaries = [-1] + cuts + [n]
            per_schedule = [sum(costs[a + 1:b])
                            for a, b in zip(boundaries, boundaries[1:])]
            assert sum(per_schedule) == row["total_cost_in"], (costs, row)


def _run_standalone():
    tests = [v for name, v in sorted(globals().items())
             if name.startswith("test_") and callable(v)]
    failures = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except AssertionError as exc:
            failures += 1
            print(f"  FAIL  {test.__name__}: {exc}")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  ERROR {test.__name__}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    import sys
    sys.exit(_run_standalone())
