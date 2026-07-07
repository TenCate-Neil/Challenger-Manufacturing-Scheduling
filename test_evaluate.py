#!/usr/bin/env python3
"""
Tests for the Phase 4 evaluation and reporting layer.

Runs with pytest, or standalone with no dependencies:

    python test_evaluate.py
"""

import itertools
import json
import random

import evaluate as ev
from roll_sequencing import join_orders, sequence_cost
from sequencer import optimise


def _roll(lot, *segments, sort=None, roll_qty=1, lf=100, sf=1500):
    return {
        "navision_lot": lot,
        "sort": sort,
        "roll_qty": roll_qty,
        "mfg_roll_length_lf": lf,
        "total_mfg_sf": sf,
        "layout_signature": "|".join(f"{w}{c}" for c, w in segments),
        "layout_group": None,
        "segments": [{"color_code": c, "width_in": w} for c, w in segments],
    }


def _sample_rolls():
    # Four rolls, three distinct layouts (L2 and L3 share a layout).
    return [
        _roll("L1", ("FG", 182), sort=1),
        _roll("L2", ("FG", 177), ("WHI", 5), sort=2),
        _roll("L3", ("FG", 177), ("WHI", 5), sort=3),
        _roll("L4", ("FG", 100), ("WHI", 82), sort=4),
    ]


def _brute_force_cost(matrix):
    n = len(matrix)
    best = float("inf")
    for perm in itertools.permutations(range(n)):
        best = min(best, ev.path_cost(matrix, list(perm)))
    return best


def _random_symmetric_matrix(n, rng, hi=200):
    m = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            m[i][j] = m[j][i] = rng.randint(0, hi)
    return m


# --- conservation ---------------------------------------------------------
def test_conservation_passes_for_optimise_output():
    rolls = _sample_rolls()
    result = optimise(rolls)
    cons = ev.check_conservation(rolls, result["sequence"])
    assert cons["passed"], cons["discrepancies"]
    assert cons["checks"]["roll_count"]["match"]
    assert cons["checks"]["linear_feet_lf"]["original"] == 400
    assert cons["checks"]["linear_feet_lf"]["sequence"] == 400


def test_conservation_detects_dropped_roll():
    rolls = _sample_rolls()
    cons = ev.check_conservation(rolls, rolls[:-1])  # one roll missing
    assert not cons["passed"]
    assert any("roll count" in d for d in cons["discrepancies"])


def test_conservation_detects_changed_quantity():
    rolls = _sample_rolls()
    tampered = [dict(r) for r in rolls]
    tampered[0] = dict(tampered[0], roll_qty=99)
    cons = ev.check_conservation(rolls, tampered)
    assert not cons["passed"]
    assert any("physical_roll_qty" in d for d in cons["discrepancies"])


def test_conservation_tolerates_float_rounding_in_totals():
    # Summing the same square footage in a different order can differ in the
    # last floating-point bit; a reorder that preserves the rolls must still
    # pass. {0.1, 0.2, 0.3} reproduces the associativity difference:
    # (0.1 + 0.2) + 0.3 == 0.6000000000000001, but (0.3 + 0.2) + 0.1 == 0.6.
    rolls = [_roll("A", ("BLU", 100), sf=0.1),
             _roll("B", ("RED", 100), sf=0.2),
             _roll("C", ("GRN", 100), sf=0.3)]
    reordered = [rolls[2], rolls[1], rolls[0]]
    orig_sum = ev._sum_field(rolls, "total_mfg_sf")
    seq_sum = ev._sum_field(reordered, "total_mfg_sf")
    assert orig_sum != seq_sum  # raw sums differ by a floating-point bit
    cons = ev.check_conservation(rolls, reordered)
    assert cons["passed"], cons["discrepancies"]
    assert cons["checks"]["square_feet_sf"]["match"]


def test_conservation_detects_altered_layout():
    rolls = _sample_rolls()
    tampered = [dict(r) for r in rolls]
    # Change one roll's threading — a reorder must never do this.
    tampered[0] = dict(tampered[0],
                       segments=[{"color_code": "WHI", "width_in": 182}])
    cons = ev.check_conservation(rolls, tampered)
    assert not cons["passed"]
    assert any("layout" in d for d in cons["discrepancies"])


# --- lower bound ----------------------------------------------------------
def test_lower_bound_trivial_sizes():
    assert ev.lower_bound([]) == 0
    assert ev.lower_bound([[0]]) == 0
    # Two nodes: the single edge is the whole spanning tree.
    assert ev.lower_bound([[0, 7], [7, 0]]) == 7


def test_lower_bound_never_exceeds_optimum():
    rng = random.Random(2024)
    for n in range(2, 8):
        for _ in range(30):
            m = _random_symmetric_matrix(n, rng)
            lb = ev.lower_bound(m)
            opt = _brute_force_cost(m)
            assert lb <= opt + 1e-9, (lb, opt, m)


# --- solution quality -----------------------------------------------------
def test_quality_gap_zero_when_proven_optimal():
    rolls = _sample_rolls()
    result = optimise(rolls)  # small -> exact
    q = ev.solution_quality(result["distance_matrix"], result["cost"],
                            result["optimal"])
    assert q["proven_optimal"] is True
    assert q["gap_to_exact_optimum_in"] == 0
    assert q["exact_optimum_in"] == result["cost"]
    assert q["gap_to_lower_bound_in"] >= 0


def test_quality_oracle_reports_true_gap_for_heuristic():
    # Force the heuristic branch on a small instance so the oracle can still
    # prove the optimum and report the true (here zero) gap.
    rolls = _sample_rolls()
    result = optimise(rolls, exact_max_layouts=1)
    assert result["optimal"] is False
    q = ev.solution_quality(result["distance_matrix"], result["cost"],
                            result["optimal"], oracle_max_layouts=16)
    assert q["exact_optimum_in"] is not None
    assert q["exact_optimum_source"] == "held-karp (oracle)"
    assert q["gap_to_exact_optimum_in"] == result["cost"] - q["exact_optimum_in"]
    # achieved >= exact optimum >= lower bound
    assert q["achieved_cost_in"] >= q["exact_optimum_in"] >= q["lower_bound_in"]


# --- full report ----------------------------------------------------------
def test_evaluate_report_structure_and_consistency():
    rolls = _sample_rolls()
    report = ev.evaluate(rolls)

    assert report["conservation"]["passed"]
    assert report["roll_count"] == 4
    assert report["distinct_layout_count"] == 3

    # The per-transition change costs in the sequence view must sum to the
    # achieved cost.
    view = report["manufacturing_sequence"]
    assert sum(entry["change_cost_in"] for entry in view) == report["achieved_cost_in"]
    assert view[0]["change_cost_in"] == 0  # fresh start


def test_evaluate_sequence_view_matches_positions():
    report = ev.evaluate(_sample_rolls())
    view = report["manufacturing_sequence"]
    assert [e["position"] for e in view] == list(range(1, len(view) + 1))
    assert len(view) == report["roll_count"]


def test_report_json_is_serialisable():
    report = ev.evaluate(_sample_rolls())
    text = ev.report_json(report)
    reparsed = json.loads(text)
    assert reparsed["achieved_cost_in"] == report["achieved_cost_in"]
    assert reparsed["conservation"]["passed"] is True


def test_evaluate_empty_and_single():
    empty = ev.evaluate([])
    assert empty["roll_count"] == 0
    assert empty["achieved_cost_in"] == 0
    assert empty["conservation"]["passed"]

    one = ev.evaluate([_roll("only", ("FG", 182))])
    assert one["roll_count"] == 1
    assert one["achieved_cost_in"] == 0
    assert one["conservation"]["passed"]
    assert one["transition_breakdown"]["transition_count"] == 0


def test_evaluate_achieved_cost_matches_scored_sequence():
    # Build a larger, heuristic order and confirm the reported cost equals the
    # directly scored cost of the emitted sequence (conservation of cost).
    rolls = [_roll(f"L{i}", ("FG", 182 - i), ("WHI", i), sort=i)
             for i in range(1, 14)]
    report = ev.evaluate(rolls, exact_max_layouts=5)
    # Rebuild the sequence order from the view and score it.
    lots = [e["navision_lot"] for e in report["manufacturing_sequence"]]
    by_lot = {r["navision_lot"]: r for r in rolls}
    ordered = [by_lot[lot] for lot in lots]
    assert sequence_cost(ordered) == report["achieved_cost_in"]
    assert report["conservation"]["passed"]


def test_evaluate_cross_checks_mfg_summary():
    rolls = _sample_rolls()  # linear feet total 400, square feet 6000
    extraction = {
        "source_file": "SAMPLE.xlsx",
        "mfg_summary": {"mfg_lf": 999, "mfg_sf": 6000},
    }
    warnings = []
    report = ev.evaluate(rolls, extraction=extraction, warnings=warnings)
    assert report["source_file"] == "SAMPLE.xlsx"
    # The bogus linear-feet total should surface a warning.
    assert any("linear feet" in w for w in report["warnings"])


# --- combined orders (join_orders -> evaluate) ------------------------------
def _extraction(name, rolls):
    """Extraction-result dict whose MFG summary matches its rolls exactly, so
    the cross-check has a correct second source to compare against."""
    return {
        "source_file": name,
        "rolls": rolls,
        "mfg_summary": {
            "mfg_rolls": sum(r["roll_qty"] for r in rolls),
            "mfg_lf": sum(r["mfg_roll_length_lf"] for r in rolls),
            "mfg_sf": sum(r["total_mfg_sf"] for r in rolls),
        },
    }


def test_combined_orders_conserve_and_cross_check_cleanly():
    # Two files joined into one order: conservation must hold over the union,
    # and the summed MFG summary must agree with the summed roll rows, so no
    # cross-check warning fires.
    ext_a = _extraction("A.xlsx", [_roll("A1", ("FG", 182), sort=1),
                                   _roll("A2", ("FG", 177), ("WHI", 5), sort=2)])
    ext_b = _extraction("B.xlsx", [_roll("B1", ("FG", 100), ("WHI", 82), sort=1),
                                   _roll("B2", ("WHI", 182), sort=2)])
    combined = join_orders([ext_a, ext_b])
    report = ev.evaluate(combined["rolls"], extraction=combined)
    assert report["conservation"]["passed"], \
        report["conservation"]["discrepancies"]
    assert "A.xlsx" in report["source_file"]
    assert "B.xlsx" in report["source_file"]
    assert not any("MFG summary" in w for w in report["warnings"])


def test_combined_cross_check_catches_wrong_file_total():
    # One file's stated linear-feet total is wrong, so the combined stated
    # total disagrees with the combined roll rows -> warning.
    ext_a = _extraction("A.xlsx", [_roll("A1", ("FG", 182), sort=1)])
    ext_b = _extraction("B.xlsx", [_roll("B1", ("WHI", 182), sort=1)])
    ext_b["mfg_summary"]["mfg_lf"] = 55  # rolls actually total 100
    combined = join_orders([ext_a, ext_b])
    report = ev.evaluate(combined["rolls"], extraction=combined)
    assert any("linear feet" in w for w in report["warnings"])


def test_combined_evaluation_has_no_file_local_layout_group_warning():
    # The extractor numbers layout_group per file, so in a combined order the
    # same id legitimately names two different layouts. join_orders clears the
    # file-local ids, so the Phase 2 extractor-consistency warning ("spans
    # distinct layouts") must not fire on a clean two-file join.
    ext_a = _extraction("A.xlsx", [_roll("A1", ("FG", 182), sort=1)])
    ext_b = _extraction("B.xlsx", [_roll("B1", ("WHI", 182), sort=1)])
    for extraction in (ext_a, ext_b):
        for roll in extraction["rolls"]:
            roll["layout_group"] = 3  # same file-local id, different layouts
    combined = join_orders([ext_a, ext_b])
    report = ev.evaluate(combined["rolls"], extraction=combined)
    assert not any("layout_group" in w for w in report["warnings"]), \
        report["warnings"]
    assert report["conservation"]["passed"]


def test_cross_check_catches_wrong_physical_roll_count():
    # The extractor's stated roll count disagrees with the summed roll_qty.
    rolls = _sample_rolls()  # roll_qty sums to 4
    extraction = {"source_file": "S.xlsx", "mfg_summary": {"mfg_rolls": 7}}
    report = ev.evaluate(rolls, extraction=extraction)
    assert any("physical roll" in w for w in report["warnings"])


# --- split_report -----------------------------------------------------------
def test_split_report_k2_cuts_most_expensive_transition():
    report = ev.evaluate(_sample_rolls())  # 3 distinct layouts
    view = report["manufacturing_sequence"]
    costs = [e["change_cost_in"] for e in view[1:]]
    split = ev.split_report(report, k=2)

    assert split["schedule_count"] == 2
    assert len(split["schedules"]) == 2
    assert split["cut_transition_costs"] == [max(costs)]
    assert split["cut_after_positions"] == [costs.index(max(costs)) + 1]
    assert split["total_saving_in"] == max(costs)
    assert split["cost_before_in"] == report["achieved_cost_in"]
    assert split["cost_after_in"] == report["achieved_cost_in"] - max(costs)

    for schedule in split["schedules"]:
        seq = schedule["manufacturing_sequence"]
        assert seq[0]["change_cost_in"] == 0  # fresh start per schedule
        assert [e["position"] for e in seq] == list(range(1, len(seq) + 1))
    assert sum(s["achieved_cost_in"] for s in split["schedules"]) == \
        split["cost_after_in"]

    # Nothing is lost or duplicated at the cut.
    split_lots = [e["navision_lot"] for s in split["schedules"]
                  for e in s["manufacturing_sequence"]]
    assert sorted(split_lots) == sorted(e["navision_lot"] for e in view)


def test_split_report_threshold_above_max_is_single_schedule():
    report = ev.evaluate(_sample_rolls())
    costs = [e["change_cost_in"] for e in report["manufacturing_sequence"][1:]]
    split = ev.split_report(report, threshold=max(costs) + 1)
    assert split["schedule_count"] == 1
    assert split["cut_after_positions"] == []
    assert split["total_saving_in"] == 0
    assert split["cost_after_in"] == report["achieved_cost_in"]
    assert split["schedules"][0]["roll_count"] == report["roll_count"]


def test_split_report_threshold_zero_cuts_every_positive_transition():
    report = ev.evaluate(_sample_rolls())
    costs = [e["change_cost_in"] for e in report["manufacturing_sequence"][1:]]
    positive = [c for c in costs if c > 0]
    split = ev.split_report(report, threshold=0)
    assert split["schedule_count"] == len(positive) + 1
    assert split["total_saving_in"] == sum(positive)
    # Only zero-cost transitions remain inside the schedules.
    assert split["cost_after_in"] == 0
    assert all(s["achieved_cost_in"] == 0 for s in split["schedules"])


def test_split_report_k1_is_identity():
    report = ev.evaluate(_sample_rolls())
    split = ev.split_report(report, k=1)
    assert split["schedule_count"] == 1
    assert split["cut_after_positions"] == []
    assert split["total_saving_in"] == 0
    only = split["schedules"][0]
    assert only["roll_count"] == report["roll_count"]
    assert only["achieved_cost_in"] == report["achieved_cost_in"]
    assert only["manufacturing_sequence"] == report["manufacturing_sequence"]


def test_split_report_rejects_both_k_and_threshold():
    report = ev.evaluate(_sample_rolls())
    try:
        ev.split_report(report, k=2, threshold=5)
    except ValueError:
        pass
    else:
        assert False, "expected ValueError"


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
