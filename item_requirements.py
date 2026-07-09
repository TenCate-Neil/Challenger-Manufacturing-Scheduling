#!/usr/bin/env python3
"""
Per-item batch requirements.

A turf order is tufted from up to 4 yarn types (creel positions Y1-Y4); each
yarn type + colour combination is an "item" with its own SKU. Batch planning
needs two numbers per item:

  - the total pounds of that yarn the order requires, which the workbook
    already computes and the extractor reads as the `yarn_lbs` block; and
  - a bobbin count. Every inch of a roll's width contains all yarn types, so
    an item's width in a roll is its colour's TOTAL width in that roll
    (same-colour segments in one roll are summed — 5" + 5" counts as 10").
    The creel must be dressed for the widest such requirement anywhere in the
    order, so bobbins = ceil(max width over all rolls, in inches, x 3).
    Roll length and quantity play no part.

Each entry in a roll's `additional_panel_layouts` is a panel cut from the
same threading, so it is an independent width candidate — its segments are
per-colour summed on their own, never added to the roll's own segments.

`item_requirements` joins an extraction's `yarn_lbs` block with the maximum
colour widths derived from its `rolls`, producing one entry per (yarn row,
colour) item. Mismatches between the two sources — a colour priced in the
block but absent from every roll, or tufted in a roll but missing from the
block — are surfaced as warnings, extractor-style.

Usage:
    python item_requirements.py EXTRACTED.json [EXTRACTED2.json ...]
"""

import argparse
import json
import math
import sys
from pathlib import Path

# Bobbins are dressed at 3 per inch of tufted width.
BOBBINS_PER_INCH = 3


# --------------------------------------------------------------------------
# Maximum colour width over the order's rolls
# --------------------------------------------------------------------------
def max_color_widths(rolls):
    """For each colour code, the maximum TOTAL width (inches) that colour
    occupies within any single roll or additional panel layout. Same-colour
    segments within one roll/panel are summed; each panel layout is scored on
    its own as an independent candidate. Returns {color_code: max_width_in}."""
    maxima = {}

    def consider(segments):
        totals = {}
        for seg in segments:
            code = seg.get("color_code")
            width = seg.get("width_in")
            if code is None or not isinstance(width, (int, float)):
                continue
            totals[code] = totals.get(code, 0) + width
        for code, total in totals.items():
            if total > maxima.get(code, 0):
                maxima[code] = total

    for roll in rolls:
        consider(roll.get("segments") or [])
        for panel in roll.get("additional_panel_layouts") or []:
            consider(panel.get("segments") or [])
    return maxima


# --------------------------------------------------------------------------
# Join: yarn_lbs block x roll widths -> per-item requirements
# --------------------------------------------------------------------------
def item_requirements(extraction, warnings=None):
    """Per-item batch requirements for one extracted order.

    Joins the extraction's `yarn_lbs` block (item SKUs and total pounds per
    yarn row + colour) with `max_color_widths` over its `rolls`. Returns one
    dict per item, yarn rows in block order and colours in block order:

        {"item_number": sku, "yarn_position": "Y1", "yarn_type": ...,
         "color_code": "FG", "color_name": ..., "lbs_needed": float,
         "max_width_in": float, "bobbins_required": int}

    Extractions without a `yarn_lbs` block (older JSON, joined orders) yield
    an empty list plus a warning. `warnings` is an optional list to append
    human-readable notes to, extractor-style."""
    if warnings is None:
        warnings = []

    yarn_lbs = extraction.get("yarn_lbs")
    if yarn_lbs is None:
        warnings.append(
            "Extraction carries no 'yarn_lbs' block (older extraction or "
            "joined order) - no item requirements computed.")
        return []

    widths = max_color_widths(extraction.get("rolls") or [])
    block_colors = set()
    items = []
    for yarn in yarn_lbs:
        position = yarn.get("yarn_position")
        for color in yarn.get("colors") or []:
            code = color.get("color_code")
            block_colors.add(code)
            max_width = widths.get(code, 0)
            if code not in widths:
                warnings.append(
                    f"Item requirements: {position} colour {code} is in the "
                    "yarn lbs block but never appears in any roll - "
                    "0 bobbins.")
            items.append({
                "item_number": color.get("sku"),
                "yarn_position": position,
                "yarn_type": yarn.get("yarn_type"),
                "color_code": code,
                "color_name": color.get("color_name"),
                "lbs_needed": color.get("lbs_needed"),
                "max_width_in": max_width,
                "bobbins_required": math.ceil(max_width * BOBBINS_PER_INCH),
            })

    for code in widths:
        if code not in block_colors:
            warnings.append(
                f"Item requirements: colour {code} appears in rolls but is "
                "missing from the yarn lbs block.")
    return items


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _print_items(label, items, warnings):
    print(f"\n{label}")
    if not items:
        print("  (no item requirements)")
    else:
        print(f"  {'item':>8}  {'pos':<4}{'yarn type':<22}{'colour':<8}"
              f"{'lbs needed':>12}  {'max width':>10}  {'bobbins':>7}")
        for item in items:
            lbs = item["lbs_needed"]
            lbs_text = f"{lbs:.1f}" if isinstance(lbs, (int, float)) else "-"
            print(f"  {str(item['item_number']):>8}  "
                  f"{str(item['yarn_position']):<4}"
                  f"{str(item['yarn_type']):<22}"
                  f"{str(item['color_code']):<8}"
                  f"{lbs_text:>12}  "
                  f"{item['max_width_in']:>10}  "
                  f"{item['bobbins_required']:>7}")
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
        data = json.loads(Path(path).read_text())
        warnings = []
        items = item_requirements(data, warnings)
        _print_items(path, items, warnings)


if __name__ == "__main__":
    sys.exit(main())
