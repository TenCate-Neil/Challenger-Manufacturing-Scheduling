#!/usr/bin/env python3
"""
Roll sequencing — the sequencing engine (Phase 3).

This is Step 3 of docs/optimisation_plan.md section 4: choose the order in
which the distinct layouts (from Phase 2) are manufactured so that the total
setup change cost is as low as possible.

The problem is the open-path form of the symmetric Travelling Salesman
Problem: find the minimum-cost ordering of the distinct layouts, where the
distance between two layouts is the Phase 1 positional inch mismatch. It is an
*open path*, not a cycle — the sequence is free to start and end anywhere and
never returns to its start. That matches the "fresh start" assumption (plan
assumption 7): there is no fixed current machine threading to cost the first
transition against; costing from a known start state is deferred to a later
cross-order phase.

Tiered approach (plan section 4, step 3):

  - Small orders (few distinct layouts): solved exactly with a Held–Karp
    dynamic program over subsets, which returns the proven minimum-cost path.
  - Larger orders: a multi-start nearest-neighbour construction improved by
    2-opt and Or-opt local search — standard, well-understood methods that
    behave reliably at this scale, returning a near-optimal path.

The engine works on the distance matrix and returns an ordering of distinct
layouts; `optimise` wires it to Phase 2 (collapse + distances) and Phase 2's
`expand_sequence` to produce the full roll sequence. Reporting solution
quality (gap to a lower bound, transition breakdown) is Phase 4.

Usage:
    python sequencer.py EXTRACTED.json [EXTRACTED2.json ...]
"""

import argparse
import json
import sys
from pathlib import Path

from layout_graph import build_graph, expand_sequence
from roll_sequencing import _clean_number, join_orders, load_rolls, sequence_cost

# Orders with at most this many distinct layouts are solved exactly with
# Held–Karp. The DP is O(2^n * n^2) in time and O(2^n * n) in memory, so this
# stays fast and small; above it we fall back to the heuristic. Most turf
# orders have well under this many distinct layouts, so exact is the common
# case.
DEFAULT_EXACT_MAX_LAYOUTS = 15

# Segment lengths relocated during Or-opt local search.
_OR_OPT_SEGMENTS = (1, 2, 3)


# --------------------------------------------------------------------------
# Cost of an ordering
# --------------------------------------------------------------------------
def path_cost(matrix, order):
    """Total cost of visiting the layouts in `order` as an open path (sum of
    consecutive edges; no wrap-around back to the start)."""
    return sum(matrix[order[i]][order[i + 1]] for i in range(len(order) - 1))


# --------------------------------------------------------------------------
# Exact solver — Held–Karp for the open shortest Hamiltonian path
# --------------------------------------------------------------------------
def solve_exact(matrix):
    """Minimum-cost open Hamiltonian path over all layouts, solved exactly.

    dp[mask][j] is the least cost to visit exactly the set `mask` of layouts
    and end at `j`. The base case allows *any* layout to be the start (free
    start), and the answer is the best over all end layouts (free end), so
    both endpoints are unconstrained. Returns (order, cost)."""
    n = len(matrix)
    if n == 0:
        return [], 0
    if n == 1:
        return [0], 0

    size = 1 << n
    inf = float("inf")
    dp = [[inf] * n for _ in range(size)]
    parent = [[-1] * n for _ in range(size)]

    for j in range(n):
        dp[1 << j][j] = 0  # a path may start at any single layout

    for mask in range(size):
        row = dp[mask]
        for j in range(n):
            base = row[j]
            if base == inf or not (mask >> j) & 1:
                continue
            for k in range(n):
                if (mask >> k) & 1:
                    continue
                nmask = mask | (1 << k)
                cand = base + matrix[j][k]
                if cand < dp[nmask][k]:
                    dp[nmask][k] = cand
                    parent[nmask][k] = j

    full = size - 1
    end = min(range(n), key=lambda j: dp[full][j])
    cost = dp[full][end]

    # walk parent pointers back to the start
    order = []
    mask, j = full, end
    while j != -1:
        order.append(j)
        prev = parent[mask][j]
        mask ^= (1 << j)
        j = prev
    order.reverse()
    return order, cost


# --------------------------------------------------------------------------
# Heuristic solver — nearest neighbour + 2-opt + Or-opt
# --------------------------------------------------------------------------
def _nearest_neighbour(matrix, start):
    n = len(matrix)
    visited = [False] * n
    visited[start] = True
    order = [start]
    current = start
    for _ in range(n - 1):
        best, best_d = -1, float("inf")
        row = matrix[current]
        for k in range(n):
            if not visited[k] and row[k] < best_d:
                best, best_d = k, row[k]
        order.append(best)
        visited[best] = True
        current = best
    return order


def _two_opt(order, matrix):
    """Reverse path segments while any reversal lowers the cost. For an open
    path only the edges at the two cut points change, so each move is O(1) to
    evaluate."""
    n = len(order)
    improved = True
    while improved:
        improved = False
        for i in range(n - 1):
            a = order[i - 1] if i > 0 else None
            b = order[i]
            for j in range(i + 1, n):
                c = order[j]
                e = order[j + 1] if j + 1 < n else None
                # cost of the edges we would remove vs. add
                removed = (matrix[a][b] if a is not None else 0) \
                    + (matrix[c][e] if e is not None else 0)
                added = (matrix[a][c] if a is not None else 0) \
                    + (matrix[b][e] if e is not None else 0)
                if added + 1e-9 < removed:
                    order[i:j + 1] = order[i:j + 1][::-1]
                    improved = True
                    b = order[i]
    return order


def _or_opt(order, matrix):
    """Relocate short segments (length 1, 2, 3) to a better position. Catches
    improvements 2-opt alone can miss.

    Each candidate move is scored incrementally: relocating a segment only
    breaks the two edges around it (plus the edge at the insertion point) and
    creates three new ones, so the cost delta is O(1) to evaluate — a full
    `path_cost` recompute per candidate made one pass O(n^3), which large
    combined orders can't afford. The move is applied exactly when the delta
    is an improvement, so accepted moves (and the final order) are identical
    to the recompute-based form."""
    n = len(order)
    improved = True
    while improved:
        improved = False
        for seg_len in _OR_OPT_SEGMENTS:
            if seg_len >= n:
                continue
            for i in range(0, n - seg_len + 1):
                segment = order[i:i + seg_len]
                rest = order[:i] + order[i + seg_len:]
                prev = order[i - 1] if i > 0 else None
                nxt = order[i + seg_len] if i + seg_len < n else None
                # Removing the segment drops its boundary edges and joins the
                # neighbours it leaves behind.
                removed = (matrix[prev][segment[0]] if prev is not None else 0) \
                    + (matrix[segment[-1]][nxt] if nxt is not None else 0)
                join = matrix[prev][nxt] \
                    if prev is not None and nxt is not None else 0
                for pos in range(len(rest) + 1):
                    if pos == i:
                        continue  # same place
                    before = rest[pos - 1] if pos > 0 else None
                    after = rest[pos] if pos < len(rest) else None
                    broken = matrix[before][after] \
                        if before is not None and after is not None else 0
                    added = (matrix[before][segment[0]] if before is not None else 0) \
                        + (matrix[segment[-1]][after] if after is not None else 0)
                    if (join - removed) + (added - broken) < -1e-9:
                        order = rest[:pos] + segment + rest[pos:]
                        improved = True
                        break
                if improved:
                    break
            if improved:
                break
    return order


def solve_heuristic(matrix, max_starts=None):
    """Near-optimal open path via multi-start nearest neighbour, then 2-opt
    and Or-opt local search. Returns (order, cost)."""
    n = len(matrix)
    if n <= 1:
        return list(range(n)), 0

    # Try several nearest-neighbour starts and keep the best construction.
    if max_starts is None:
        max_starts = n if n <= 60 else 30
    starts = range(n) if n <= max_starts else range(max_starts)

    best_order, best_cost = None, float("inf")
    for start in starts:
        order = _nearest_neighbour(matrix, start)
        cost = path_cost(matrix, order)
        if cost < best_cost:
            best_order, best_cost = order, cost

    # Local search until neither move improves further.
    improved = True
    while improved:
        before = path_cost(matrix, best_order)
        best_order = _two_opt(best_order, matrix)
        best_order = _or_opt(best_order, matrix)
        after = path_cost(matrix, best_order)
        improved = after + 1e-9 < before
    return best_order, path_cost(matrix, best_order)


# --------------------------------------------------------------------------
# Top-level: collapse -> distances -> sequence -> expand
# --------------------------------------------------------------------------
def optimise(rolls, exact_max_layouts=DEFAULT_EXACT_MAX_LAYOUTS, warnings=None):
    """Optimise the manufacturing order of a single order's rolls.

    Collapses duplicate layouts (Phase 2), builds the distance graph, chooses
    a low-cost layout ordering (exactly when small, heuristically when large),
    and expands it back into the full roll sequence. Returns a result dict:

        {
          "layout_order": [layout_index, ...],   # order of distinct layouts
          "sequence": [roll dict, ...],           # full expanded roll order
          "cost": total setup change cost (inches),
          "method": "held-karp" | "nearest-neighbour + 2-opt/Or-opt",
          "optimal": bool,        # True only when solved exactly
          "roll_count": int,
          "distinct_layout_count": int,
          "groups": [...],        # Phase 2 layout groups
          "distance_matrix": [[...]],
        }
    """
    groups, matrix = build_graph(rolls, warnings)
    n = len(groups)

    if n <= exact_max_layouts:
        order, cost = solve_exact(matrix)
        method, optimal = "held-karp", True
    else:
        order, cost = solve_heuristic(matrix)
        method, optimal = "nearest-neighbour + 2-opt/Or-opt", False

    sequence = expand_sequence(groups, order)

    # Sanity: the cost computed over the distance matrix must equal the cost
    # of the expanded roll sequence scored directly by the Phase 1 model.
    scored = sequence_cost(sequence)
    if scored != cost:
        (warnings if warnings is not None else []).append(
            f"Internal check: matrix path cost {cost} != scored sequence cost "
            f"{scored}; using the scored value.")
        cost = scored

    return {
        "layout_order": order,
        "sequence": sequence,
        "cost": cost,
        "method": method,
        "optimal": optimal,
        "roll_count": len(sequence),
        "distinct_layout_count": n,
        "groups": groups,
        "distance_matrix": matrix,
    }


# --------------------------------------------------------------------------
# Splitting a solved path into k manufacturing schedules
# --------------------------------------------------------------------------
def choose_cuts(transition_costs, k=None, threshold=None):
    """Choose where to cut a solved open path into separate manufacturing
    schedules. `transition_costs[i]` is the cost of the transition between
    sequence positions i and i+1; the returned cut indices are ascending
    positions in that list. Exactly one of `k` / `threshold` must be given:

      - `k`: cut the k-1 most expensive transitions, giving k schedules (or
        one per roll if k exceeds the sequence length). Ties are broken
        toward the earlier transition so the result is deterministic.
      - `threshold`: cut every transition whose cost strictly exceeds it —
        "split where a changeover exceeds X inches".

    Each schedule restarts from a fresh machine state (cost 0 at its start,
    matching plan assumption 7), so the total cost drops by exactly the sum
    of the removed transitions. Cutting the largest edges is therefore
    optimal for partitioning *this fixed ordering* into k open paths. It is
    not a proven global optimum over all assignments of rolls to k schedules
    — the ordering was solved before the cuts — though the solved path
    already clusters similar layouts, so its big changeovers are the natural
    boundaries."""
    if (k is None) == (threshold is None):
        raise ValueError("give exactly one of k or threshold")
    if threshold is not None:
        return [i for i, cost in enumerate(transition_costs)
                if cost > threshold]
    if k < 1:
        raise ValueError(f"k must be at least 1, got {k}")
    ranked = sorted(range(len(transition_costs)),
                    key=lambda i: (-transition_costs[i], i))
    return sorted(ranked[:min(k - 1, len(transition_costs))])


def split_guidance(transition_costs, max_k=None):
    """Elbow data for choosing k: one row per k = 1..max_k with the total
    cost after cutting the k-1 most expensive transitions and the marginal
    saving one more cut (k -> k+1) would add.

    The marginal savings are simply the transition costs in descending order,
    so the total is non-increasing in k and each extra schedule saves no more
    than the last — the natural k sits where the next saving stops being
    worth a separate schedule. `max_k` defaults to one more than the number
    of positive-cost transitions, past which further cuts save nothing."""
    if max_k is None:
        max_k = sum(1 for cost in transition_costs if cost > 0) + 1
    max_k = max(1, min(max_k, len(transition_costs) + 1))
    ranked = sorted(transition_costs, reverse=True)
    remaining = sum(transition_costs)
    rows = []
    for k in range(1, max_k + 1):
        saving = ranked[k - 1] if k - 1 < len(ranked) else 0
        rows.append({
            "k": k,
            "total_cost_in": _clean_number(remaining),
            "marginal_saving_in": _clean_number(saving),
        })
        remaining -= saving
    return rows


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _report_rolls(label, rolls, exact_max_layouts):
    warnings = []
    result = optimise(rolls, exact_max_layouts=exact_max_layouts, warnings=warnings)

    print(f"\n{label}")
    print(f"  rolls:              {result['roll_count']}")
    print(f"  distinct layouts:   {result['distinct_layout_count']}")
    print(f"  method:             {result['method']} "
          f"({'proven optimum' if result['optimal'] else 'near-optimal'})")
    print(f"  achieved setup cost: {result['cost']} in")

    # For a human sanity check only — the as-extracted order is assigned by
    # sales and is explicitly not a target (plan section 6).
    as_extracted = sequence_cost(rolls)
    print(f"  (as-extracted order cost, reference only: {as_extracted} in)")

    order = result["layout_order"]
    sigs = {g["layout_index"]: g["layout_signature"] for g in result["groups"]}
    print("  layout order: " + " -> ".join(f"{i}:{sigs[i]}" for i in order))

    for w in warnings:
        print(f"  warning: {w}")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+",
                        help="Extraction result JSON file(s) from extract_turf_layout.py")
    parser.add_argument("--exact-max-layouts", type=int,
                        default=DEFAULT_EXACT_MAX_LAYOUTS,
                        help="Solve exactly (Held–Karp) at or below this many "
                             "distinct layouts; use the heuristic above it "
                             f"(default {DEFAULT_EXACT_MAX_LAYOUTS}).")
    parser.add_argument("--combine", action="store_true",
                        help="Join all inputs into one combined order and "
                             "sequence it as a single run, instead of "
                             "sequencing each file on its own.")
    args = parser.parse_args()

    if args.combine:
        extractions = [json.loads(Path(path).read_text()) for path in args.files]
        combined = join_orders(extractions)
        _report_rolls(f"combined: {combined['source_file']}",
                      combined["rolls"], args.exact_max_layouts)
        return
    for path in args.files:
        data = json.loads(Path(path).read_text())
        _report_rolls(path, load_rolls(data), args.exact_max_layouts)


if __name__ == "__main__":
    sys.exit(main())
