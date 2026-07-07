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
