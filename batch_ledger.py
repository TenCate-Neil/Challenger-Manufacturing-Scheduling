#!/usr/bin/env python3
"""
Batch-aware bobbin ledger over an optimised sequence.

This is the next slice of the leftover-batch plan (`Next Phase To Be Done/
leftover_batch_utilisation_and_bobbin_planning.md`, sections 2, 7 and 8):
given the batches available in inventory, assign one batch to each of the
order's items and then simulate the whole optimised run bobbin by bobbin, so
that at order end the leftover is a known quantity — how many untouched full
bobbins remain in the batch and how many partials are left, with the pounds
remaining on each — instead of something discovered on the floor.

Inputs
------
- The optimised sequence and its extraction (the same pair `bobbin_usage`
  works from). The consumption rate needs no new measurement: it is derived
  from the workbook's own yarn lbs block as item lbs ÷ the item's tufted
  area across the order (brief section 6), and the per-roll draw per bobbin
  is then `rate × length_lf / 36` exactly as in `bobbin_usage`.
- A batch workbook: one row per batch with `batch_number`, `item_number`,
  `number_of_bobbins`, `weight_per_bobbin` and `total_batch_weight`. Every
  bobbin in a batch is taken to be full at `weight_per_bobbin` (the file
  carries no per-bobbin remainders yet); a stated total that disagrees with
  count × weight is warned about and count × weight is what the ledger uses.
- A buffer ratio (default 10%, brief section 8): the planners' informal
  margin made explicit. A bobbin is only trusted to feed a demand D when it
  holds at least D × (1 + buffer), both when deciding whether a hanging
  bobbin survives the next roll and when picking a partial to re-mount.

The creel rule this simulates
-----------------------------
Bobbins hang while their inch positions carry the item and come off at the
setup change that gives those positions to another colour — the same
positional model as the setup cost. Removed bobbins are not discarded: they
are kept creel-side and re-mounted at a later setup change wherever their
remaining yarn covers that position's upcoming demand (the contiguous run of
rolls it is being mounted for) plus buffer. So when 182" of field green
narrows to 177" and later widens back, the bobbins that came off are the
ones that go back on — not fresh ones. Selection is best-fit: the *smallest*
sufficient partial is used first, preserving fuller bobbins (and the batch's
untouched ones) for deeper demands. Bobbins are never allowed to run dry
mid-roll (floor rule, brief section 5): a hanging bobbin that cannot cover
the next roll plus buffer is swapped out beforehand, and its replacement is
chosen by the same reuse-first policy. Within-batch splicing is allowed,
cross-batch is not — and since an item has exactly one batch in an order,
every partial in the pool is by construction spliceable.

The ledger answers, per item: which batch was assigned and whether it covers
the requirement (pounds with buffer, bobbin count), how many fresh bobbins
the run actually consumes, how many mounts were served by re-used partials,
and the end state — untouched full bobbins, partials still hanging on the
creel, and partials creel-side — each with the pounds left on it.

Usage:
    python batch_ledger.py EXTRACTED.json [EXTRACTED2.json ...]
              --batches BATCHES.xlsx [--buffer 0.1]
"""

import argparse
import json
import sys
from pathlib import Path

from bobbin_usage import (
    BOBBINS_PER_INCH,
    _color_width_in,
    _coverage_intervals,
    _depletion_classes,
    _expand_physical_rolls,
    _item_key,
)
from item_requirements import item_requirements
from roll_sequencing import _clean_number

# The planners' informal ~10% margin (brief section 8), as an explicit,
# tunable parameter rather than a rounding habit.
DEFAULT_BUFFER_RATIO = 0.10

# Floating-point guard only — planning margin comes from the buffer ratio.
_TOL = 1e-9

_EXPECTED_HEADER = ["batch_number", "item_number", "number_of_bobbins",
                    "weight_per_bobbin", "total_batch_weight"]

_ASSUMPTIONS = (
    "Bobbins hang while their inch positions carry the item and come off at "
    "the setup change that re-threads those positions; removed partials are "
    "kept creel-side and re-mounted (smallest sufficient first) wherever "
    "their remaining yarn covers the position's upcoming contiguous demand "
    "plus the buffer, before any fresh bobbin is drawn from the batch. A "
    "hanging bobbin that cannot cover the next roll plus the buffer is "
    "swapped out beforehand (bobbins never run dry mid-roll). Every batch "
    "bobbin starts full at the stated weight per bobbin; the consumption "
    "rate is derived from the workbook's own yarn lbs block (item lbs / "
    "item area), so total draw equals the stated pounds by construction."
)


# --------------------------------------------------------------------------
# Batch workbook loader
# --------------------------------------------------------------------------
def _positive_number(value, label, row, warnings):
    """A strictly positive number from a workbook cell, or None with a
    warning — zero or negative supply figures are data-entry errors that
    would make the simulation nonsense."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        warnings.append(
            f"Batch workbook row {row}: {label} {value!r} is not a number - "
            "row skipped.")
        return None
    if not value > 0:  # also catches NaN
        warnings.append(
            f"Batch workbook row {row}: {label} {value!r} must be positive - "
            "row skipped.")
        return None
    return value


def load_batch_workbook(source):
    """Load the batch inventory workbook. `source` is a path or a bytes /
    file-like object (a Streamlit upload). Returns `(batches, warnings)`
    where `batches` maps the item number *as a string* (same normalisation
    as the bobbin data table) to that item's batches, each:

        {"batch_number": str, "item_number": str, "bobbin_count": int,
         "weight_per_bobbin_lb": float, "total_weight_lb": float}

    `total_weight_lb` is always count × weight per bobbin — the quantity the
    per-bobbin simulation can actually honour; a stated total that disagrees
    is warned about, not silently trusted. The loader is forgiving in the
    style of `item_data.load_item_data`: a malformed row warns and is
    dropped, never raises, and an unreadable file returns `({}, [warning])`.
    Columns are matched by header name so column order does not matter."""
    warnings = []
    try:
        import openpyxl
    except ImportError as exc:
        return {}, [f"Batch workbook needs openpyxl ({exc}) - "
                    "batch ledger is off."]

    import io
    if isinstance(source, (bytes, bytearray)):
        source = io.BytesIO(bytes(source))
    try:
        workbook = openpyxl.load_workbook(source, data_only=True,
                                          read_only=True)
    except Exception as exc:  # noqa: BLE001 - any unreadable file
        return {}, [f"Batch workbook could not be read ({exc}) - "
                    "batch ledger is off."]

    sheet = workbook.worksheets[0]
    rows = sheet.iter_rows(values_only=True)
    header = next(rows, None)
    if header is None:
        return {}, ["Batch workbook is empty - batch ledger is off."]

    names = [str(cell).strip().lower() if cell is not None else ""
             for cell in header]
    columns = {}
    for wanted in _EXPECTED_HEADER:
        if wanted in names:
            columns[wanted] = names.index(wanted)
        else:
            warnings.append(
                f"Batch workbook: column '{wanted}' not found in the header "
                f"row {tuple(names)} - batch ledger is off.")
    if len(columns) != len(_EXPECTED_HEADER):
        return {}, warnings

    def cell(row_values, name):
        index = columns[name]
        return row_values[index] if index < len(row_values) else None

    batches = {}
    seen = set()
    for row_number, values in enumerate(rows, start=2):
        if values is None or not any(v is not None for v in values):
            continue  # blank row
        batch_number = cell(values, "batch_number")
        item_number = cell(values, "item_number")
        if batch_number is None or item_number is None:
            warnings.append(
                f"Batch workbook row {row_number}: blank batch or item "
                "number - row skipped.")
            continue
        count = _positive_number(cell(values, "number_of_bobbins"),
                                 "number_of_bobbins", row_number, warnings)
        per_bobbin = _positive_number(cell(values, "weight_per_bobbin"),
                                      "weight_per_bobbin", row_number,
                                      warnings)
        if count is None or per_bobbin is None:
            continue
        if not float(count).is_integer():
            warnings.append(
                f"Batch workbook row {row_number}: number_of_bobbins "
                f"{count!r} is not a whole number - row skipped.")
            continue
        count = int(count)

        effective_total = count * per_bobbin
        stated_total = cell(values, "total_batch_weight")
        if isinstance(stated_total, (int, float)) \
                and not isinstance(stated_total, bool) \
                and abs(stated_total - effective_total) > 0.01:
            warnings.append(
                f"Batch workbook row {row_number}: total_batch_weight "
                f"{stated_total} differs from bobbins x weight "
                f"({_clean_number(effective_total)}) - using bobbins x "
                "weight (the file carries no per-bobbin remainders).")

        batch_number = str(batch_number).strip()
        item_key = _item_key(item_number)
        if (batch_number, item_key) in seen:
            warnings.append(
                f"Batch workbook row {row_number}: duplicate batch "
                f"{batch_number} for item {item_key} - first occurrence "
                "kept, row skipped.")
            continue
        seen.add((batch_number, item_key))
        batches.setdefault(item_key, []).append({
            "batch_number": batch_number,
            "item_number": item_key,
            "bobbin_count": count,
            "weight_per_bobbin_lb": per_bobbin,
            "total_weight_lb": _clean_number(effective_total),
        })

    workbook.close()
    return batches, warnings


# --------------------------------------------------------------------------
# Batch assignment — one batch per item, never split
# --------------------------------------------------------------------------
def _choose_batch(candidates, lbs_with_buffer, bobbins_required, warnings,
                  item_label):
    """Pick the one batch this item uses for the whole order. Among batches
    covering both the buffered pounds and the bobbin count, the smallest
    such batch wins (best fit — large batches are preserved for demands
    that need them; the pool-wide assignment objective is still an open
    question in the docs, so this is a stated provisional rule). When no
    batch covers the requirement the largest batch is taken and the
    shortfall surfaces as an infeasible assignment plus a warning — the
    ledger still runs so the planner can see how far it falls short."""
    feasible = [b for b in candidates
                if b["total_weight_lb"] + _TOL >= lbs_with_buffer
                and b["bobbin_count"] >= bobbins_required]
    if feasible:
        return min(feasible, key=lambda b: b["total_weight_lb"]), True
    chosen = max(candidates, key=lambda b: b["total_weight_lb"])
    warnings.append(
        f"Batch ledger: no batch of item {item_label} covers the "
        f"requirement ({_clean_number(lbs_with_buffer)} lb with buffer, "
        f"{bobbins_required} bobbins) - simulating with the largest "
        f"available batch {chosen['batch_number']}.")
    return chosen, False


# --------------------------------------------------------------------------
# The per-item bobbin ledger
# --------------------------------------------------------------------------
def _color_area_sqft(physical_rolls, color_code):
    """The colour's tufted area across the expanded sequence, in square
    feet: each physical roll contributes its colour width / 12 × its share
    of the entry's length. Matches the length accounting `bobbin_usage`
    uses, so the derived rate reproduces the workbook's pounds exactly."""
    area = 0.0
    for _position, roll, length_lf in physical_rolls:
        width = _color_width_in(roll, color_code)
        if width > 0 and length_lf:
            area += (width / 12.0) * length_lf
    return area


def _simulate_item(color_code, physical_rolls, rate_lb_per_sqft, batch,
                   buffer_ratio, warnings, item_label):
    """Run one item's bobbins through the whole expanded sequence.

    Positions are grouped into depletion classes (sets of inch positions
    covered by exactly the same rolls — they mount, draw and release
    together). State per class is the list of its hanging bobbins' remaining
    pounds; removed bobbins join a creel-side pool shared across the item.
    Releases happen before mounts at each transition, so a bobbin taken off
    one stretch can go straight back on at another in the same stop (a
    move). Returns (rows, totals, end_state)."""
    per_bobbin = batch["weight_per_bobbin_lb"]
    margin = 1.0 + buffer_ratio

    coverages = [_coverage_intervals(roll, color_code)
                 for _position, roll, _length in physical_rolls]
    draws = [rate_lb_per_sqft * length_lf / 36.0
             for _position, _roll, length_lf in physical_rolls]

    # One ledger entry per depletion class; `histories` index the steps
    # (physical rolls) that cover the class's positions.
    classes = []
    for history, width in _depletion_classes(coverages).items():
        covered = set(history)
        # Demand of the contiguous run starting at each covered step: the
        # pounds a bobbin mounted there must feed before its positions next
        # change colour. Computed backwards so each entry is O(1).
        run_demand = {}
        for step in sorted(history, reverse=True):
            run_demand[step] = draws[step] + run_demand.get(step + 1, 0.0)
        classes.append({
            "width": width,
            "bobbins": int(round(BOBBINS_PER_INCH * width)),
            "covered": covered,
            "run_demand": run_demand,
            "hanging": None,  # list of remaining lb once mounted
        })

    pool = []  # creel-side partials, each its remaining lb
    fresh_remaining = batch["bobbin_count"]
    stats = {"fresh_bobbins_used": 0, "reused_mounts": 0, "swap_events": 0,
             "bobbins_swapped": 0, "shortfall_bobbins": 0, "unmet_lb": 0.0,
             "total_drawn_lb": 0.0}
    warned_short = False

    def take_bobbin(demand):
        """One bobbin able to feed `demand` (buffered): the smallest
        sufficient creel-side partial, else a fresh bobbin from the batch,
        else — batch exhausted — the largest partial (or a phantom fresh
        bobbin) with a warning, so the run is still simulated end to end."""
        nonlocal fresh_remaining, warned_short
        needed = demand * margin
        best = None
        for index, remaining in enumerate(pool):
            if remaining + _TOL >= needed and (
                    best is None or remaining < pool[best]):
                best = index
        if best is not None:
            stats["reused_mounts"] += 1
            return pool.pop(best)
        if fresh_remaining > 0:
            fresh_remaining -= 1
            stats["fresh_bobbins_used"] += 1
            return per_bobbin
        # The batch has no full bobbins left: fall back rather than stop.
        if not warned_short:
            warned_short = True
            warnings.append(
                f"Batch ledger: batch {batch['batch_number']} of item "
                f"{item_label} runs out of full bobbins during the run - "
                "the remainder is simulated with the largest partials / "
                "extra bobbins and the shortfall is reported.")
        if pool:
            stats["reused_mounts"] += 1
            return pool.pop(max(range(len(pool)), key=pool.__getitem__))
        stats["shortfall_bobbins"] += 1
        stats["fresh_bobbins_used"] += 1
        return per_bobbin

    rows = []
    for step, (position, roll, _length) in enumerate(physical_rolls):
        active = {id(c): c for c in classes if step in c["covered"]}
        draw = draws[step]
        mounted_before = (stats["fresh_bobbins_used"], stats["reused_mounts"])
        released = 0
        swapped = 0

        # 1. Releases first: positions this roll gives to another colour
        #    free their bobbins into the creel-side pool, available for
        #    re-mounting in this same stop.
        for entry in classes:
            if id(entry) not in active and entry["hanging"] is not None:
                pool.extend(entry["hanging"])
                released += len(entry["hanging"])
                entry["hanging"] = None

        # 2. Swaps on continuing positions: a hanging bobbin that cannot
        #    cover this roll plus buffer comes off now (never dry mid-roll)
        #    and its replacement must cover the class's remaining run.
        for entry in active.values():
            if entry["hanging"] is None:
                continue
            remaining_run = entry["run_demand"][step]
            for index, remaining in enumerate(entry["hanging"]):
                if remaining + _TOL < draw * margin:
                    pool.append(remaining)
                    entry["hanging"][index] = take_bobbin(remaining_run)
                    swapped += 1
        if swapped:
            stats["swap_events"] += 1
            stats["bobbins_swapped"] += swapped

        # 3. Mounts on newly covered positions, sized to the contiguous run
        #    they are being mounted for.
        for entry in active.values():
            if entry["hanging"] is None:
                remaining_run = entry["run_demand"][step]
                entry["hanging"] = [take_bobbin(remaining_run)
                                    for _ in range(entry["bobbins"])]

        # 4. Tuft the roll: every hanging bobbin of the item draws the same
        #    pounds (width cancels — bobbin_usage section "The physics").
        for entry in active.values():
            hanging = entry["hanging"]
            for index, remaining in enumerate(hanging):
                if remaining + _TOL < draw:
                    stats["unmet_lb"] += draw - remaining
                    hanging[index] = 0.0
                else:
                    hanging[index] = remaining - draw
            stats["total_drawn_lb"] += entry["bobbins"] * draw

        mounted_fresh = stats["fresh_bobbins_used"] - mounted_before[0]
        mounted_reused = stats["reused_mounts"] - mounted_before[1]
        width = sum(entry["width"] for entry in active.values())
        if width > 0 or released or mounted_fresh or mounted_reused:
            lot = roll.get("navision_lot")
            rows.append({
                "position": position,
                "navision_lot": str(lot) if lot is not None else None,
                "item_width_in": _clean_number(width),
                "bobbins_hanging":
                    sum(entry["bobbins"] for entry in active.values()),
                "lb_per_bobbin": draw if width > 0 else 0,
                "mounted_fresh": mounted_fresh,
                "mounted_reused": mounted_reused,
                "released": released,
                "swapped": swapped,
            })

    on_creel = [remaining for entry in classes
                if entry["hanging"] is not None
                for remaining in entry["hanging"]]
    end_state = _end_state(batch, fresh_remaining, on_creel, pool,
                           stats["shortfall_bobbins"])
    return rows, stats, end_state


def _group_partials(remainders, where):
    """Partials grouped by remaining pounds (rounded to 4 decimals so bobbins
    with an identical history collapse into one row), fullest first."""
    groups = {}
    for remaining in remainders:
        key = round(remaining, 4)
        groups[key] = groups.get(key, 0) + 1
    return [{"remaining_lb": _clean_number(key), "bobbins": count,
             "where": where}
            for key, count in sorted(groups.items(), reverse=True)]


def _end_state(batch, fresh_remaining, on_creel, pool, shortfall):
    """What is left of the batch when the order ends: untouched full bobbins
    still in inventory, partials hanging on the creel, and partials removed
    creel-side — each with the pounds remaining on it."""
    per_bobbin = batch["weight_per_bobbin_lb"]
    leftovers = (_group_partials(on_creel, "on creel")
                 + _group_partials(pool, "creel-side (removed)"))
    total_left = (fresh_remaining * per_bobbin
                  + sum(on_creel) + sum(pool))
    return {
        "full_bobbins_remaining": fresh_remaining,
        "full_bobbin_weight_lb": _clean_number(per_bobbin),
        "partial_bobbins": leftovers,
        "partial_bobbin_count": len(on_creel) + len(pool),
        "total_leftover_lb": _clean_number(total_left),
        # > 0 only when the batch could not supply the run (also warned).
        "shortfall_bobbins": shortfall,
    }


# --------------------------------------------------------------------------
# The ledger over a whole order
# --------------------------------------------------------------------------
def compute_batch_ledger(sequence_rolls, extraction, batches,
                         buffer_ratio=DEFAULT_BUFFER_RATIO):
    """The batch-aware bobbin ledger of an optimised sequence.

    `sequence_rolls` is the optimised roll order (e.g.
    `sequencer.optimise(...)["sequence"]`), `extraction` the full extraction
    dict (its `yarn_lbs` block supplies both the item join and the pounds
    the rate is derived from), and `batches` the `load_batch_workbook`
    result. Returns None when no batch matches any item of the order,
    otherwise:

        {"buffer_ratio": float,
         "items": [...one entry per matched item...],
         "unmatched_items": [...item numbers in the order with no batch...],
         "assumptions": str, "warnings": [str, ...]}

    Each item entry carries the assigned batch, the requirement checks
    (pounds with buffer, bobbins), the roll-by-roll ledger rows (mounts
    split into fresh vs re-used, releases, swaps), the run totals, and the
    end state (full bobbins remaining plus every partial's remaining
    pounds, on creel and creel-side). One batch serves an item for the
    whole run — in combined mode that is the whole combined run, which
    treats the joined orders as sharing that batch."""
    if not batches:
        return None

    warnings = []
    requirements = item_requirements(extraction or {}, warnings=warnings)
    if not requirements:
        # No yarn_lbs block: no demand side to check batches against.
        return None

    physical_rolls = _expand_physical_rolls(sequence_rolls, warnings)

    items = []
    unmatched = []
    for requirement in requirements:
        item_label = _item_key(requirement["item_number"])
        candidates = batches.get(item_label)
        if not candidates:
            unmatched.append(item_label)
            continue

        color_code = requirement["color_code"]
        lbs_needed = requirement["lbs_needed"]
        bobbins_required = requirement["bobbins_required"]
        if not isinstance(lbs_needed, (int, float)) \
                or isinstance(lbs_needed, bool) or lbs_needed <= 0:
            warnings.append(
                f"Batch ledger: item {item_label} has no usable pounds "
                "figure in the yarn lbs block - not simulated.")
            continue
        area = _color_area_sqft(physical_rolls, color_code)
        if area <= 0:
            warnings.append(
                f"Batch ledger: item {item_label} (colour {color_code}) is "
                "tufted in no roll - batch left untouched.")
            rate = None
        else:
            rate = lbs_needed / area

        lbs_with_buffer = lbs_needed * (1.0 + buffer_ratio)
        batch, feasible = _choose_batch(
            candidates, lbs_with_buffer, bobbins_required, warnings,
            item_label)

        if rate is None:
            rows, stats = [], {
                "fresh_bobbins_used": 0, "reused_mounts": 0,
                "swap_events": 0, "bobbins_swapped": 0,
                "shortfall_bobbins": 0, "unmet_lb": 0.0,
                "total_drawn_lb": 0.0}
            end_state = _end_state(batch, batch["bobbin_count"], [], [], 0)
        else:
            rows, stats, end_state = _simulate_item(
                color_code, physical_rolls, rate, batch, buffer_ratio,
                warnings, item_label)

        items.append({
            "item_number": requirement["item_number"],
            "yarn_type": requirement["yarn_type"],
            "color_code": color_code,
            "color_name": requirement.get("color_name"),
            "batch": dict(batch),
            "requirements": {
                "lbs_needed": _clean_number(lbs_needed),
                "lbs_with_buffer": _clean_number(lbs_with_buffer),
                "bobbins_required": bobbins_required,
                "lbs_feasible":
                    batch["total_weight_lb"] + _TOL >= lbs_with_buffer,
                "bobbins_feasible":
                    batch["bobbin_count"] >= bobbins_required,
                "feasible": feasible,
            },
            "derived_rate_lb_per_sqft": rate,
            "rolls": rows,
            "totals": {
                "fresh_bobbins_used": stats["fresh_bobbins_used"],
                "reused_mounts": stats["reused_mounts"],
                "swap_events": stats["swap_events"],
                "bobbins_swapped": stats["bobbins_swapped"],
                "total_drawn_lb": _clean_number(stats["total_drawn_lb"]),
                "shortfall_bobbins": stats["shortfall_bobbins"],
                "unmet_lb": _clean_number(stats["unmet_lb"]),
            },
            "end_state": end_state,
        })

    if not items:
        return None
    return {
        "buffer_ratio": buffer_ratio,
        "items": items,
        "unmatched_items": unmatched,
        "assumptions": _ASSUMPTIONS,
        "warnings": warnings,
    }


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------
def _print_ledger(label, ledger):
    print(f"\n{label}")
    if ledger is None:
        print("  (no batch in the workbook matches an item of this order)")
        return
    print(f"  buffer: {ledger['buffer_ratio'] * 100:.0f}%")
    for item in ledger["items"]:
        batch = item["batch"]
        req = item["requirements"]
        verdict = "OK" if req["feasible"] else "INSUFFICIENT"
        print(f"  item {item['item_number']}  {item['yarn_type']}  "
              f"colour {item['color_code']}  <-  batch "
              f"{batch['batch_number']} ({batch['bobbin_count']} bobbins x "
              f"{batch['weight_per_bobbin_lb']} lb = "
              f"{batch['total_weight_lb']} lb)  [{verdict}]")
        print(f"    needs {req['lbs_needed']} lb "
              f"({req['lbs_with_buffer']} with buffer) and "
              f"{req['bobbins_required']} bobbins")
        t = item["totals"]
        print(f"    fresh bobbins used: {t['fresh_bobbins_used']}   "
              f"re-used mounts: {t['reused_mounts']}   "
              f"swaps: {t['bobbins_swapped']} in {t['swap_events']} stops   "
              f"drawn: {t['total_drawn_lb']:.1f} lb")
        e = item["end_state"]
        print(f"    left over: {e['full_bobbins_remaining']} full bobbins @ "
              f"{e['full_bobbin_weight_lb']} lb + "
              f"{e['partial_bobbin_count']} partials = "
              f"{e['total_leftover_lb']:.1f} lb")
        for group in e["partial_bobbins"]:
            print(f"      {group['bobbins']} bobbin(s) @ "
                  f"{group['remaining_lb']} lb  ({group['where']})")
    if ledger["unmatched_items"]:
        print(f"  items with no batch in the workbook: "
              f"{', '.join(ledger['unmatched_items'])}")
    for w in ledger["warnings"]:
        print(f"  warning: {w}")


def main():
    from roll_sequencing import load_rolls
    from sequencer import optimise

    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("files", nargs="+",
                        help="Extraction result JSON file(s) from "
                             "extract_turf_layout.py")
    parser.add_argument("--batches", required=True,
                        help="Batch inventory workbook (.xlsx): batch_number, "
                             "item_number, number_of_bobbins, "
                             "weight_per_bobbin, total_batch_weight.")
    parser.add_argument("--buffer", type=float, default=DEFAULT_BUFFER_RATIO,
                        help="Planning buffer ratio applied to pounds checks "
                             "and per-bobbin swap/reuse decisions "
                             f"(default {DEFAULT_BUFFER_RATIO}).")
    args = parser.parse_args()

    batches, batch_warnings = load_batch_workbook(args.batches)
    for w in batch_warnings:
        print(f"warning: {w}")

    for path in args.files:
        data = json.loads(Path(path).read_text())
        result = optimise(load_rolls(data))
        ledger = compute_batch_ledger(
            result["sequence"],
            data if isinstance(data, dict) else None,
            batches, buffer_ratio=args.buffer)
        _print_ledger(path, ledger)
    return 0


if __name__ == "__main__":
    sys.exit(main())
