#!/usr/bin/env python3
"""
Roll sequencing — cost model and sequence scorer (Phase 1).

This is the core costing logic described in docs/optimisation_plan.md,
sections 3 and 9. It reads the roll layouts produced by
`extract_turf_layout.py` and answers two questions:

  1. What does it cost to change the machine setup between two consecutive
     rolls?  (`transition_cost`)
  2. What does a whole manufacturing sequence cost, and how is that cost
     distributed across its transitions?  (`sequence_cost`,
     `transition_breakdown`)

It does *not* reorder anything. Choosing a cheaper sequence is Phase 3;
Phase 1 only measures the cost of a sequence that is handed to it.

The cost model
--------------
Each roll is a positional profile across the roll width: for every position
`x` measured from the front of the machine, the profile records which
colour/type code is threaded there. The setup change cost between two
consecutive rolls is the total width (in inches) of the positions whose
colour/type differs:

    cost(A, B) = sum of segment widths where profile_A(x) != profile_B(x)

For integer segment widths this is exactly the "number of inch positions
that differ" from the plan. Fractional widths (fractional gauges) fall out
of the same formula as fractional inches. The cost is symmetric — swapping A
and B changes nothing — and two identical layouts cost 0.

See docs/optimisation_plan.md sections 3, 4 and 7 for the assumptions this
rests on (fixed orientation, fixed widths and creel locations, cost measured
in inches with no per-stoppage penalty).

Usage:
    python roll_sequencing.py EXTRACTED.json [EXTRACTED2.json ...]

Scores each extraction result in the order the rolls appear in the file and
prints the total cost and a transition breakdown. The as-extracted order is
assigned by sales and is *not* a target to beat (plan section 6); scoring it
here is simply a way to exercise the model on real data.
"""

import argparse
import json
import re
import sys
from pathlib import Path

# Widths may be integer inches or fractional (fractional gauges). We compare
# accumulated widths with a small tolerance so floating-point segment sums
# don't leak a sliver of phantom cost.
_TOL = 1e-9

# A layout signature segment looks like "177LIM" or "5WHI" or "12.5FG":
# a numeric width immediately followed by the colour/type code.
_SIGNATURE_SEGMENT = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*([^|]+?)\s*$")


# --------------------------------------------------------------------------
# Building profiles
# --------------------------------------------------------------------------
def _canonical(segments):
    """Normalise a list of (color_code, width) segments left to right:
    drop zero-width segments and merge runs of the same colour/type. Merging
    never changes the cost — it just gives every equivalent layout one
    canonical form, so identical layouts written differently still cost 0
    against each other."""
    canonical = []
    for color, width in segments:
        if width <= _TOL:
            continue
        if canonical and canonical[-1][0] == color:
            canonical[-1] = (color, canonical[-1][1] + width)
        else:
            canonical.append((color, width))
    return canonical


def roll_profile(roll):
    """The threading profile of a roll: its ordered (color_code, width)
    segments across the roll width. Continuation panels
    (`additional_panel_layouts`) are cut from the same threaded setup and so
    do not change the profile — only the roll's own `segments` matter."""
    segments = [
        (s["color_code"], s["width_in"])
        for s in roll.get("segments", [])
    ]
    return _canonical(segments)


def parse_signature(signature):
    """Parse a `layout_signature` string (e.g. "5WHI|177LIM") back into a
    canonical profile. Handy for tests and for Phase 2, which groups rolls by
    this string."""
    segments = []
    for part in signature.split("|"):
        if not part.strip():
            continue
        match = _SIGNATURE_SEGMENT.match(part)
        if not match:
            raise ValueError(f"Unparseable layout signature segment: {part!r}")
        width_text, color = match.groups()
        width = float(width_text)
        if width.is_integer():
            width = int(width)
        segments.append((color, width))
    return _canonical(segments)


def profile_width(profile):
    """Total width covered by a profile."""
    return sum(width for _, width in profile)


def _clean_number(value):
    """Return an int when a numeric result is a whole number, otherwise the
    float. Keeps costs reading as "5" rather than "5.0" when widths are whole
    inches, without hiding genuine fractional costs."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    return value


# --------------------------------------------------------------------------
# The cost model
# --------------------------------------------------------------------------
def profile_cost(profile_a, profile_b):
    """Setup change cost between two profiles: the total width of positions
    whose colour/type differs. Symmetric; identical profiles cost 0.

    Both profiles are expected to span the same total width (roll width is
    constant within an order — plan assumption 3). If they don't, the
    unmatched tail of the longer profile is counted as differing, since those
    positions must be threaded or cleared. `sequence_cost` warns when widths
    disagree so the assumption violation is visible rather than silent."""
    ia = ib = 0
    remaining_a = profile_a[ia][1] if profile_a else 0
    remaining_b = profile_b[ib][1] if profile_b else 0
    cost = 0

    while ia < len(profile_a) and ib < len(profile_b):
        step = min(remaining_a, remaining_b)
        if profile_a[ia][0] != profile_b[ib][0]:
            cost += step
        remaining_a -= step
        remaining_b -= step
        if remaining_a <= _TOL:
            ia += 1
            if ia < len(profile_a):
                remaining_a = profile_a[ia][1]
        if remaining_b <= _TOL:
            ib += 1
            if ib < len(profile_b):
                remaining_b = profile_b[ib][1]

    # Any unmatched tail (only when total widths differ) is a position present
    # in one roll and not the other, so it counts as differing.
    tail = remaining_a + sum(w for _, w in profile_a[ia + 1:]) \
        + remaining_b + sum(w for _, w in profile_b[ib + 1:])
    cost += tail
    return _clean_number(cost)


def transition_cost(roll_a, roll_b):
    """Setup change cost between two consecutive rolls (dicts as produced by
    the extractor)."""
    return profile_cost(roll_profile(roll_a), roll_profile(roll_b))


def sequence_cost(rolls, warnings=None):
    """Total setup change cost of a manufacturing sequence: the sum of the
    costs of each adjacent pair. Cost depends only on neighbouring rolls, so
    this is well defined for any order the rolls are given in.

    A sequence of 0 or 1 rolls has no transitions and costs 0."""
    profiles = [roll_profile(r) for r in rolls]
    if warnings is not None and profiles:
        widths = {_clean_number(profile_width(p)) for p in profiles}
        if len(widths) > 1:
            warnings.append(
                f"Rolls span differing total widths {sorted(widths)}; "
                "the plan assumes a constant roll width within an order. "
                "Costs still computed, treating unmatched tails as changed."
            )
    total = 0
    for a, b in zip(profiles, profiles[1:]):
        total += profile_cost(a, b)
    return _clean_number(total)


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------
def transition_breakdown(rolls):
    """Per-transition costs and summary statistics for a sequence, matching
    the "transition breakdown" the plan asks for in section 6.4: how many
    transitions are zero-cost (identical consecutive rolls) and the
    distribution of the rest."""
    profiles = [roll_profile(r) for r in rolls]
    costs = [profile_cost(a, b) for a, b in zip(profiles, profiles[1:])]

    non_zero = [c for c in costs if c > 0]
    return {
        "roll_count": len(rolls),
        "transition_count": len(costs),
        "total_cost": _clean_number(sum(costs)),
        "zero_cost_transitions": sum(1 for c in costs if c == 0),
        "max_transition_cost": _clean_number(max(costs)) if costs else 0,
        "min_transition_cost": _clean_number(min(costs)) if costs else 0,
        "mean_transition_cost": _clean_number(round(sum(costs) / len(costs), 3))
        if costs else 0,
        "mean_nonzero_transition_cost": _clean_number(
            round(sum(non_zero) / len(non_zero), 3)) if non_zero else 0,
        "transition_costs": [_clean_number(c) for c in costs],
    }


# --------------------------------------------------------------------------
# Loading extraction results
# --------------------------------------------------------------------------
def load_rolls(extracted):
    """Accept either a full extraction result dict (with a "rolls" key) or a
    bare list of roll dicts, and return the list of rolls."""
    if isinstance(extracted, dict):
        return extracted.get("rolls", [])
    return list(extracted)


def _score_file(path):
    data = json.loads(Path(path).read_text())
    rolls = load_rolls(data)
    warnings = []
    total = sequence_cost(rolls, warnings)
    breakdown = transition_breakdown(rolls)

    print(f"\n{path}")
    print(f"  rolls:              {breakdown['roll_count']}")
    if isinstance(data, dict):
        distinct = data.get("distinct_layout_count")
        if distinct is not None:
            print(f"  distinct layouts:   {distinct}")
    print(f"  transitions:        {breakdown['transition_count']}")
    print(f"  total setup cost:   {total} in  (as-extracted order)")
    print(f"  zero-cost changes:  {breakdown['zero_cost_transitions']}")
    print(f"  max / mean change:  {breakdown['max_transition_cost']} in / "
          f"{breakdown['mean_transition_cost']} in")
    for w in warnings:
        print(f"  warning: {w}")


def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+",
                        help="Extraction result JSON file(s) from extract_turf_layout.py")
    args = parser.parse_args()
    for path in args.files:
        _score_file(path)


if __name__ == "__main__":
    sys.exit(main())
