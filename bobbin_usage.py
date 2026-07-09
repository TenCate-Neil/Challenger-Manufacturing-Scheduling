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

Width cancelling means every bobbin of an item depletes at the same rate
regardless of which inch position it feeds; only tufted length matters. That
is what makes a *uniform-position* model honest: one running total per item
stands for every one of its bobbins. Same-colour segments within a roll sum
their widths (5" + 5" behaves as 10" - more bobbins, same lb each).

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
When the item's fresh-bobbin net weight is known, a swap is planned *before*
any roll that the remaining yarn cannot cover - zero margin, exactly at need.
The planners' informal ~10% buffer (brief section 8) is deliberately not
baked in here; the report shows the un-buffered numbers and the margin policy
stays a planning decision. When the fresh weight is unknown (blank in the
data file), consumption still accumulates but no swaps are planned.

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
    "Uniform-position model: every bobbin of an item depletes at the same "
    "rate (per-bobbin consumption is width-independent), bobbins are assumed "
    "to persist on the creel across rolls and setup changes, and swaps are "
    "planned with zero margin - a swap is placed before a roll only when the "
    "remaining yarn cannot cover that roll."
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


# --------------------------------------------------------------------------
# Per-item usage over the expanded sequence
# --------------------------------------------------------------------------
def _item_usage(item, color_code, physical_rolls, warnings):
    """One item's roll-by-roll bobbin usage over the expanded sequence.
    Returns the item's report entry (schema in `compute_bobbin_usage`)."""
    weight = item["weight_lb_per_sqft"]
    fresh = item["fresh_bobbin_weight_lb"]

    rows = []
    cumulative = 0
    swap_count = 0
    # Fresh bobbins consumed, under the uniform-position assumption: we do
    # not track individual creel positions, so we estimate as if the item
    # always occupies the same stretch of creel. The first item-carrying roll
    # hangs a fresh set (3 x its item width); when a later roll widens the
    # item beyond the widest seen so far, only the new positions hang fresh
    # bobbins (3 x the increase) - positions already hanging are assumed to
    # persist, including through narrower rolls in between; and a planned
    # swap replaces that roll's full set (3 x its item width) with fresh
    # bobbins. Narrow-then-wide sequences that in reality re-hang partials
    # are counted as persisting, so this is an estimate, not an inventory
    # count.
    fresh_consumed = 0
    running_max_bobbins = 0
    warned_lots = set()

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

        bobbins = int(round(BOBBINS_PER_INCH * width))
        lb_per_bobbin = weight * length_lf / 36
        swap_before = False
        if fresh is not None:
            if lb_per_bobbin > fresh + _TOL:
                lot = roll.get("navision_lot")
                if ("over", lot) not in warned_lots:
                    warned_lots.add(("over", lot))
                    warnings.append(
                        f"Bobbin usage: roll {lot} needs {lb_per_bobbin:.3f} "
                        f"lb per bobbin of item {item['item_number']}, more "
                        f"than a fresh bobbin holds ({fresh} lb) - a "
                        "mid-roll same-batch splice will be required.")
            if fresh - cumulative + _TOL < lb_per_bobbin:
                swap_before = True
                swap_count += 1
                cumulative = lb_per_bobbin
            else:
                cumulative += lb_per_bobbin
        else:
            cumulative += lb_per_bobbin

        if swap_before:
            fresh_consumed += bobbins  # the roll's full set hangs fresh
        elif bobbins > running_max_bobbins:
            fresh_consumed += bobbins - running_max_bobbins
        running_max_bobbins = max(running_max_bobbins, bobbins)

        lot = roll.get("navision_lot")
        rows.append({
            "position": position,
            "navision_lot": str(lot) if lot is not None else None,
            "item_width_in": _clean_number(width),
            "bobbins_hanging": bobbins,
            "length_lf": _clean_number(length_lf),
            "lb_per_bobbin": lb_per_bobbin,
            "cumulative_lb_per_bobbin": cumulative,
            "swap_before": swap_before,
        })

    return {
        "item_number": item["item_number"],
        "yarn_type": item["yarn_type"],
        "color": color_code,
        "weight_lb_per_sqft": weight,
        "fresh_bobbin_weight_lb": fresh,
        "rolls": rows,
        "totals": {
            "rolls_with_item": len(rows),
            "total_lb_per_bobbin": sum(r["lb_per_bobbin"] for r in rows),
            "swap_count": swap_count if fresh is not None else None,
            "estimated_fresh_bobbins_consumed":
                fresh_consumed if fresh is not None else None,
            "final_remaining_lb_per_bobbin":
                fresh - cumulative if fresh is not None else None,
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
    sheet), a `swap_before` plan when the fresh bobbin weight is known, and
    totals as described in the module docstring."""
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
              f"total lb/bobbin: {t['total_lb_per_bobbin']:.3f}   "
              f"swaps: {swap_text}   fresh bobbins: {bobbin_text}")
        if item["rolls"]:
            print(f"    {'pos':>5}  {'lot':<14}{'width':>7}  {'bobbins':>7}  "
                  f"{'LF':>8}  {'lb/bobbin':>10}  {'cumulative':>10}  swap")
            for row in item["rolls"]:
                print(f"    {row['position']:>5}  "
                      f"{str(row['navision_lot']):<14}"
                      f"{row['item_width_in']:>7}  "
                      f"{row['bobbins_hanging']:>7}  "
                      f"{row['length_lf']:>8}  "
                      f"{row['lb_per_bobbin']:>10.4f}  "
                      f"{row['cumulative_lb_per_bobbin']:>10.4f}  "
                      f"{'SWAP' if row['swap_before'] else ''}")
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
