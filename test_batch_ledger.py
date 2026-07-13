#!/usr/bin/env python3
"""
Tests for the batch-aware bobbin ledger (`batch_ledger.py`).

The simulation tests are dependency-free (plain dicts in, plain dicts out).
The workbook-loader tests need openpyxl to build a real .xlsx in memory and
skip cleanly where it is not installed, in the style of the app tests.

Runs with pytest, or standalone:

    python test_batch_ledger.py
"""

import io

from batch_ledger import (
    DEFAULT_BUFFER_RATIO,
    compute_batch_ledger,
    load_batch_workbook,
)

try:
    import openpyxl
    _HAVE_OPENPYXL = True
except ImportError:
    _HAVE_OPENPYXL = False


# --------------------------------------------------------------------------
# Fixtures
# --------------------------------------------------------------------------
def _roll(lot, *segs, qty=1, lf=100):
    return {"navision_lot": lot, "roll_qty": qty, "mfg_roll_length_lf": lf,
            "total_mfg_sf": 1500,
            "layout_signature": "|".join(f"{w}{c}" for c, w in segs),
            "segments": [{"color_code": c, "width_in": w} for c, w in segs]}


def _extraction(rolls, items):
    """A synthetic extraction: `items` is a list of
    (sku, color_code, lbs_needed) carried in a single yarn row."""
    return {
        "source_file": "S.xlsx",
        "rolls": rolls,
        "yarn_lbs": [{"yarn_position": "Y1", "yarn_type": "5040 XP+",
                      "colors": [{"color_code": code, "color_name": code,
                                  "sku": sku, "lbs_needed": lbs}
                                 for sku, code, lbs in items]}],
    }


def _batch(number, item, count, per_bobbin):
    return {"batch_number": number, "item_number": str(item),
            "bobbin_count": count, "weight_per_bobbin_lb": per_bobbin,
            "total_weight_lb": count * per_bobbin}


def _workbook_bytes(rows, header=("batch_number", "item_number",
                                  "number_of_bobbins", "weight_per_bobbin",
                                  "total_batch_weight")):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(list(header))
    for row in rows:
        ws.append(list(row))
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# The worked example from planning discussion: 182" FG, then 5" WHI +
# 177" FG, then 177" FG + 5" WHI. The 15 FG bobbins that come off when
# white arrives must go back on when FG widens again, and the 15 WHI
# bobbins must move from the front edge to the back edge — no fresh
# bobbins after the first mounts.
_EXAMPLE_ROLLS = [_roll("L1", ("FG", 182)),
                  _roll("L2", ("WHI", 5), ("FG", 177)),
                  _roll("L3", ("FG", 177), ("WHI", 5))]
_EXAMPLE_EXTRACTION = _extraction(
    _EXAMPLE_ROLLS, [(121051, "FG", 1000.0), (121054, "WHI", 30.0)])
_EXAMPLE_BATCHES = {"121051": [_batch("B-FG", 121051, 600, 4.0)],
                    "121054": [_batch("B1234", 121054, 50, 4.0)]}


def _example_ledger(buffer_ratio=DEFAULT_BUFFER_RATIO):
    return compute_batch_ledger(_EXAMPLE_ROLLS, _EXAMPLE_EXTRACTION,
                                _EXAMPLE_BATCHES, buffer_ratio=buffer_ratio)


def _item(ledger, number):
    return next(i for i in ledger["items"] if i["item_number"] == number)


# --------------------------------------------------------------------------
# The reuse example
# --------------------------------------------------------------------------
def test_example_reuses_removed_bobbins_instead_of_fresh():
    ledger = _example_ledger()
    fg = _item(ledger, 121051)
    # The initial hang is fresh; nothing after it is.
    assert fg["totals"]["fresh_bobbins_used"] == 546
    # Roll 3's 5" of FG is fed by the 15 partials that came off earlier,
    # not by fresh bobbins.
    assert fg["totals"]["reused_mounts"] == 15
    by_position = {r["position"]: r for r in fg["rolls"]}
    assert by_position[3]["mounted_reused"] == 15
    assert by_position[3]["mounted_fresh"] == 0
    # Roll 2 releases the 15 bobbins white displaces.
    assert by_position[2]["released"] == 15


def test_example_moves_white_bobbins_across_the_creel():
    ledger = _example_ledger()
    whi = _item(ledger, 121054)
    # 15 fresh at roll 2; at roll 3 the same bobbins move to the far edge —
    # released and re-mounted in the same stop, no fresh involved.
    assert whi["totals"]["fresh_bobbins_used"] == 15
    assert whi["totals"]["reused_mounts"] == 15
    by_position = {r["position"]: r for r in whi["rolls"]}
    assert by_position[3]["released"] == 15
    assert by_position[3]["mounted_reused"] == 15
    assert by_position[3]["mounted_fresh"] == 0


def test_example_best_fit_prefers_the_most_depleted_sufficient_partial():
    ledger = _example_ledger()
    fg = _item(ledger, 121051)
    # Two partial groups exist when roll 3 mounts: 15 released after one
    # roll's draw and 15 released after two. Best fit takes the more
    # depleted set, so the fresher 15 remain creel-side at order end.
    fg_area_sqft = (182 + 177 + 177) * 100 / 12
    per_roll = (1000.0 / fg_area_sqft) * 100 / 36  # w * L / 36
    off_creel = [g for g in fg["end_state"]["partial_bobbins"]
                 if g["where"] == "creel-side (removed)"]
    assert len(off_creel) == 1
    assert off_creel[0]["bobbins"] == 15
    assert abs(off_creel[0]["remaining_lb"] - (4.0 - per_roll)) < 1e-3


def test_example_end_state_and_conservation():
    ledger = _example_ledger()
    for item in ledger["items"]:
        batch = item["batch"]
        end = item["end_state"]
        totals = item["totals"]
        # Total draw equals the workbook's stated pounds (the rate is
        # derived from them, so this is exact by construction).
        assert abs(totals["total_drawn_lb"]
                   - item["requirements"]["lbs_needed"]) < 1e-6
        # No pound appears or vanishes: initial batch weight = drawn +
        # leftover (full + every partial).
        initial = batch["bobbin_count"] * batch["weight_per_bobbin_lb"]
        assert abs(initial - totals["total_drawn_lb"]
                   - end["total_leftover_lb"]) < 1e-6
        assert end["shortfall_bobbins"] == 0
    fg_end = _item(ledger, 121051)["end_state"]
    # 600 - 546 fresh mounts = 54 untouched full bobbins.
    assert fg_end["full_bobbins_remaining"] == 54
    # 531 still hanging + 15 creel-side = 546 partials in play.
    assert fg_end["partial_bobbin_count"] == 546
    wheres = {g["where"] for g in fg_end["partial_bobbins"]}
    assert wheres == {"on creel", "creel-side (removed)"}


# --------------------------------------------------------------------------
# Buffer behaviour
# --------------------------------------------------------------------------
def test_buffer_blocks_a_partial_that_only_just_covers():
    # One item, two rolls: 10" of RED on a long roll, nothing on the second,
    # then 10" again on a third. The partials removed after roll 1 hold
    # exactly the pounds roll 3 draws: acceptable with no buffer, rejected
    # (fresh used instead) with a 10% buffer.
    rolls = [_roll("L1", ("RED", 10), ("FG", 172), lf=90),
             _roll("L2", ("FG", 182), lf=90),
             _roll("L3", ("RED", 10), ("FG", 172), lf=90)]
    # Choose the pounds so a fresh bobbin holds exactly two rolls' draw:
    # draw per bobbin per roll = w * 90 / 36; make w s.t. draw = 2.0 lb.
    w = 2.0 * 36 / 90  # lb/sqft
    area = (10 / 12) * 90 * 2  # RED tufted on rolls 1 and 3
    red_lbs = w * area
    extraction = _extraction(rolls, [(200, "RED", red_lbs),
                                     (201, "FG", 1.0)])
    batches = {"200": [_batch("B-RED", 200, 100, 4.0)]}

    no_buffer = compute_batch_ledger(rolls, extraction, batches,
                                     buffer_ratio=0.0)
    red = _item(no_buffer, 200)
    # remaining 2.0 lb == roll 3's 2.0 lb draw: reused with no buffer...
    assert red["totals"]["reused_mounts"] == 30
    assert red["totals"]["fresh_bobbins_used"] == 30

    buffered = compute_batch_ledger(rolls, extraction, batches,
                                    buffer_ratio=0.10)
    red = _item(buffered, 200)
    # ...but 2.0 < 2.0 x 1.1, so the buffered run mounts fresh instead.
    assert red["totals"]["reused_mounts"] == 0
    assert red["totals"]["fresh_bobbins_used"] == 60


def test_swap_before_a_roll_the_hanging_bobbin_cannot_cover():
    # One 5" stripe across three long rolls; a fresh bobbin holds only two
    # rolls' draw, so the third roll forces a proactive swap (never dry
    # mid-roll), replaced from the batch.
    rolls = [_roll("L1", ("RED", 5), ("FG", 177), lf=90),
             _roll("L2", ("RED", 5), ("FG", 177), lf=90),
             _roll("L3", ("RED", 5), ("FG", 177), lf=90)]
    w = 2.0 * 36 / 90
    red_lbs = w * (5 / 12) * 90 * 3
    extraction = _extraction(rolls, [(200, "RED", red_lbs),
                                     (201, "FG", 1.0)])
    batches = {"200": [_batch("B-RED", 200, 100, 4.0)]}
    ledger = compute_batch_ledger(rolls, extraction, batches,
                                  buffer_ratio=0.0)
    red = _item(ledger, 200)
    by_position = {r["position"]: r for r in red["rolls"]}
    assert by_position[3]["swapped"] == 15
    assert red["totals"]["swap_events"] == 1
    assert red["totals"]["bobbins_swapped"] == 15
    # 15 initial + 15 replacements, and the swapped-out (now empty with no
    # buffer) partials sit creel-side at the end.
    assert red["totals"]["fresh_bobbins_used"] == 30
    assert red["end_state"]["partial_bobbin_count"] == 30


# --------------------------------------------------------------------------
# Assignment and feasibility
# --------------------------------------------------------------------------
def test_smallest_feasible_batch_wins():
    batches = {"121051": [_batch("BIG", 121051, 600, 4.0),
                          _batch("SMALL", 121051, 560, 4.0),
                          _batch("TOO-SMALL", 121051, 300, 4.0)]}
    ledger = compute_batch_ledger(_EXAMPLE_ROLLS, _EXAMPLE_EXTRACTION,
                                  batches)
    fg = _item(ledger, 121051)
    # SMALL covers 1000 x 1.1 lb and 546 bobbins; BIG is preserved.
    assert fg["batch"]["batch_number"] == "SMALL"
    assert fg["requirements"]["feasible"]


def test_infeasible_falls_back_to_largest_batch_with_warning():
    batches = {"121051": [_batch("A", 121051, 400, 4.0),
                          _batch("B", 121051, 500, 4.0)]}
    ledger = compute_batch_ledger(_EXAMPLE_ROLLS, _EXAMPLE_EXTRACTION,
                                  batches)
    fg = _item(ledger, 121051)
    assert fg["batch"]["batch_number"] == "B"
    assert not fg["requirements"]["feasible"]
    # 546 bobbins needed from a 500-bobbin batch: the run is still
    # simulated, with the shortfall counted and warned about.
    assert not fg["requirements"]["bobbins_feasible"]
    assert fg["totals"]["shortfall_bobbins"] == 46
    assert fg["end_state"]["shortfall_bobbins"] == 46
    assert any("no batch of item 121051 covers" in w
               for w in ledger["warnings"])
    assert any("runs out of full bobbins" in w for w in ledger["warnings"])


def test_requirement_checks_use_buffered_pounds():
    # 280 lb covers 250 lb needed but not 250 x 1.2: feasible at 10%,
    # infeasible at 20%.
    rolls = [_roll("L1", ("FG", 182), lf=100)]
    extraction = _extraction(rolls, [(121051, "FG", 250.0)])
    batches = {"121051": [_batch("B", 121051, 560, 0.5)]}
    at_10 = _item(compute_batch_ledger(rolls, extraction, batches,
                                       buffer_ratio=0.10), 121051)
    assert at_10["requirements"]["lbs_feasible"]
    at_20 = _item(compute_batch_ledger(rolls, extraction, batches,
                                       buffer_ratio=0.20), 121051)
    assert not at_20["requirements"]["lbs_feasible"]
    assert not at_20["requirements"]["feasible"]


def test_unmatched_items_and_no_overlap():
    # WHI has no batch: it is listed, not simulated. An inventory with no
    # overlap at all yields no ledger.
    batches = {"121051": [_batch("B-FG", 121051, 600, 4.0)]}
    ledger = compute_batch_ledger(_EXAMPLE_ROLLS, _EXAMPLE_EXTRACTION,
                                  batches)
    assert [i["item_number"] for i in ledger["items"]] == [121051]
    assert ledger["unmatched_items"] == ["121054"]
    assert compute_batch_ledger(_EXAMPLE_ROLLS, _EXAMPLE_EXTRACTION,
                                {"999": [_batch("X", 999, 10, 4.0)]}) is None
    assert compute_batch_ledger(_EXAMPLE_ROLLS, _EXAMPLE_EXTRACTION,
                                {}) is None


def test_no_yarn_lbs_block_means_no_ledger():
    extraction = {"source_file": "S.xlsx", "rolls": _EXAMPLE_ROLLS}
    assert compute_batch_ledger(_EXAMPLE_ROLLS, extraction,
                                _EXAMPLE_BATCHES) is None


def test_item_tufted_in_no_roll_leaves_batch_untouched():
    # BLK is priced in the yarn lbs block but appears in no roll: the batch
    # is assigned but never opened, and a warning says so.
    extraction = _extraction(_EXAMPLE_ROLLS, [(121051, "FG", 1000.0),
                                              (300, "BLK", 50.0)])
    batches = {"121051": [_batch("B-FG", 121051, 600, 4.0)],
               "300": [_batch("B-BLK", 300, 40, 4.0)]}
    ledger = compute_batch_ledger(_EXAMPLE_ROLLS, extraction, batches)
    blk = _item(ledger, 300)
    assert blk["rolls"] == []
    assert blk["end_state"]["full_bobbins_remaining"] == 40
    assert blk["end_state"]["partial_bobbin_count"] == 0
    assert blk["end_state"]["total_leftover_lb"] == 160
    assert any("tufted in no roll" in w for w in ledger["warnings"])


def test_roll_qty_expansion_draws_per_physical_roll():
    # A qty=2 entry tufts its stated length once, split across two physical
    # rolls — the ledger must draw the entry's total, not double it.
    rolls = [_roll("L1", ("FG", 182), qty=2, lf=200)]
    extraction = _extraction(rolls, [(121051, "FG", 500.0)])
    batches = {"121051": [_batch("B", 121051, 600, 4.0)]}
    ledger = compute_batch_ledger(rolls, extraction, batches)
    fg = _item(ledger, 121051)
    assert len(fg["rolls"]) == 2
    assert abs(fg["totals"]["total_drawn_lb"] - 500.0) < 1e-6
    assert fg["totals"]["fresh_bobbins_used"] == 546


# --------------------------------------------------------------------------
# Workbook loader
# --------------------------------------------------------------------------
def test_load_batch_workbook_demo_shape():
    if not _HAVE_OPENPYXL:
        return  # skip: openpyxl not installed
    data = _workbook_bytes([("B1234", 121054, 50, 4, 200)])
    batches, warnings = load_batch_workbook(data)
    assert warnings == []
    assert batches == {"121054": [{"batch_number": "B1234",
                                   "item_number": "121054",
                                   "bobbin_count": 50,
                                   "weight_per_bobbin_lb": 4,
                                   "total_weight_lb": 200}]}


def test_load_batch_workbook_column_order_and_multiple_batches():
    if not _HAVE_OPENPYXL:
        return  # skip: openpyxl not installed
    data = _workbook_bytes(
        [(121051, "A1", 100, 4, 400), (121051, "A2", 50, 4, 200)],
        header=("item_number", "batch_number", "number_of_bobbins",
                "weight_per_bobbin", "total_batch_weight"))
    batches, warnings = load_batch_workbook(data)
    assert warnings == []
    assert [b["batch_number"] for b in batches["121051"]] == ["A1", "A2"]


def test_load_batch_workbook_bad_rows_warn_and_skip():
    if not _HAVE_OPENPYXL:
        return  # skip: openpyxl not installed
    data = _workbook_bytes([
        ("B1", 121051, 50, 4, 200),        # good
        ("B1", 121051, 60, 4, 240),        # duplicate batch+item
        ("B2", 121051, -5, 4, 100),        # negative bobbins
        ("B3", 121051, 50, "heavy", 200),  # non-numeric weight
        ("B4", None, 50, 4, 200),          # blank item
        (None, None, None, None, None),    # blank row
        ("B5", 121051, 50, 4, 987),        # stated total disagrees
    ])
    batches, warnings = load_batch_workbook(data)
    assert [b["batch_number"] for b in batches["121051"]] == ["B1", "B5"]
    # B5's usable total is bobbins x weight, not the stated figure.
    assert batches["121051"][1]["total_weight_lb"] == 200
    assert any("duplicate batch B1" in w for w in warnings)
    assert any("must be positive" in w for w in warnings)
    assert any("is not a number" in w for w in warnings)
    assert any("blank batch or item" in w for w in warnings)
    assert any("differs from bobbins x weight" in w for w in warnings)


def test_load_batch_workbook_missing_column_or_garbage():
    if not _HAVE_OPENPYXL:
        return  # skip: openpyxl not installed
    data = _workbook_bytes([("B1", 121051, 50)],
                           header=("batch_number", "item_number",
                                   "number_of_bobbins"))
    batches, warnings = load_batch_workbook(data)
    assert batches == {}
    assert any("column 'weight_per_bobbin' not found" in w for w in warnings)

    batches, warnings = load_batch_workbook(b"not a workbook at all")
    assert batches == {}
    assert any("could not be read" in w for w in warnings)


# --------------------------------------------------------------------------
# Report integration
# --------------------------------------------------------------------------
def test_evaluate_carries_batch_ledger_key_only_with_batches():
    from evaluate import evaluate, report_json

    extraction = _EXAMPLE_EXTRACTION
    with_batches = evaluate(_EXAMPLE_ROLLS, extraction=extraction,
                            batches=_EXAMPLE_BATCHES)
    assert "batch_ledger" in with_batches
    assert with_batches["batch_ledger"]["buffer_ratio"] \
        == DEFAULT_BUFFER_RATIO
    # The optimised sequence reorders the rolls, but the totals are
    # order-independent.
    fg = _item(with_batches["batch_ledger"], 121051)
    assert abs(fg["totals"]["total_drawn_lb"] - 1000.0) < 1e-6
    # The report stays JSON-serialisable with the new key.
    assert "batch_ledger" in report_json(with_batches)

    without = evaluate(_EXAMPLE_ROLLS, extraction=extraction)
    assert "batch_ledger" not in without
    no_match = evaluate(_EXAMPLE_ROLLS, extraction=extraction,
                        batches={"999": [_batch("X", 999, 10, 4.0)]})
    assert "batch_ledger" not in no_match


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
