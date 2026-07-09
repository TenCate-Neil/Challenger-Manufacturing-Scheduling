#!/usr/bin/env python3
"""
Per-bobbin yarn consumption and swap planning over an optimised sequence.

This implements the consumption side of the bobbin simulator described in
`Next Phase To Be Done/leftover_batch_utilisation_and_bobbin_planning.md`
(sections 5-7): given the optimised roll sequence, the extraction it came
from, and the per-item bobbin data table (`item_data.py`), predict how much
yarn each bobbin feeds per roll, where along the run a bobbin must be
swapped for a fresh one, and roughly how many fresh bobbins the run consumes.
The floor rule it serves is "bobbins are never allowed to run dry mid-roll";
today that is managed by operator judgement, and this turns it into a
printed plan.

The physics
-----------
An item is a yarn type + colour, keyed by its item/SKU number. Bobbins are
dressed at 3 per inch of tufted width, per item. For a roll of length L feet
in which the item occupies W inches of width:

    area tufted     = (W / 12) x L        square feet
    yarn consumed   = w x (W / 12) x L    lb, where w is lb per sqft
    bobbins feeding = 3 x W
    lb per bobbin   = w x L / 36          - the width cancels

Width cancelling means every bobbin *fed by the same rolls* depletes at the
same rate; only tufted length matters. But real orders vary an item's width
across rolls, so different inch positions can be fed by different subsets of
the sequence: the positions on the extra inches of a wider roll join late
and deplete less. This module therefore keeps a ledger per inch position,
with layouts aligned from the front of the machine (inch 0 at the front -
the fixed-creel alignment the setup cost model already assumes): a position
covered by a roll draws that roll's lb per bobbin, a position the roll does
not cover is untouched by it. Positions with an identical coverage history
(the same set of sequence steps feeding them) carry identical numbers, so
the report groups them into *depletion classes* (`bobbin_groups`).
Same-colour segments within a roll sum their widths (5" + 5" behaves as
10" - more bobbins, same lb each) but keep their own inch positions.

Length accounting
-----------------
Per-bobbin consumption must count exactly the tufted feet that the Phase 4
conservation checks count, or the two reports would disagree about the same
run. Conservation totals linear feet as the sum of each roll entry's
`mfg_roll_length_lf` - counted once per entry, with `additional_panel_layouts`
adding nothing (a panel is another piece cut from the same tufted length the
entry already states, not extra tufting). This module therefore takes an
entry's `mfg_roll_length_lf` as the entry's whole tufted length and splits it
evenly across its `roll_qty` physical rolls, so the expanded sequence tufts
exactly the feet conservation accounts for. Position numbering runs over the
same expansion (one position per physical roll, quantities expanded), so it
matches the printed run sheet.

Swap planning
-------------
When the item's fresh-bobbin net weight is known, a position's bobbins are
swapped *before* any roll that their remaining yarn cannot cover - zero
margin, exactly at need - and only the positions that actually run short are
swapped (`bobbins_swapped` per roll). The planners' informal ~10% buffer
(brief section 8) is deliberately not baked in here; the report shows the
un-buffered numbers and the margin policy stays a planning decision. When
the fresh weight is unknown (blank in the data file), consumption still
accumulates but no swaps are planned.

Usage:
    python bobbin_usage.py EXTRACTED.json [EXTRACTED2.json ...]
              [--item-data CSV]

Runs the Phase 3 optimiser on each extraction and prints the per-item bobbin
usage of the optimised sequence.
"""

import argparse
import json
import sys
from pathlib import Path

from item_data import load_item_data
from roll_sequencing import _clean_number, load_rolls

# Bobbins are dressed at 3 per inch of tufted width, per item
# (docs/batch_assignment_context.md section 4.2).
BOBBINS_PER_INCH = 3

# Guard against floating-point noise only. This is not a planning margin:
# a bobbin predicted to end a roll exactly dry is still allowed to feed it.
_TOL = 1e-9

_ASSUMPTIONS = (
    "Per-position model: consumption is tracked per inch position, with "
    "layouts aligned from the front of the machine (fixed creel positions - "
    "the same alignment the setup cost model assumes), so positions joining "
    "only on wider rolls carry their own, lower depletion. Bobbins are "
    "assumed to persist on the creel across rolls and setup changes, and "
    "swaps are planned with zero margin - a position's bobbins are swapped "
    "before a roll only when their remaining yarn cannot cover that roll."
)


# --------------------------------------------------------------------------
# Joining item numbers to the order's colours
# --------------------------------------------------------------------------
def _item_key(sku):
    """An item/SKU number as a canonical string key. Workbook SKUs arrive as
    ints (121051) or strings ("145190A"); the data file always reads them as
    strings, so both sides normalise the same way. A float that is a whole
    number (an Excel artefact) keys as its integer form."""
    if isinstance(sku, float) and sku.is_integer():
        sku = int(sku)
    return str(sku)


def _order_item_colors(extraction, warnings):
    """The order's own item number -> colour mapping, as
    {item_key: {"color_code": ..., "yarn_type": ...}}.

    Uses the same join `item_requirements.py` uses: the `yarn_lbs` block
    (item SKU and colour per yarn row) when the extraction carries one,
    falling back to the `yarn_skus` creel block when it does not (older
    extractions). Returns ({}, source=None) when the extraction offers
    neither. The second element names the block used, so callers can apply
    block-specific rules."""
    if not isinstance(extraction, dict):
        return {}, None

    yarn_lbs = extraction.get("yarn_lbs")
    if yarn_lbs is not None:
        rows = [(yarn.get("yarn_type"), yarn.get("colors") or [])
                for yarn in yarn_lbs]
        source = "yarn_lbs"
    elif extraction.get("yarn_skus") is not None:
        rows = [(creel.get("yarn_type"), creel.get("available_colors") or [])
                for creel in extraction.get("yarn_skus")]
        source = "yarn_skus"
    else:
        return {}, None

    mapping = {}
    for yarn_type, colors in rows:
        for color in colors:
            sku = color.get("sku")
            if sku is None:
                continue
            key = _item_key(sku)
            entry = {"color_code": color.get("color_code"),
                     "yarn_type": yarn_type}
            seen = mapping.get(key)
            if seen is None:
                mapping[key] = entry
            elif seen["color_code"] != entry["color_code"]:
                warnings.append(
                    f"Bobbin usage: item {key} maps to two colours in the "
                    f"order ({seen['color_code']} and {entry['color_code']}) "
                    "- keeping the first.")
    return mapping, source


# --------------------------------------------------------------------------
# Expanding the sequence to physical rolls
# --------------------------------------------------------------------------
def _reps_of(qty):
    """How many physical rolls a sequence entry stands for (its roll_qty),
    defaulting to 1 when the quantity is missing or not a whole number.
    Matches the run sheet's expansion (app._reps_of), so positions here name
    the same physical rolls the printed sheet numbers."""
    if isinstance(qty, bool):
        return 1
    if isinstance(qty, int) and qty > 0:
        return qty
    if isinstance(qty, float) and qty.is_integer() and qty > 0:
        return int(qty)
    return 1


def _expand_physical_rolls(sequence_rolls, warnings):
    """The sequence expanded to physical rolls, in order, as a list of
    (position, roll_entry, length_lf) tuples. Positions are 1-based over the
    whole expansion. Each entry's `mfg_roll_length_lf` is split evenly across
    its physical rolls (see "Length accounting" in the module docstring); an
    entry without a numeric length counts as 0 LF, with a warning, exactly as
    the conservation totals would ignore it."""
    physical = []
    position = 0
    for roll in sequence_rolls or []:
        reps = _reps_of(roll.get("roll_qty"))
        length = roll.get("mfg_roll_length_lf")
        if isinstance(length, (int, float)) and not isinstance(length, bool):
            per_roll_lf = length / reps
        else:
            per_roll_lf = 0
            warnings.append(
                f"Bobbin usage: roll {roll.get('navision_lot')} has no "
                "numeric mfg_roll_length_lf - counted as 0 LF.")
        for _ in range(reps):
            position += 1
            physical.append((position, roll, per_roll_lf))
    return physical


def _color_width_in(roll, color_code):
    """The total width (inches) the colour occupies in the roll's own
    segments. Same-colour segments are summed. The roll's own segments define
    the threading for its whole tufted length; additional panel layouts are
    cut from that same threading and are not summed in."""
    total = 0
    for seg in roll.get("segments") or []:
        if seg.get("color_code") != color_code:
            continue
        width = seg.get("width_in")
        if isinstance(width, (int, float)) and not isinstance(width, bool):
            total += width
    return total


def _panel_only_color(roll, color_code):
    """True when the colour appears in one of the roll's additional panel
    layouts but not in the roll's own segments - an inconsistency worth
    surfacing, since a panel is supposed to be cut from the same threading
    the roll's own segments describe."""
    if _color_width_in(roll, color_code) > 0:
        return False
    for panel in roll.get("additional_panel_layouts") or []:
        for seg in panel.get("segments") or []:
            if seg.get("color_code") == color_code:
                return True
    return False


def _coverage_intervals(roll, color_code):
    """The inch intervals the colour occupies in the roll's own segments, as
    a merged, ascending list of (start, end) pairs. Positions are absolute
    inches from the front of the machine (inch 0 at the front): offsets
    accumulate over the ordered segment widths, so layouts align at the
    front - the fixed-creel alignment the whole cost model assumes. A
    non-numeric segment width shifts nothing and covers nothing (the same
    treatment `_color_width_in` gives it)."""
    intervals = []
    offset = 0
    for seg in roll.get("segments") or []:
        width = seg.get("width_in")
        if not isinstance(width, (int, float)) or isinstance(width, bool):
            continue
        if seg.get("color_code") == color_code and width > 0:
            intervals.append((offset, offset + width))
        offset += width
    intervals.sort()
    merged = []
    for start, end in intervals:
        if merged and start <= merged[-1][1] + _TOL:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _depletion_classes(coverages):
    """Inch positions grouped by identical coverage history. `coverages`
    lists, per item-carrying sequence step, that step's coverage intervals
    (from `_coverage_intervals`). Positions fed by exactly the same set of
    steps deplete identically, so one ledger entry stands for all of them.
    Returns {history: width_in} where history is the ascending tuple of step
    indices feeding that class and width_in the total inches in it."""
    points = sorted({point for coverage in coverages
                     for interval in coverage for point in interval})
    # Collapse float-noise duplicates so breakpoints that should coincide
    # (the same offset reached via different segment splits) do.
    breaks = []
    for point in points:
        if not breaks or point - breaks[-1] > _TOL:
            breaks.append(point)
    classes = {}
    for start, end in zip(breaks, breaks[1:]):
        mid = (start + end) / 2
        history = tuple(
            i for i, coverage in enumerate(coverages)
            if any(a - _TOL <= mid <= b + _TOL for a, b in coverage))
        if history:
            classes[history] = classes.get(history, 0) + (end - start)
    return classes


# --------------------------------------------------------------------------
# Per-item usage over the expanded sequence
# --------------------------------------------------------------------------
def _item_usage(item, color_code, physical_rolls, warnings):
    """One item's roll-by-roll bobbin usage over the expanded sequence, with
    depletion tracked per inch position (grouped into depletion classes).
    Returns the item's report entry (schema in `compute_bobbin_usage`)."""
    weight = item["weight_lb_per_sqft"]
    fresh = item["fresh_bobbin_weight_lb"]
    warned_lots = set()

    # First pass: the sequence steps that carry the item, each with its inch
    # coverage from the front of the machine.
    steps = []
    for position, roll, length_lf in physical_rolls:
        if _panel_only_color(roll, color_code):
            lot = roll.get("navision_lot")
            if lot not in warned_lots:
                warned_lots.add(lot)
                warnings.append(
                    f"Bobbin usage: roll {lot} carries colour {color_code} "
                    "only in an additional panel layout, not in its own "
                    "segments - not counted (panels are cut from the roll's "
                    "own threading).")
            continue
        width = _color_width_in(roll, color_code)
        if width <= 0:
            continue
        lb_per_bobbin = weight * length_lf / 36
        if fresh is not None and lb_per_bobbin > fresh + _TOL:
            lot = roll.get("navision_lot")
            if ("over", lot) not in warned_lots:
                warned_lots.add(("over", lot))
                warnings.append(
                    f"Bobbin usage: roll {lot} needs {lb_per_bobbin:.3f} "
                    f"lb per bobbin of item {item['item_number']}, more "
                    f"than a fresh bobbin holds ({fresh} lb) - a "
                    "mid-roll same-batch splice will be required.")
        steps.append({
            "position": position,
            "roll": roll,
            "length_lf": length_lf,
            "width": width,
            "lb_per_bobbin": lb_per_bobbin,
            "coverage": _coverage_intervals(roll, color_code),
        })

    # The per-position ledger, one entry per depletion class: positions with
    # an identical coverage history carry identical numbers.
    classes = _depletion_classes([step["coverage"] for step in steps])
    ledger = {history: {"cum": 0.0, "swaps": 0, "drawn": 0.0}
              for history in classes}
    fed_by_step = [[] for _ in steps]
    for history in classes:
        for i in history:
            fed_by_step[i].append(history)

    rows = []
    swap_events = 0
    for i, step in enumerate(steps):
        lb_per_bobbin = step["lb_per_bobbin"]
        swapped_width = 0
        for history in fed_by_step[i]:
            state = ledger[history]
            if fresh is not None and fresh - state["cum"] + _TOL < lb_per_bobbin:
                # These positions' remaining yarn cannot cover this roll:
                # swap just their bobbins before it (zero margin).
                state["swaps"] += 1
                state["cum"] = 0.0
                swapped_width += classes[history]
            state["cum"] += lb_per_bobbin
            state["drawn"] += lb_per_bobbin
        swap_before = swapped_width > 0
        if swap_before:
            swap_events += 1
        # The row's cumulative is the DEEPEST-drawn covered position's
        # cumulative since its last swap - for constant-width orders every
        # position shares one history and this equals the old single-track
        # value.
        deepest = max((ledger[history]["cum"] for history in fed_by_step[i]),
                      default=lb_per_bobbin)

        lot = step["roll"].get("navision_lot")
        rows.append({
            "position": step["position"],
            "navision_lot": str(lot) if lot is not None else None,
            "item_width_in": _clean_number(step["width"]),
            "bobbins_hanging": int(round(BOBBINS_PER_INCH * step["width"])),
            "length_lf": _clean_number(step["length_lf"]),
            "lb_per_bobbin": lb_per_bobbin,
            "cumulative_lb_per_bobbin": deepest,
            "swap_before": swap_before,
            "bobbins_swapped":
                int(round(BOBBINS_PER_INCH * swapped_width))
                if fresh is not None else None,
        })

    # One report entry per depletion class, deepest-drawn first. The fresh
    # bobbins consumed per class - the initial hang plus one set per swap -
    # is exact under the front-alignment assumption, since each class's
    # positions are tracked individually.
    groups = []
    for history, class_width in classes.items():
        state = ledger[history]
        bobbin_count = int(round(BOBBINS_PER_INCH * class_width))
        groups.append({
            "width_in": _clean_number(class_width),
            "bobbin_count": bobbin_count,
            "rolls_fed": len(history),
            "lb_drawn_per_bobbin": state["drawn"],
            "swap_count": state["swaps"] if fresh is not None else None,
            "fresh_bobbins_consumed":
                bobbin_count * (1 + state["swaps"])
                if fresh is not None else None,
            "final_remaining_lb":
                fresh - state["cum"] if fresh is not None else None,
        })
    groups.sort(key=lambda g: (-g["lb_drawn_per_bobbin"], -g["width_in"]))
    deepest_group = groups[0] if groups else None

    return {
        "item_number": item["item_number"],
        "yarn_type": item["yarn_type"],
        "color": color_code,
        "weight_lb_per_sqft": weight,
        "fresh_bobbin_weight_lb": fresh,
        "rolls": rows,
        "bobbin_groups": groups,
        "totals": {
            "rolls_with_item": len(rows),
            # Worst case: the deepest depletion class's total draw per
            # bobbin over the whole order (across swaps). Equals the old
            # single-track sum for constant-width orders.
            "total_lb_per_bobbin":
                deepest_group["lb_drawn_per_bobbin"] if deepest_group else 0,
            # Swap events: rolls before which ANY position swaps.
            "swap_count": swap_events if fresh is not None else None,
            "estimated_fresh_bobbins_consumed":
                sum(g["fresh_bobbins_consumed"] for g in groups)
                if fresh is not None else None,
            # What remains on the deepest class's hanging bobbins at order
            # end (fresh weight minus its cumulative since its last swap).
            "final_remaining_lb_per_bobbin":
                (deepest_group["final_remaining_lb"]
                 if deepest_group else fresh)
                if fresh is not None else None,
        },
    }


def compute_bobbin_usage(sequence_rolls, extraction, item_data):
    """Per-bobbin usage of an optimised sequence, for every item that appears
    both in `item_data` (the loaded per-item bobbin table, see
    `item_data.load_item_data`) and in the order.

    `sequence_rolls` is the optimised roll order - the extraction's roll
    dicts, reordered (e.g. `sequencer.optimise(...)["sequence"]`).
    `extraction` is the full extraction dict; it supplies the item number ->
    colour join (`yarn_lbs` preferred, `yarn_skus` as fallback) and may be
    None, in which case no item can match. Items joined via `yarn_lbs` are
    reported even when their colour reaches no roll (the block states the
    order needs them); items joined via the `yarn_skus` availability block
    are reported only when their colour is actually tufted in the sequence.

    Returns None when no item matches, otherwise:

        {"items": [...per-item entries, sorted by item number...],
         "assumptions": str,
         "warnings": [str, ...]}

    where each item entry carries the roll-by-roll usage (`position` numbers
    every physical roll of the whole expanded sequence, so it matches the run
    sheet), a `swap_before` plan with per-roll `bobbins_swapped` counts when
    the fresh bobbin weight is known, the item's depletion classes under
    `bobbin_groups` (one entry per set of inch positions with an identical
    coverage history, deepest-drawn first), and totals as described in the
    module docstring."""
    if not item_data:
        return None

    warnings = []
    order_items, source = _order_item_colors(extraction, warnings)
    matched = {key: order_items[key] for key in order_items if key in item_data}
    if not matched:
        return None

    physical_rolls = _expand_physical_rolls(sequence_rolls, warnings)

    items = []
    for key in sorted(matched):
        data = item_data[key]
        color_code = matched[key]["color_code"]
        if data["color_code"] != color_code:
            warnings.append(
                f"Bobbin usage: item {key} is colour {data['color_code']} in "
                f"the bobbin data file but {color_code} in the order - using "
                "the order's colour.")
        entry = _item_usage(data, color_code, physical_rolls, warnings)
        if source == "yarn_skus" and not entry["rolls"]:
            # The creel block lists what is *available*, not what the order
            # uses; an available colour tufted in no roll is not part of the
            # order.
            continue
        items.append(entry)

    if not items:
        return None
    return {
        "items": items,
        "assumptions": _ASSUMPTIONS,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _print_usage(label, usage):
    print(f"\n{label}")
    if usage is None:
        print("  (no item in the bobbin data file appears in this order)")
        return
    for item in usage["items"]:
        fresh = item["fresh_bobbin_weight_lb"]
        fresh_text = f"{fresh} lb" if fresh is not None else "unknown"
        print(f"  item {item['item_number']}  {item['yarn_type']}  "
              f"colour {item['color']}  "
              f"({item['weight_lb_per_sqft']} lb/sqft, fresh bobbin "
              f"{fresh_text})")
        t = item["totals"]
        swap_text = t["swap_count"] if t["swap_count"] is not None else "-"
        bobbin_text = (t["estimated_fresh_bobbins_consumed"]
                       if t["estimated_fresh_bobbins_consumed"] is not None
                       else "-")
        print(f"    rolls with item: {t['rolls_with_item']}   "
              f"deepest lb/bobbin: {t['total_lb_per_bobbin']:.3f}   "
              f"swaps: {swap_text}   fresh bobbins: {bobbin_text}")
        if item["rolls"]:
            print(f"    {'pos':>5}  {'lot':<14}{'width':>7}  {'bobbins':>7}  "
                  f"{'LF':>8}  {'lb/bobbin':>10}  {'cumulative':>10}  swap")
            for row in item["rolls"]:
                swap = ""
                if row["swap_before"]:
                    swap = f"SWAP {row['bobbins_swapped']}"
                print(f"    {row['position']:>5}  "
                      f"{str(row['navision_lot']):<14}"
                      f"{row['item_width_in']:>7}  "
                      f"{row['bobbins_hanging']:>7}  "
                      f"{row['length_lf']:>8}  "
                      f"{row['lb_per_bobbin']:>10.4f}  "
                      f"{row['cumulative_lb_per_bobbin']:>10.4f}  "
                      f"{swap}")
        if item["bobbin_groups"]:
            print("    depletion groups (positions with an identical "
                  "coverage history):")
            print(f"    {'width':>7}  {'bobbins':>7}  {'rolls':>5}  "
                  f"{'lb drawn':>10}  {'swaps':>5}  {'fresh':>6}  "
                  f"{'left lb':>8}")
            for g in item["bobbin_groups"]:
                swaps = (str(g["swap_count"])
                         if g["swap_count"] is not None else "-")
                consumed = (str(g["fresh_bobbins_consumed"])
                            if g["fresh_bobbins_consumed"] is not None
                            else "-")
                left = (f"{g['final_remaining_lb']:.4f}"
                        if g["final_remaining_lb"] is not None else "-")
                print(f"    {g['width_in']:>7}  {g['bobbin_count']:>7}  "
                      f"{g['rolls_fed']:>5}  "
                      f"{g['lb_drawn_per_bobbin']:>10.4f}  "
                      f"{swaps:>5}  {consumed:>6}  {left:>8}")
    print(f"  assumptions: {usage['assumptions']}")
    for w in usage["warnings"]:
        print(f"  warning: {w}")


def main():
    from sequencer import optimise

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+",
                        help="Extraction result JSON file(s) from extract_turf_layout.py")
    parser.add_argument("--item-data", default=None,
                        help="Path to the per-item bobbin data CSV "
                             "(default: data/item_bobbin_data.csv next to "
                             "this module).")
    args = parser.parse_args()

    item_data, data_warnings = load_item_data(args.item_data)
    for w in data_warnings:
        print(f"warning: {w}")

    for path in args.files:
        data = json.loads(Path(path).read_text())
        result = optimise(load_rolls(data))
        usage = compute_bobbin_usage(
            result["sequence"],
            data if isinstance(data, dict) else None,
            item_data)
        _print_usage(path, usage)
    return 0


if __name__ == "__main__":
    sys.exit(main())
