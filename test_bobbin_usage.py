#!/usr/bin/env python3
"""
Tests for per-bobbin usage over an optimised sequence.

Covers the consumption formula (lb per bobbin = w x L / 36 and its
width-cancellation property), same-colour width summing, roll_qty expansion
and run-sheet position numbering, the per-position ledger (depletion classes
in `bobbin_groups`, with late-joining positions on wider rolls carrying
their own lower depletion), swap placement against a known fresh bobbin
weight (including partial swaps that replace only the positions that run
short, and the no-swap behaviour when the weight is unknown), the item join
(yarn_lbs preferred, yarn_skus fallback), and the evaluate report
integration against the repo's real data file.

Runs with pytest, or standalone with no dependencies:

    python test_bobbin_usage.py
"""

import json

import evaluate as ev
from bobbin_usage import compute_bobbin_usage
from item_data import DEFAULT_ITEM_DATA_PATH, load_item_data


def _roll(lot, *segments, qty=1, lf=100):
    return {
        "navision_lot": lot,
        "roll_qty": qty,
        "mfg_roll_length_lf": lf,
        "layout_signature": "|".join(f"{w}{c}" for c, w in segments),
        "segments": [{"color_code": c, "width_in": w} for c, w in segments],
    }


def _item(number, color, weight, fresh=None, yarn_type="T"):
    return {number: {"item_number": number, "yarn_type": yarn_type,
                     "color_code": color, "weight_lb_per_sqft": weight,
                     "fresh_bobbin_weight_lb": fresh}}


def _extraction(*sku_colors, block="yarn_lbs"):
    """A minimal extraction carrying the item number -> colour join in the
    requested block: yarn_lbs (the preferred join) or yarn_skus (fallback)."""
    colors = [{"color_code": color, "color_name": color, "sku": sku}
              for sku, color in sku_colors]
    if block == "yarn_lbs":
        return {"source_file": "S.xlsx",
                "yarn_lbs": [{"yarn_position": "Y1", "yarn_type": "T",
                              "colors": [dict(c, lbs_needed=1.0)
                                         for c in colors]}]}
    return {"source_file": "S.xlsx",
            "yarn_skus": [{"creel_position": "Y1 Top", "yarn_type": "T",
                           "available_colors": colors}]}


def _usage(rolls, extraction, item_data):
    usage = compute_bobbin_usage(rolls, extraction, item_data)
    assert usage is not None
    return usage


# --- the consumption formula -------------------------------------------------
def test_lb_per_bobbin_is_weight_times_length_over_36():
    # w = 0.36 lb/sqft, L = 100 LF: lb per bobbin = 0.36 x 100 / 36 = 1.0.
    # Cross-check the full physics: area = (W/12) x L sqft, yarn = w x area,
    # bobbins = 3W -> 182 in x 100 LF at 0.36 consumes 546 lb over 546
    # bobbins: 1.0 lb each.
    usage = _usage([_roll("L1", ("FG", 182), lf=100)],
                   _extraction((111, "FG")), _item("111", "FG", 0.36))
    item = usage["items"][0]
    row = item["rolls"][0]
    assert row["item_width_in"] == 182
    assert row["bobbins_hanging"] == 546
    assert row["length_lf"] == 100
    assert row["lb_per_bobbin"] == 1.0
    assert item["totals"]["total_lb_per_bobbin"] == 1.0


def test_width_cancels_two_widths_same_length_same_lb_per_bobbin():
    # 12" and 100" of the item in equal-length rolls deplete each bobbin
    # identically; only the bobbin counts differ.
    rolls = [_roll("L1", ("FG", 12), ("WHI", 170), lf=250),
             _roll("L2", ("FG", 100), ("WHI", 82), lf=250)]
    usage = _usage(rolls, _extraction((111, "FG")), _item("111", "FG", 0.09))
    a, b = usage["items"][0]["rolls"]
    assert a["lb_per_bobbin"] == b["lb_per_bobbin"] == 0.09 * 250 / 36
    assert a["bobbins_hanging"] == 36
    assert b["bobbins_hanging"] == 300


def test_same_colour_segments_in_one_roll_sum_their_widths():
    # 5" + 7" of FG in one roll behaves as 12": 36 bobbins, and the per-bobbin
    # consumption is unchanged (width cancels).
    rolls = [_roll("L1", ("FG", 5), ("WHI", 170), ("FG", 7), lf=100)]
    usage = _usage(rolls, _extraction((111, "FG")), _item("111", "FG", 0.36))
    row = usage["items"][0]["rolls"][0]
    assert row["item_width_in"] == 12
    assert row["bobbins_hanging"] == 36
    assert row["lb_per_bobbin"] == 1.0


# --- expansion, positions, and length accounting ------------------------------
def test_roll_qty_expansion_positions_and_split_length():
    # Entry A (qty 2) becomes positions 1-2; B carries no FG but still holds
    # position 3; C is position 4. A's 200 LF is the entry's tufted total -
    # what conservation counts - so each of its two physical rolls tufts 100.
    rolls = [_roll("A", ("FG", 12), ("WHI", 170), qty=2, lf=200),
             _roll("B", ("WHI", 182), lf=100),
             _roll("C", ("FG", 12), ("WHI", 170), lf=100)]
    usage = _usage(rolls, _extraction((111, "FG")), _item("111", "FG", 0.36))
    rows = usage["items"][0]["rolls"]
    assert [r["position"] for r in rows] == [1, 2, 4]
    assert [r["length_lf"] for r in rows] == [100, 100, 100]
    assert [r["lb_per_bobbin"] for r in rows] == [1.0, 1.0, 1.0]
    # Cumulative accumulates across rolls (no swaps: fresh weight unknown).
    assert [r["cumulative_lb_per_bobbin"] for r in rows] == [1.0, 2.0, 3.0]
    # The item's tufted feet match what conservation counts for its entries:
    # 200 (entry A, once) + 100 (entry C).
    assert sum(r["length_lf"] for r in rows) == 300


def test_additional_panel_layouts_add_no_length():
    # A panel is cut from the same tufted length the entry already states;
    # the entry's mfg_roll_length_lf is counted once, panels add nothing.
    roll = _roll("L1", ("FG", 12), ("WHI", 170), lf=100)
    roll["additional_panel_layouts"] = [
        {"segments": [{"color_code": "FG", "width_in": 6},
                      {"color_code": "WHI", "width_in": 176}]}]
    usage = _usage([roll], _extraction((111, "FG")), _item("111", "FG", 0.36))
    rows = usage["items"][0]["rolls"]
    assert len(rows) == 1
    assert rows[0]["length_lf"] == 100
    assert rows[0]["lb_per_bobbin"] == 1.0
    # The roll's own segments define the threading: 12", not the panel's 6".
    assert rows[0]["item_width_in"] == 12


# --- swap planning -------------------------------------------------------------
def test_swap_planned_before_roll_that_remaining_yarn_cannot_cover():
    # Four rolls at 1.0 lb per bobbin each, fresh bobbin 2.5 lb: after rolls
    # 1-2 (cumulative 2.0) only 0.5 lb remains - not enough for roll 3, so
    # the swap is planned BEFORE roll 3 and the cumulative restarts there.
    rolls = [_roll(f"L{i}", ("FG", 12), ("WHI", 170), lf=100)
             for i in range(1, 5)]
    usage = _usage(rolls, _extraction((111, "FG")),
                   _item("111", "FG", 0.36, fresh=2.5))
    item = usage["items"][0]
    assert [r["swap_before"] for r in item["rolls"]] == [False, False, True, False]
    assert [r["cumulative_lb_per_bobbin"] for r in item["rolls"]] == \
        [1.0, 2.0, 1.0, 2.0]
    # A constant-width order: the swap replaces the roll's full set.
    assert [r["bobbins_swapped"] for r in item["rolls"]] == [0, 0, 36, 0]
    # One depletion class: every position is fed by all four rolls.
    assert len(item["bobbin_groups"]) == 1
    group = item["bobbin_groups"][0]
    assert group["width_in"] == 12
    assert group["bobbin_count"] == 36
    assert group["rolls_fed"] == 4
    assert group["lb_drawn_per_bobbin"] == 4.0
    assert group["swap_count"] == 1
    assert group["fresh_bobbins_consumed"] == 72
    assert group["final_remaining_lb"] == 0.5
    t = item["totals"]
    assert t["swap_count"] == 1
    assert t["total_lb_per_bobbin"] == 4.0
    assert t["final_remaining_lb_per_bobbin"] == 0.5
    # Initial hang 36 + one full fresh set 36.
    assert t["estimated_fresh_bobbins_consumed"] == 72


def test_zero_margin_exact_fit_needs_no_swap():
    # 2 rolls at 1.0 lb each against a 2.0 lb fresh bobbin: the bobbin ends
    # exactly dry at the end of roll 2, and zero margin means no swap.
    rolls = [_roll("L1", ("FG", 12), ("WHI", 170), lf=100),
             _roll("L2", ("FG", 12), ("WHI", 170), lf=100)]
    usage = _usage(rolls, _extraction((111, "FG")),
                   _item("111", "FG", 0.36, fresh=2.0))
    item = usage["items"][0]
    assert [r["swap_before"] for r in item["rolls"]] == [False, False]
    assert item["totals"]["swap_count"] == 0
    assert item["totals"]["final_remaining_lb_per_bobbin"] == 0.0


def test_unknown_fresh_weight_no_swaps_and_none_totals():
    rolls = [_roll(f"L{i}", ("FG", 12), ("WHI", 170), lf=100)
             for i in range(1, 4)]
    usage = _usage(rolls, _extraction((111, "FG")),
                   _item("111", "FG", 0.36, fresh=None))
    item = usage["items"][0]
    assert item["fresh_bobbin_weight_lb"] is None
    assert all(r["swap_before"] is False for r in item["rolls"])
    assert all(r["bobbins_swapped"] is None for r in item["rolls"])
    assert item["rolls"][-1]["cumulative_lb_per_bobbin"] == 3.0
    # The depletion groups are still reported; only the swap-dependent
    # figures are None.
    assert len(item["bobbin_groups"]) == 1
    group = item["bobbin_groups"][0]
    assert group["rolls_fed"] == 3
    assert group["lb_drawn_per_bobbin"] == 3.0
    assert group["swap_count"] is None
    assert group["fresh_bobbins_consumed"] is None
    assert group["final_remaining_lb"] is None
    t = item["totals"]
    assert t["rolls_with_item"] == 3
    assert t["total_lb_per_bobbin"] == 3.0
    assert t["swap_count"] is None
    assert t["estimated_fresh_bobbins_consumed"] is None
    assert t["final_remaining_lb_per_bobbin"] is None


def test_widening_adds_only_the_new_positions_fresh_bobbins():
    # 10" (30 bobbins), then 20" (60): the widening hangs 30 fresh bobbins on
    # the new positions; the narrower roll after it adds nothing (persisting
    # bobbins). No swaps (fresh weight is ample). Two depletion classes: the
    # front 10" fed by all three rolls, the back 10" only by the middle one.
    rolls = [_roll("L1", ("FG", 10), ("WHI", 172), lf=100),
             _roll("L2", ("FG", 20), ("WHI", 162), lf=100),
             _roll("L3", ("FG", 10), ("WHI", 172), lf=100)]
    usage = _usage(rolls, _extraction((111, "FG")),
                   _item("111", "FG", 0.36, fresh=100.0))
    item = usage["items"][0]
    t = item["totals"]
    assert t["swap_count"] == 0
    assert t["estimated_fresh_bobbins_consumed"] == 60
    front, back = item["bobbin_groups"]
    assert (front["width_in"], front["rolls_fed"]) == (10, 3)
    assert front["lb_drawn_per_bobbin"] == 3.0
    assert (back["width_in"], back["rolls_fed"]) == (10, 1)
    assert back["lb_drawn_per_bobbin"] == 1.0
    # The worst case in the totals is the deepest class, not the sum of
    # every roll's draw (the old single-track reading).
    assert t["total_lb_per_bobbin"] == 3.0


def test_varying_width_two_depletion_groups_177_then_182():
    # The defect scenario: eight rolls at 177" FG then two at 182" FG,
    # ~163 LF each, w = 0.04831. The 531 bobbins on the common 177" feed all
    # ten rolls (10 x need drawn each); the 15 bobbins on the extra 5" hang
    # only for the last two (2 x need each) - not the 10 x need the old
    # single-track model implied for all 546.
    w, lf = 0.04831, 163
    need = w * lf / 36
    rolls = ([_roll(f"A{i}", ("FG", 177), ("WHI", 5), lf=lf)
              for i in range(1, 9)]
             + [_roll(f"B{i}", ("FG", 182), lf=lf) for i in (1, 2)])
    usage = _usage(rolls, _extraction((111, "FG")), _item("111", "FG", w))
    item = usage["items"][0]
    assert len(item["bobbin_groups"]) == 2
    deep, late = item["bobbin_groups"]
    assert (deep["width_in"], deep["bobbin_count"], deep["rolls_fed"]) == \
        (177, 531, 10)
    assert abs(deep["lb_drawn_per_bobbin"] - 10 * need) < 1e-9
    assert (late["width_in"], late["bobbin_count"], late["rolls_fed"]) == \
        (5, 15, 2)
    assert abs(late["lb_drawn_per_bobbin"] - 2 * need) < 1e-9
    # Fresh weight unknown: the swap-dependent group figures are None.
    assert deep["swap_count"] is None
    assert late["fresh_bobbins_consumed"] is None
    # Totals carry the deepest class - the worst case.
    assert abs(item["totals"]["total_lb_per_bobbin"] - 10 * need) < 1e-9
    # The last two rolls' cumulative is still the deepest covered track
    # (9 x, 10 x need), not the late positions' own 1 x, 2 x.
    cums = [r["cumulative_lb_per_bobbin"] for r in item["rolls"]]
    assert abs(cums[8] - 9 * need) < 1e-9
    assert abs(cums[9] - 10 * need) < 1e-9


def test_partial_swap_replaces_only_the_positions_that_run_short():
    # 10", 20", 20" of FG at 1.0 lb per bobbin per roll, fresh 2.5 lb: the
    # front 10" (30 bobbins) has drawn 2.0 lb after two rolls and cannot
    # cover roll 3, so it swaps; the back 10" joined at roll 2, has drawn
    # only 1.0 lb, and keeps its bobbins. The swap replaces 30 bobbins, not
    # the 60 hanging on the roll.
    rolls = [_roll("L1", ("FG", 10), ("WHI", 172), lf=100),
             _roll("L2", ("FG", 20), ("WHI", 162), lf=100),
             _roll("L3", ("FG", 20), ("WHI", 162), lf=100)]
    usage = _usage(rolls, _extraction((111, "FG")),
                   _item("111", "FG", 0.36, fresh=2.5))
    item = usage["items"][0]
    assert [r["swap_before"] for r in item["rolls"]] == [False, False, True]
    assert [r["bobbins_swapped"] for r in item["rolls"]] == [0, 0, 30]
    # Roll 3's cumulative is the deepest covered position AFTER the partial
    # swap: the unswapped back 10" (2.0 lb) is now deeper than the freshly
    # swapped front (1.0 lb).
    assert [r["cumulative_lb_per_bobbin"] for r in item["rolls"]] == \
        [1.0, 2.0, 2.0]
    front, back = item["bobbin_groups"]
    assert (front["width_in"], front["bobbin_count"], front["rolls_fed"]) == \
        (10, 30, 3)
    assert front["lb_drawn_per_bobbin"] == 3.0
    assert front["swap_count"] == 1
    assert front["fresh_bobbins_consumed"] == 60
    assert front["final_remaining_lb"] == 1.5  # 2.5 - 1.0 since its swap
    assert (back["width_in"], back["bobbin_count"], back["rolls_fed"]) == \
        (10, 30, 2)
    assert back["lb_drawn_per_bobbin"] == 2.0
    assert back["swap_count"] == 0
    assert back["fresh_bobbins_consumed"] == 30
    assert back["final_remaining_lb"] == 0.5
    t = item["totals"]
    assert t["swap_count"] == 1
    assert t["estimated_fresh_bobbins_consumed"] == 90
    assert t["total_lb_per_bobbin"] == 3.0
    # The deepest class's remaining, not the unswapped back's.
    assert t["final_remaining_lb_per_bobbin"] == 1.5


def test_roll_needing_more_than_a_fresh_bobbin_warns():
    # One roll consuming 2.0 lb per bobbin against a 1.5 lb fresh bobbin:
    # even a fresh bobbin cannot cover it - surfaced, not hidden.
    rolls = [_roll("L1", ("FG", 12), ("WHI", 170), lf=200)]
    usage = _usage(rolls, _extraction((111, "FG")),
                   _item("111", "FG", 0.36, fresh=1.5))
    assert any("mid-roll" in w for w in usage["warnings"])


# --- the item join --------------------------------------------------------------
def test_returns_none_when_nothing_matches():
    rolls = [_roll("L1", ("FG", 182))]
    # No extraction at all.
    assert compute_bobbin_usage(rolls, None, _item("111", "FG", 0.36)) is None
    # Empty item data.
    assert compute_bobbin_usage(rolls, _extraction((111, "FG")), {}) is None
    # Order's SKUs don't include any listed item.
    assert compute_bobbin_usage(rolls, _extraction((999, "FG")),
                                _item("111", "FG", 0.36)) is None
    # Extraction with neither yarn_lbs nor yarn_skus.
    assert compute_bobbin_usage(rolls, {"source_file": "S.xlsx"},
                                _item("111", "FG", 0.36)) is None


def test_yarn_skus_fallback_joins_when_yarn_lbs_missing():
    rolls = [_roll("L1", ("FG", 12), ("WHI", 170), lf=100)]
    item_data = {}
    item_data.update(_item("111", "FG", 0.36))
    item_data.update(_item("222", "BLK", 0.36))  # available but never tufted
    usage = _usage(rolls, _extraction((111, "FG"), (222, "BLK"),
                                      block="yarn_skus"), item_data)
    # The availability block lists BLK, but no roll tufts it: only FG reports.
    assert [i["item_number"] for i in usage["items"]] == ["111"]
    assert usage["items"][0]["rolls"][0]["lb_per_bobbin"] == 1.0


def test_yarn_lbs_item_with_no_carrying_roll_is_still_reported():
    # The yarn_lbs block states the order needs the item; a colour reaching
    # no roll is a data mismatch item_requirements already warns about, so
    # the item reports with zero rolls rather than vanishing.
    rolls = [_roll("L1", ("WHI", 182))]
    usage = _usage(rolls, _extraction((111, "FG")),
                   _item("111", "FG", 0.36, fresh=2.0))
    item = usage["items"][0]
    assert item["rolls"] == []
    assert item["bobbin_groups"] == []
    t = item["totals"]
    assert t["rolls_with_item"] == 0
    assert t["total_lb_per_bobbin"] == 0
    assert t["swap_count"] == 0
    assert t["estimated_fresh_bobbins_consumed"] == 0
    assert t["final_remaining_lb_per_bobbin"] == 2.0


def test_items_sorted_by_item_number_and_schema_keys():
    rolls = [_roll("L1", ("FG", 12), ("WHI", 170), lf=100)]
    item_data = {}
    item_data.update(_item("222", "WHI", 0.18))
    item_data.update(_item("111", "FG", 0.36))
    usage = _usage(rolls, _extraction((111, "FG"), (222, "WHI")), item_data)
    assert [i["item_number"] for i in usage["items"]] == ["111", "222"]
    assert set(usage) == {"items", "assumptions", "warnings"}
    assert isinstance(usage["assumptions"], str)
    item = usage["items"][0]
    assert set(item) == {"item_number", "yarn_type", "color",
                         "weight_lb_per_sqft", "fresh_bobbin_weight_lb",
                         "rolls", "bobbin_groups", "totals"}
    assert set(item["rolls"][0]) == {
        "position", "navision_lot", "item_width_in", "bobbins_hanging",
        "length_lf", "lb_per_bobbin", "cumulative_lb_per_bobbin",
        "swap_before", "bobbins_swapped"}
    assert set(item["bobbin_groups"][0]) == {
        "width_in", "bobbin_count", "rolls_fed", "lb_drawn_per_bobbin",
        "swap_count", "fresh_bobbins_consumed", "final_remaining_lb"}
    assert set(item["totals"]) == {
        "rolls_with_item", "total_lb_per_bobbin", "swap_count",
        "estimated_fresh_bobbins_consumed", "final_remaining_lb_per_bobbin"}


def test_color_disagreement_uses_order_colour_and_warns():
    # The data file says 111 is BLK, the order maps it to FG: the order's
    # join decides which segments are read, and the disagreement surfaces.
    rolls = [_roll("L1", ("FG", 12), ("WHI", 170), lf=100)]
    usage = _usage(rolls, _extraction((111, "FG")), _item("111", "BLK", 0.36))
    assert usage["items"][0]["color"] == "FG"
    assert usage["items"][0]["rolls"][0]["item_width_in"] == 12
    assert any("BLK" in w and "FG" in w for w in usage["warnings"])


# --- evaluate report integration -------------------------------------------------
def test_evaluate_report_gains_bobbin_usage_for_fg_order_with_real_csv():
    if not DEFAULT_ITEM_DATA_PATH.exists():
        return  # skip: checkout without the repo data file
    real_items, _ = load_item_data()
    assert "121051" in real_items  # the seeded row
    rolls = [_roll("L1", ("FG", 177), ("WHI", 5), lf=100),
             _roll("L2", ("FG", 182), lf=150)]
    extraction = {
        "source_file": "S.xlsx",
        "rolls": rolls,
        "yarn_lbs": [{
            "yarn_position": "Y1", "yarn_type": "5040 XP+ (6Pin)",
            "colors": [
                {"color_code": "FG", "color_name": "Field Green",
                 "sku": 121051, "lbs_needed": 100.0},
                {"color_code": "WHI", "color_name": "White",
                 "sku": 999, "lbs_needed": 2.0},
            ],
        }],
    }
    report = ev.evaluate(rolls, extraction=extraction)
    usage = report["bobbin_usage"]
    assert [i["item_number"] for i in usage["items"]] == ["121051"]
    item = usage["items"][0]
    assert item["color"] == "FG"
    assert item["weight_lb_per_sqft"] == real_items["121051"]["weight_lb_per_sqft"]
    # Both rolls carry FG; per-bobbin consumption follows w x L / 36 for the
    # sequence order evaluate chose.
    assert item["totals"]["rolls_with_item"] == 2
    w = item["weight_lb_per_sqft"]
    assert abs(item["totals"]["total_lb_per_bobbin"]
               - (w * 100 / 36 + w * 150 / 36)) < 1e-12
    # Fresh weight is blank in the seeded file: no swap plan yet.
    if item["fresh_bobbin_weight_lb"] is None:
        assert item["totals"]["swap_count"] is None
    # The report stays JSON-serialisable with the new key present.
    reparsed = json.loads(ev.report_json(report))
    assert reparsed["bobbin_usage"]["items"][0]["item_number"] == "121051"


def test_evaluate_omits_bobbin_usage_when_no_item_matches():
    # An FG order whose SKUs are not in the data file: the key is absent
    # entirely, and reports without any extraction stay unchanged.
    rolls = [_roll("L1", ("FG", 182), lf=100)]
    extraction = {
        "source_file": "S.xlsx",
        "rolls": rolls,
        "yarn_lbs": [{"yarn_position": "Y1", "yarn_type": "T",
                      "colors": [{"color_code": "FG", "color_name": "FG",
                                  "sku": 999, "lbs_needed": 1.0}]}],
    }
    assert "bobbin_usage" not in ev.evaluate(rolls, extraction=extraction)
    assert "bobbin_usage" not in ev.evaluate(rolls)


def test_evaluate_carries_bobbin_usage_warnings_into_report():
    if not DEFAULT_ITEM_DATA_PATH.exists():
        return  # skip: checkout without the repo data file
    # The order maps 121051 to WHI while the data file says FG: the module
    # warns (and uses the order's colour), and that warning must land in the
    # report's warnings, mirroring the item-requirements pattern.
    rolls = [_roll("L1", ("WHI", 182), lf=100)]
    extraction = {
        "source_file": "S.xlsx",
        "rolls": rolls,
        "yarn_lbs": [{"yarn_position": "Y1", "yarn_type": "5040 XP+ (6Pin)",
                      "colors": [{"color_code": "WHI", "color_name": "White",
                                  "sku": 121051, "lbs_needed": 1.0}]}],
    }
    report = ev.evaluate(rolls, extraction=extraction)
    usage = report["bobbin_usage"]
    assert usage["items"][0]["color"] == "WHI"
    assert usage["items"][0]["totals"]["rolls_with_item"] == 1
    assert usage["warnings"], "expected a colour-disagreement warning"
    # bobbin_usage's own warnings are carried into the report's warnings.
    assert all(w in report["warnings"] for w in usage["warnings"])


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
