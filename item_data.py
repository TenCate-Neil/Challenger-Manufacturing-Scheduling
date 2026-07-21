#!/usr/bin/env python3
"""
Per-item bobbin data loader.

Bobbin-level planning (see
`docs/leftover_batch_utilisation_and_bobbin_planning.md`, sections 6 and 7) needs
two numbers per item that the order workbooks do not carry:

  - `weight_lb_per_sqft` — the item's weight rate, in pounds of yarn per
    square foot of tufted area. It converts a roll's tufted length into
    per-bobbin yarn consumption (`bobbin_usage.py`).
  - `fresh_bobbin_weight_lb` — the net yarn weight of a fresh bobbin of that
    item. It turns cumulative consumption into planned swap points. This
    figure comes from the floor (question 7 of the next-phase brief) and may
    not be known yet, so it is allowed to be blank — blank means None, and
    downstream reporting simply skips swap planning for that item.

The data lives in `data/item_bobbin_data.csv`, one row per item (yarn type +
colour, keyed by item/SKU number), and is expected to be maintained by hand —
edited on GitHub, appended to as rates are measured. The loader is therefore
deliberately forgiving: blank lines and '#' comment lines are skipped, and a
malformed row produces a warning and is dropped, never an exception. A
missing file returns an empty table plus a warning, which downstream treats
as "feature off".

Usage:
    python item_data.py [CSV_PATH]
"""

import argparse
import csv
import sys
from pathlib import Path

# The repo's own copy of the table, next to this module. Callers that pass no
# path get this one; a checkout without it simply has no item data.
DEFAULT_ITEM_DATA_PATH = Path(__file__).resolve().parent / "data" / "item_bobbin_data.csv"

_EXPECTED_HEADER = ["item_number", "yarn_type", "color_code",
                    "weight_lb_per_sqft", "fresh_bobbin_weight_lb"]


def _parse_weight(text, label, line, warnings):
    """A strictly positive float from a CSV field, or None with a warning.
    Rejects the unparsable and the non-positive alike — a zero or negative
    weight rate or bobbin weight is a data-entry error, and computing with it
    would silently produce nonsense downstream."""
    try:
        value = float(text)
    except ValueError:
        warnings.append(
            f"Item data row {line}: {label} {text!r} is not a number - "
            "row skipped.")
        return None
    if not value > 0:  # also catches NaN
        warnings.append(
            f"Item data row {line}: {label} {text!r} must be positive - "
            "row skipped.")
        return None
    return value


def load_item_data(path=None):
    """Load the per-item bobbin data table. Returns (items, warnings) where
    `items` maps the item number *as a string* to:

        {"item_number": str, "yarn_type": str, "color_code": str,
         "weight_lb_per_sqft": float, "fresh_bobbin_weight_lb": float | None}

    `path` defaults to DEFAULT_ITEM_DATA_PATH. The loader never raises on bad
    content: blank lines and lines whose first field starts with '#' are
    skipped silently; malformed rows, unparsable or non-positive weights, and
    duplicate item numbers each produce a warning string and the row is
    dropped (first occurrence wins for duplicates). A blank
    `fresh_bobbin_weight_lb` is valid and loads as None — the figure is
    expected to be filled in later. A missing or unreadable file returns
    ({}, [warning])."""
    if path is None:
        path = DEFAULT_ITEM_DATA_PATH
    path = Path(path)
    warnings = []
    items = {}

    try:
        # utf-8-sig: tolerate the BOM that web-based editors sometimes add.
        handle = open(path, newline="", encoding="utf-8-sig")
    except OSError as exc:
        return {}, [f"Item bobbin data file not available ({exc}) - "
                    "bobbin usage reporting is off."]

    with handle:
        reader = csv.reader(handle)
        header_seen = False
        for row in reader:
            line = reader.line_num
            fields = [field.strip() for field in row]
            if not any(fields):
                continue  # blank line
            if fields[0].startswith("#"):
                continue  # comment line
            if not header_seen:
                header_seen = True
                if fields[0] == _EXPECTED_HEADER[0]:
                    if fields != _EXPECTED_HEADER:
                        warnings.append(
                            f"Item data row {line}: header differs from the "
                            f"expected {','.join(_EXPECTED_HEADER)} - columns "
                            "are read positionally.")
                    continue  # header row consumed
                warnings.append(
                    f"Item data row {line}: no header row found - reading "
                    "rows positionally as "
                    f"{','.join(_EXPECTED_HEADER)}.")
                # fall through: this row is data
            if len(fields) != len(_EXPECTED_HEADER):
                warnings.append(
                    f"Item data row {line}: expected "
                    f"{len(_EXPECTED_HEADER)} fields, got {len(fields)} - "
                    "row skipped.")
                continue
            item_number, yarn_type, color_code, weight_text, fresh_text = fields
            if not item_number:
                warnings.append(
                    f"Item data row {line}: blank item number - row skipped.")
                continue
            weight = _parse_weight(weight_text, "weight_lb_per_sqft",
                                   line, warnings)
            if weight is None:
                continue
            if fresh_text == "":
                fresh = None  # not yet measured; swap planning stays off
            else:
                fresh = _parse_weight(fresh_text, "fresh_bobbin_weight_lb",
                                      line, warnings)
                if fresh is None:
                    continue
            if item_number in items:
                warnings.append(
                    f"Item data row {line}: duplicate item number "
                    f"{item_number} - first occurrence kept, row skipped.")
                continue
            items[item_number] = {
                "item_number": item_number,
                "yarn_type": yarn_type,
                "color_code": color_code,
                "weight_lb_per_sqft": weight,
                "fresh_bobbin_weight_lb": fresh,
            }

    return items, warnings


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", nargs="?", default=None,
                        help="Path to the item bobbin data CSV "
                             f"(default: {DEFAULT_ITEM_DATA_PATH})")
    args = parser.parse_args()

    items, warnings = load_item_data(args.path)
    print(f"{args.path or DEFAULT_ITEM_DATA_PATH}")
    if not items:
        print("  (no item data loaded)")
    else:
        print(f"  {'item':>8}  {'yarn type':<22}{'colour':<8}"
              f"{'lb/sqft':>10}  {'fresh bobbin lb':>15}")
        for number in sorted(items):
            item = items[number]
            fresh = item["fresh_bobbin_weight_lb"]
            fresh_text = f"{fresh}" if fresh is not None else "-"
            print(f"  {item['item_number']:>8}  {item['yarn_type']:<22}"
                  f"{item['color_code']:<8}"
                  f"{item['weight_lb_per_sqft']:>10}  {fresh_text:>15}")
    for w in warnings:
        print(f"  warning: {w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
