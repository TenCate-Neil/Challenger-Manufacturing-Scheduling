#!/usr/bin/env python3
"""
Roll sequencing — collapse duplicates and distance graph (Phase 2).

This is Step 1 and Step 2 of docs/optimisation_plan.md section 4, built on the
Phase 1 cost model in `roll_sequencing.py`. It does two things:

  1. Collapse identical layouts (`collapse_layouts`). Rolls that share an
     identical threading profile can be produced in any internal order at zero
     cost, so we group them into *distinct layouts*. This shrinks the problem
     from "number of rolls" to "number of distinct layouts", which is what
     makes exact sequencing feasible in Phase 3.

  2. Build the distance graph (`distance_matrix`). Compute the pairwise setup
     change cost between every pair of distinct layouts, using the positional
     inch-mismatch cost from Phase 1.

It also provides `expand_sequence`, the inverse of collapsing: given an order
of distinct layouts, expand each back into its member rolls to recover a full
manufacturing sequence. Grouping preserves every roll, so expansion always
reproduces exactly the rolls that went in — nothing added, dropped, or altered
(plan section 6.1, conservation).

Choosing the order of the distinct layouts is Phase 3; this module only builds
the collapsed problem and its distances.

Grouping identity
-----------------
Two rolls belong to the same layout exactly when their canonical threading
profiles are equal — which is precisely the condition under which the Phase 1
cost between them is 0. We therefore group on the canonical profile itself
rather than trusting a precomputed id. In normal data this matches the
extractor's `layout_group`; where a workbook happens to split one colour run
into two adjacent segments, grouping on the canonical (merged) profile is the
stricter, correct choice. The extractor's `layout_group` / `layout_signature`
are carried through for traceability, and a mismatch is reported as a warning.

Usage:
    python layout_graph.py EXTRACTED.json [EXTRACTED2.json ...]
"""

import argparse
import json
import sys
from pathlib import Path

from roll_sequencing import (
    profile_cost,
    profile_width,
    roll_profile,
)


def _clean_number(value):
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


def _signature_from_profile(profile):
    """Render a canonical profile back to the extractor's signature string
    form, e.g. [("WHI", 5), ("FG", 177)] -> "5WHI|177FG"."""
    return "|".join(f"{_clean_number(w)}{c}" for c, w in profile)


# --------------------------------------------------------------------------
# Step 1 — collapse identical layouts
# --------------------------------------------------------------------------
def collapse_layouts(rolls, warnings=None):
    """Partition rolls into distinct layouts by identical canonical profile.

    Returns a list of layout-group dicts in first-appearance order:

        {
          "layout_index": int,            # stable 0-based position
          "profile": [(color_code, width), ...],   # canonical threading
          "layout_signature": str,        # canonical signature string
          "width_in": total roll width,
          "extractor_layout_group": id or None,    # from the extractor, if any
          "rolls": [roll dict, ...],      # members, original order preserved
          "roll_entry_count": int,        # number of roll rows in the group
          "physical_roll_qty": number or None,     # sum of roll_qty across rows
        }

    Every input roll appears in exactly one group, so the groups are a
    lossless partition of the order."""
    groups = {}          # profile tuple -> group dict
    order = []           # profile tuples in first-appearance order

    for roll in rolls:
        profile = roll_profile(roll)
        key = tuple(profile)
        if key not in groups:
            groups[key] = {
                "layout_index": len(order),
                "profile": profile,
                "layout_signature": _signature_from_profile(profile),
                "width_in": _clean_number(profile_width(profile)),
                "extractor_layout_group": roll.get("layout_group"),
                "rolls": [],
                "roll_entry_count": 0,
                "physical_roll_qty": None,
            }
            order.append(key)
        group = groups[key]
        group["rolls"].append(roll)
        group["roll_entry_count"] += 1
        qty = roll.get("roll_qty")
        if isinstance(qty, (int, float)):
            group["physical_roll_qty"] = (group["physical_roll_qty"] or 0) + qty

    result = [groups[key] for key in order]
    if warnings is not None:
        _check_extractor_consistency(result, warnings)
    return result


def _check_extractor_consistency(groups, warnings):
    """Cross-check our profile-based grouping against the extractor's
    `layout_group` ids, and surface any disagreement rather than hiding it."""
    # A single extractor layout_group id must not span two distinct profiles.
    id_to_index = {}
    for group in groups:
        for roll in group["rolls"]:
            gid = roll.get("layout_group")
            if gid is None:
                continue
            seen = id_to_index.setdefault(gid, group["layout_index"])
            if seen != group["layout_index"]:
                warnings.append(
                    f"Extractor layout_group {gid} spans distinct layouts "
                    f"{seen} and {group['layout_index']}; grouping by threading "
                    "profile instead."
                )
    # Conversely, one profile should not carry two different extractor ids.
    for group in groups:
        ids = {r.get("layout_group") for r in group["rolls"]
               if r.get("layout_group") is not None}
        if len(ids) > 1:
            warnings.append(
                f"Layout {group['layout_index']} ({group['layout_signature']}) "
                f"has an identical threading profile but multiple extractor "
                f"layout_group ids {sorted(ids)}; treating them as one layout."
            )


# --------------------------------------------------------------------------
# Step 2 — build the distance graph
# --------------------------------------------------------------------------
def distance_matrix(groups):
    """Symmetric matrix of pairwise setup change costs between distinct
    layouts. `matrix[i][j]` is the cost of changing between layout `i` and
    layout `j`; the diagonal is 0 and `matrix[i][j] == matrix[j][i]`."""
    n = len(groups)
    profiles = [g["profile"] for g in groups]
    matrix = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            cost = profile_cost(profiles[i], profiles[j])
            matrix[i][j] = cost
            matrix[j][i] = cost
    return matrix


# --------------------------------------------------------------------------
# Inverse of Step 1 — expand distinct layouts back into rolls
# --------------------------------------------------------------------------
def expand_sequence(groups, order=None):
    """Expand an ordering of distinct layouts back into the full roll
    sequence. `order` is a list of `layout_index` values; if omitted, the
    groups are expanded in their given order. Within each layout the member
    rolls keep their original relative order (all internal orders cost the
    same, so this is an arbitrary but stable choice).

    The result is a permutation of the original rolls — this is the step that
    guarantees conservation when Phase 3 hands back a reordered layout
    sequence."""
    if order is None:
        order = [g["layout_index"] for g in groups]
    by_index = {g["layout_index"]: g for g in groups}
    missing = [i for i in order if i not in by_index]
    if missing:
        raise ValueError(f"Unknown layout_index values in order: {missing}")
    if len(set(order)) != len(order):
        raise ValueError("Ordering repeats a layout_index; each distinct "
                         "layout must appear exactly once.")
    if len(order) != len(groups):
        raise ValueError(
            f"Ordering covers {len(order)} of {len(groups)} distinct layouts; "
            "every layout must be sequenced exactly once to conserve the order.")
    rolls = []
    for index in order:
        rolls.extend(by_index[index]["rolls"])
    return rolls


# --------------------------------------------------------------------------
# Loading / reporting
# --------------------------------------------------------------------------
def build_graph(rolls, warnings=None):
    """Convenience: collapse the rolls and build their distance matrix in one
    call. Returns (groups, matrix)."""
    groups = collapse_layouts(rolls, warnings)
    return groups, distance_matrix(groups)


def _report_file(path):
    from roll_sequencing import load_rolls

    data = json.loads(Path(path).read_text())
    rolls = load_rolls(data)
    warnings = []
    groups, matrix = build_graph(rolls, warnings)

    print(f"\n{path}")
    print(f"  rolls:            {len(rolls)}")
    print(f"  distinct layouts: {len(groups)}")
    if isinstance(data, dict) and data.get("distinct_layout_count") is not None:
        print(f"  extractor distinct_layout_count: {data['distinct_layout_count']}")

    print("  layouts:")
    for g in groups:
        qty = g["physical_roll_qty"]
        qty_text = f", {qty} physical roll(s)" if qty is not None else ""
        print(f"    [{g['layout_index']}] {g['layout_signature']}  "
              f"({g['roll_entry_count']} roll row(s){qty_text})")

    if len(groups) <= 24:
        print("  distance graph (inches to change between layouts):")
        header = "       " + " ".join(f"{i:>4}" for i in range(len(groups)))
        print(header)
        for i, row in enumerate(matrix):
            print(f"    {i:>3}  " + " ".join(f"{v:>4}" for v in row))
    else:
        print(f"  distance graph: {len(groups)}x{len(groups)} matrix "
              "(omitted from console; available via distance_matrix()).")

    for w in warnings:
        print(f"  warning: {w}")


def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+",
                        help="Extraction result JSON file(s) from extract_turf_layout.py")
    args = parser.parse_args()
    for path in args.files:
        _report_file(path)


if __name__ == "__main__":
    sys.exit(main())
