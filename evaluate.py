#!/usr/bin/env python3
"""
Roll sequencing — evaluation and reporting (Phase 4).

This is Phase 4 of docs/optimisation_plan.md (sections 6 and 9): take the
optimised sequence produced by Phase 3 and report, in the terms the plan asks
for, whether it can be trusted and how good it is. It adds no new sequencing
logic — it measures and explains the result of `sequencer.optimise`.

The plan (section 6) asks for four things, and this module produces each:

  1. Conservation. The set of rolls, their quantities, and the linear/square
     foot totals in the optimised sequence must exactly match the order as
     extracted — nothing added, dropped, or altered, and no roll's own layout
     changed. `check_conservation` verifies this and reports any discrepancy.
  2. Achieved cost. The total setup change cost (inches re-threaded) of the
     optimised sequence, in absolute terms — carried straight from Phase 3.
  3. Solution quality. For orders solved exactly the result is the proven
     minimum (gap 0). For orders solved heuristically we report the gap to a
     computed lower bound (`lower_bound`, the minimum spanning tree weight of
     the distance graph — a valid lower bound because any Hamiltonian path is
     itself a spanning tree, so it cannot cost less than the cheapest one).
     Where the instance is small enough we also solve it exactly as an oracle
     and report the true gap.
  4. Transition breakdown. How many transitions are zero-cost (identical
     consecutive rolls) and the distribution of the rest — reused from Phase 1.

`evaluate` ties these together into one report dict; `report_json` renders it
to JSON so the optimised sequence and its evaluation can be emitted to a file
(plan section 6 / phase list: "emit the optimised sequence as JSON").

Usage:
    python evaluate.py EXTRACTED.json [EXTRACTED2.json ...] [-o OUT_DIR]
"""

import argparse
import json
import sys
from pathlib import Path

from item_requirements import item_requirements
from layout_graph import build_graph, expand_sequence
from roll_sequencing import (
    _clean_number,
    join_orders,
    load_rolls,
    profile_width,
    roll_profile,
    sequence_cost,
    transition_breakdown,
)
from sequencer import (
    DEFAULT_EXACT_MAX_LAYOUTS,
    path_cost,
    solve_exact,
    solve_heuristic,
)

# An exact Held–Karp solve is O(2^n * n^2). Above the Phase 3 exact threshold
# the sequence is produced heuristically, but for a modest range beyond it we
# can still afford to run the exact solver *as an oracle* purely to report the
# true optimality gap. Past this we fall back to the lower-bound gap only.
DEFAULT_EXACT_ORACLE_MAX_LAYOUTS = 16


# --------------------------------------------------------------------------
# Conservation — the optimised sequence must be a faithful reordering
# --------------------------------------------------------------------------
def _roll_key(roll):
    """A roll's identity for conservation: its lot number with `sort` as a
    stable secondary index (plan assumption 6). Falls back to object identity
    when neither is present so unlabeled test rolls still compare sensibly."""
    lot = roll.get("navision_lot")
    sort = roll.get("sort")
    if lot is None and sort is None:
        return ("id", id(roll))
    return ("lot", lot, sort)


def _sum_field(rolls, field):
    """Sum a numeric roll field, ignoring entries where it is missing or
    non-numeric. Returns a cleaned number (int when whole)."""
    total = 0
    for roll in rolls:
        value = roll.get(field)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            total += value
    return _clean_number(total)


def _counter(items):
    counts = {}
    for item in items:
        counts[item] = counts.get(item, 0) + 1
    return counts


def check_conservation(original_rolls, sequence):
    """Verify the optimised `sequence` is a faithful reordering of
    `original_rolls`: same rolls, same quantities, same totals, same layouts —
    only the order differs (plan section 6.1).

    Returns a dict with an overall `passed` flag and a per-check breakdown.
    Each numeric check records the original and sequenced value and whether
    they match; `discrepancies` lists any human-readable failures."""
    checks = {}
    discrepancies = []

    # Roll count.
    checks["roll_count"] = {
        "original": len(original_rolls),
        "sequence": len(sequence),
        "match": len(original_rolls) == len(sequence),
    }
    if not checks["roll_count"]["match"]:
        discrepancies.append(
            f"roll count changed: {len(original_rolls)} -> {len(sequence)}")

    # The multiset of roll identities must be identical (a permutation).
    orig_keys = _counter(_roll_key(r) for r in original_rolls)
    seq_keys = _counter(_roll_key(r) for r in sequence)
    identity_match = orig_keys == seq_keys
    checks["roll_identities"] = {"match": identity_match}
    if not identity_match:
        dropped = [k for k, c in orig_keys.items()
                   if seq_keys.get(k, 0) < c]
        added = [k for k, c in seq_keys.items()
                 if orig_keys.get(k, 0) < c]
        if dropped:
            discrepancies.append(f"rolls missing from sequence: {dropped}")
        if added:
            discrepancies.append(f"rolls added to sequence: {added}")

    # The multiset of layout signatures must match — no roll's own layout was
    # altered by reordering. We compare canonical profiles, not just the
    # extractor signature string, so a re-canonicalised layout still matches.
    orig_layouts = _counter(tuple(roll_profile(r)) for r in original_rolls)
    seq_layouts = _counter(tuple(roll_profile(r)) for r in sequence)
    layouts_match = orig_layouts == seq_layouts
    checks["layouts"] = {"match": layouts_match}
    if not layouts_match:
        discrepancies.append("set of roll layouts changed during reordering")

    # Physical quantities and totals must be preserved. Summing the same
    # values in a different order (extracted order vs optimised order) can
    # differ in the last floating-point bit, because floating-point addition
    # is not associative — e.g. 85646.1666666667 vs 85646.16666666669. That is
    # not a real change in what is produced, so totals are compared rounded to
    # 2 decimal places (the physical quantities are not tracked more finely).
    for field, label in (
        ("roll_qty", "physical_roll_qty"),
        ("mfg_roll_length_lf", "linear_feet_lf"),
        ("total_mfg_sf", "square_feet_sf"),
    ):
        orig = _sum_field(original_rolls, field)
        seq = _sum_field(sequence, field)
        match = round(orig, 2) == round(seq, 2)
        checks[label] = {"original": orig, "sequence": seq, "match": match}
        if not match:
            discrepancies.append(f"{label} changed: {orig} -> {seq}")

    return {
        "passed": not discrepancies,
        "checks": checks,
        "discrepancies": discrepancies,
    }


# --------------------------------------------------------------------------
# Solution quality — lower bound and (where feasible) exact oracle
# --------------------------------------------------------------------------
def lower_bound(matrix):
    """A lower bound on the minimum-cost open Hamiltonian path: the weight of
    the minimum spanning tree of the distance graph (Prim's algorithm).

    Any Hamiltonian path over the layouts is a spanning tree of the graph
    (connected, acyclic, n-1 edges). The MST is the cheapest spanning tree, so
    no path — including the true optimum — can cost less than the MST weight.
    It is quick to compute (O(n^2)) and needs no assumptions about the metric,
    which makes it a safe, honest floor to quote the heuristic gap against."""
    n = len(matrix)
    if n <= 1:
        return 0
    inf = float("inf")
    in_tree = [False] * n
    dist = [inf] * n
    dist[0] = 0
    total = 0
    for _ in range(n):
        u = -1
        best = inf
        for v in range(n):
            if not in_tree[v] and dist[v] < best:
                best, u = dist[v], v
        in_tree[u] = True
        total += dist[u]
        row = matrix[u]
        for v in range(n):
            if not in_tree[v] and row[v] < dist[v]:
                dist[v] = row[v]
    return _clean_number(total)


def solution_quality(matrix, achieved_cost, optimal,
                     oracle_max_layouts=DEFAULT_EXACT_ORACLE_MAX_LAYOUTS):
    """Describe how good the achieved sequence is (plan section 6.3).

    - `lower_bound_in`: the MST lower bound; the optimum cannot be cheaper.
    - `gap_to_lower_bound_in` / `gap_ratio`: how far the achieved cost sits
      above that floor. The true gap to the optimum is no larger than this.
    - When solved exactly, `proven_optimal` is True and the gap to the optimum
      is 0 by construction.
    - Otherwise, if the instance is small enough, we run the exact solver as an
      oracle and report the *true* `gap_to_exact_optimum_in`."""
    n = len(matrix)
    lb = lower_bound(matrix)
    gap_lb = _clean_number(achieved_cost - lb)
    ratio = _clean_number(round(gap_lb / lb, 4)) if lb else None

    quality = {
        "achieved_cost_in": _clean_number(achieved_cost),
        "lower_bound_in": lb,
        "lower_bound_method": "minimum spanning tree",
        "gap_to_lower_bound_in": gap_lb,
        "gap_ratio": ratio,
        "proven_optimal": bool(optimal),
        "exact_optimum_in": None,
        "gap_to_exact_optimum_in": None,
        "exact_optimum_source": None,
    }

    if optimal:
        # The achieved cost *is* the proven optimum.
        quality["exact_optimum_in"] = _clean_number(achieved_cost)
        quality["gap_to_exact_optimum_in"] = 0
        quality["exact_optimum_source"] = "held-karp (solver)"
    elif n <= oracle_max_layouts:
        # Cheap enough to prove the optimum after the fact, purely to report
        # the true gap of the heuristic result.
        _, opt_cost = solve_exact(matrix)
        opt_cost = _clean_number(opt_cost)
        quality["exact_optimum_in"] = opt_cost
        quality["gap_to_exact_optimum_in"] = _clean_number(achieved_cost - opt_cost)
        quality["exact_optimum_source"] = "held-karp (oracle)"

    return quality


# --------------------------------------------------------------------------
# Full evaluation report
# --------------------------------------------------------------------------
def evaluate(rolls, exact_max_layouts=DEFAULT_EXACT_MAX_LAYOUTS,
             oracle_max_layouts=DEFAULT_EXACT_ORACLE_MAX_LAYOUTS,
             extraction=None, warnings=None):
    """Optimise an order's rolls and evaluate the result against the plan's
    four reporting criteria (section 6). Returns a JSON-serialisable report.

    `rolls` is the list of roll dicts (Phase 1's `load_rolls` output).
    `extraction` is the optional full extraction dict, used only to echo the
    source file and purchase order number and to cross-check totals against
    the extractor's MFG summary.

    The report contains the optimised manufacturing sequence, the achieved
    cost, the conservation result, the solution-quality analysis, and the
    transition breakdown."""
    if warnings is None:
        warnings = []

    groups, matrix = build_graph(rolls, warnings)
    n = len(groups)

    if n <= exact_max_layouts:
        order, cost = solve_exact(matrix)
        method, optimal = "held-karp", True
    else:
        order, cost = solve_heuristic(matrix)
        method, optimal = "nearest-neighbour + 2-opt/Or-opt", False

    sequence = expand_sequence(groups, order)

    # Cross-check: the matrix path cost must equal the Phase 1 cost of the
    # expanded roll sequence. A disagreement means an internal bug; trust the
    # directly scored sequence and surface it.
    scored = sequence_cost(sequence, warnings)
    if scored != cost:
        warnings.append(
            f"Internal check: matrix path cost {cost} != scored sequence cost "
            f"{scored}; using the scored value.")
        cost = scored

    conservation = check_conservation(rolls, sequence)
    quality = solution_quality(matrix, cost, optimal, oracle_max_layouts)
    breakdown = transition_breakdown(sequence)

    # Optional cross-check against the extractor's own MFG summary totals,
    # and — when the extraction carries a yarn_lbs block — the per-item batch
    # requirements (item SKU, lbs of yarn, bobbin count). Item warnings land
    # in the same `warnings` list, so they surface in the report like all
    # others.
    extraction_po = None
    item_reqs = None
    if isinstance(extraction, dict):
        _cross_check_mfg_summary(extraction, sequence, warnings)
        info = extraction.get("general_information")
        if isinstance(info, dict):
            extraction_po = info.get("purchase_order_number")
        if extraction.get("yarn_lbs") is not None:
            item_reqs = item_requirements(extraction, warnings=warnings)

    return {
        "source_file": extraction.get("source_file")
        if isinstance(extraction, dict) else None,
        "roll_count": len(sequence),
        "distinct_layout_count": n,
        "method": method,
        "optimal": optimal,
        "achieved_cost_in": _clean_number(cost),
        "reference_only_as_extracted_cost_in": sequence_cost(rolls),
        "conservation": conservation,
        "solution_quality": quality,
        "transition_breakdown": breakdown,
        # Only present when the extraction carries a yarn_lbs block; omitted
        # (not an empty list) otherwise, so older reports keep their shape.
        **({"item_requirements": item_reqs} if item_reqs is not None else {}),
        "layout_order": order,
        "layouts": [
            {
                "layout_index": g["layout_index"],
                "layout_signature": g["layout_signature"],
                "width_in": g["width_in"],
                "roll_entry_count": g["roll_entry_count"],
                "physical_roll_qty": g["physical_roll_qty"],
            }
            for g in groups
        ],
        "manufacturing_sequence": _sequence_view(sequence, extraction_po),
        "distance_matrix": matrix,
        "warnings": warnings,
    }


def _cross_check_mfg_summary(extraction, sequence, warnings):
    """Compare the sequence's roll/linear-foot/square-foot totals against the
    extractor's MFG summary, if present, and warn on any mismatch. This checks
    conservation against a second, independent source rather than only the
    roll rows.

    In combined mode `extraction` is the `join_orders` result, whose summary
    fields are already summed across every input file, so the same comparison
    checks the combined sequence against the combined stated totals."""
    summary = extraction.get("mfg_summary")
    if not isinstance(summary, dict):
        return
    for field, summary_key, label in (
        ("roll_qty", "mfg_rolls", "physical roll"),
        ("mfg_roll_length_lf", "mfg_lf", "linear feet"),
        ("total_mfg_sf", "mfg_sf", "square feet"),
    ):
        stated = summary.get(summary_key)
        if not isinstance(stated, (int, float)) or isinstance(stated, bool):
            continue
        actual = _sum_field(sequence, field)
        if abs(actual - stated) > 1e-6:
            warnings.append(
                f"Sequence {label} total {actual} differs from MFG summary "
                f"{stated}; reporting the summed roll rows.")


def _sequence_view(sequence, extraction_po=None):
    """A compact, JSON-friendly view of the optimised sequence: one entry per
    roll in manufacturing order, with its identity, purchase order number,
    panel numbers, quantity, size, layout, and the setup change cost incurred
    to switch to it from the previous roll (0 for the first roll — a fresh
    start, plan assumption 7).

    The purchase order number is per roll because a combined order mixes
    files with different POs: a roll's own `purchase_order_number` tag (set
    by `join_orders`) wins, falling back to `extraction_po` — the single
    file's PO — and to None when neither is known."""
    from roll_sequencing import profile_cost

    view = []
    prev_profile = None
    for position, roll in enumerate(sequence, start=1):
        profile = roll_profile(roll)
        change = 0 if prev_profile is None else profile_cost(prev_profile, profile)
        po = roll.get("purchase_order_number")
        view.append({
            "position": position,
            "navision_lot": roll.get("navision_lot"),
            "purchase_order_number": po if po is not None else extraction_po,
            "panel_numbers": roll.get("panel_numbers"),
            "sort": roll.get("sort"),
            "roll_type": roll.get("roll_type"),
            "roll_qty": roll.get("roll_qty"),
            "mfg_roll_length_lf": roll.get("mfg_roll_length_lf"),
            "total_mfg_sf": roll.get("total_mfg_sf"),
            "layout_signature": roll.get("layout_signature"),
            "layout_group": roll.get("layout_group"),
            "change_cost_in": _clean_number(change),
        })
        prev_profile = profile
    return view


def report_json(report, indent=2):
    """Render an `evaluate` report as a JSON string. `default=str` keeps any
    stray non-JSON scalars (e.g. dates carried through from extraction)
    serialisable rather than failing."""
    return json.dumps(report, indent=indent, default=str)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _print_summary(path, report):
    q = report["solution_quality"]
    c = report["conservation"]
    b = report["transition_breakdown"]

    print(f"\n{path}")
    print(f"  rolls:               {report['roll_count']}")
    print(f"  distinct layouts:    {report['distinct_layout_count']}")
    print(f"  method:              {report['method']} "
          f"({'proven optimum' if report['optimal'] else 'near-optimal'})")
    print(f"  achieved setup cost: {report['achieved_cost_in']} in")
    print(f"  (as-extracted, reference only: "
          f"{report['reference_only_as_extracted_cost_in']} in)")

    print("  conservation:        "
          + ("PASS" if c["passed"] else "FAIL"))
    for d in c["discrepancies"]:
        print(f"    - {d}")

    print(f"  lower bound (MST):   {q['lower_bound_in']} in")
    print(f"  gap to lower bound:  {q['gap_to_lower_bound_in']} in"
          + (f" ({q['gap_ratio'] * 100:.1f}%)" if q["gap_ratio"] is not None else ""))
    if q["exact_optimum_in"] is not None:
        tag = "proven" if q["proven_optimal"] else f"oracle: {q['exact_optimum_source']}"
        print(f"  exact optimum:       {q['exact_optimum_in']} in "
              f"(gap {q['gap_to_exact_optimum_in']} in, {tag})")

    print(f"  transitions:         {b['transition_count']} "
          f"({b['zero_cost_transitions']} zero-cost)")
    print(f"  max / mean change:   {b['max_transition_cost']} in / "
          f"{b['mean_transition_cost']} in")

    for w in report["warnings"]:
        print(f"  warning: {w}")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+",
                        help="Extraction result JSON file(s) from extract_turf_layout.py")
    parser.add_argument("-o", "--out-dir",
                        help="Write the full JSON report per input into this "
                             "directory as <stem>.sequence.json. If omitted, "
                             "only the console summary is printed.")
    parser.add_argument("--exact-max-layouts", type=int,
                        default=DEFAULT_EXACT_MAX_LAYOUTS,
                        help="Solve exactly (Held–Karp) at or below this many "
                             "distinct layouts; use the heuristic above it "
                             f"(default {DEFAULT_EXACT_MAX_LAYOUTS}).")
    parser.add_argument("--oracle-max-layouts", type=int,
                        default=DEFAULT_EXACT_ORACLE_MAX_LAYOUTS,
                        help="For heuristic results, still solve exactly as an "
                             "oracle to report the true gap at or below this "
                             f"many layouts (default {DEFAULT_EXACT_ORACLE_MAX_LAYOUTS}).")
    parser.add_argument("--combine", action="store_true",
                        help="Join all inputs into one combined order and "
                             "evaluate it as a single sequence, instead of "
                             "evaluating each file on its own.")
    args = parser.parse_args()

    out_dir = None
    if args.out_dir:
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)

    if args.combine:
        extractions = [json.loads(Path(path).read_text())
                       for path in args.files]
        combined = join_orders(extractions)
        jobs = [(f"combined: {combined['source_file']}",
                 combined["rolls"], combined, "combined")]
    else:
        jobs = []
        for path in args.files:
            data = json.loads(Path(path).read_text())
            jobs.append((path, load_rolls(data),
                         data if isinstance(data, dict) else None,
                         Path(path).stem))

    exit_code = 0
    for label, rolls, extraction, stem in jobs:
        report = evaluate(
            rolls,
            exact_max_layouts=args.exact_max_layouts,
            oracle_max_layouts=args.oracle_max_layouts,
            extraction=extraction,
        )
        _print_summary(label, report)

        if not report["conservation"]["passed"]:
            exit_code = 1

        if out_dir is not None:
            out_path = out_dir / (stem + ".sequence.json")
            out_path.write_text(report_json(report))
            print(f"  wrote {out_path}")

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
