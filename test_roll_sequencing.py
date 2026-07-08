#!/usr/bin/env python3
"""
Tests for the Phase 1 roll sequencing cost model.

Runs with pytest if it's installed, and also standalone with no dependencies:

    python test_roll_sequencing.py

The central check is the worked example from docs/optimisation_plan.md
section 3: Roll A is 182" FG, Roll B is 177" FG + 5" WHI, and the cost of
changing between them must be exactly 5 inches.
"""

import roll_sequencing as rs


def _roll(*segments):
    """Build a minimal roll dict from (color_code, width) pairs, matching the
    shape the extractor emits (only the fields the cost model reads)."""
    return {"segments": [{"color_code": c, "width_in": w} for c, w in segments]}


# --- roll A and roll B from the plan's worked example --------------------
ROLL_A = _roll(("FG", 182))
ROLL_B = _roll(("FG", 177), ("WHI", 5))


def test_five_inch_example():
    # The whole point of Phase 1: reproduce the 5" example from the plan.
    assert rs.transition_cost(ROLL_A, ROLL_B) == 5


def test_cost_is_symmetric():
    assert rs.transition_cost(ROLL_A, ROLL_B) == rs.transition_cost(ROLL_B, ROLL_A)


def test_identical_layouts_cost_zero():
    assert rs.transition_cost(ROLL_A, _roll(("FG", 182))) == 0
    assert rs.transition_cost(ROLL_B, _roll(("FG", 177), ("WHI", 5))) == 0


def test_completely_different_layouts_cost_full_width():
    left = _roll(("FG", 182))
    right = _roll(("WHI", 182))
    assert rs.transition_cost(left, right) == 182


def test_misaligned_segment_boundaries():
    # A: 100 FG | 82 WHI ; B: 90 FG | 92 WHI
    # They agree on 0..90 (FG) and 100..182 (WHI); they differ on 90..100
    # (FG vs WHI) -> 10 inches.
    a = _roll(("FG", 100), ("WHI", 82))
    b = _roll(("FG", 90), ("WHI", 92))
    assert rs.transition_cost(a, b) == 10


def test_cost_is_positional_not_a_colour_total():
    # Both rolls contain the same colour *totals* (177 FG + 5 WHI) but in
    # mirror-image positions: the WHI is at the front on one and at the back
    # on the other. Cost is per inch position, so a colour-total view would
    # wrongly say 0 — the real changeover is the 5" at each end that flip
    # between FG and WHI, i.e. 10 inches.
    whi_at_front = _roll(("WHI", 5), ("FG", 177))
    whi_at_back = _roll(("FG", 177), ("WHI", 5))
    assert rs.transition_cost(whi_at_front, whi_at_back) == 10


def test_five_inch_example_changes_the_rightmost_positions():
    # Roll A is all FG; Roll B puts WHI in the last 5" (positions 177..182).
    # Only those rightmost 5 locations change; everything before is untouched.
    assert rs.transition_cost(ROLL_A, ROLL_B) == 5
    # Moving the same 5" of WHI to the front instead still costs 5 — the model
    # only cares that 5 locations changed, wherever they are.
    whi_at_front = _roll(("WHI", 5), ("FG", 177))
    assert rs.transition_cost(ROLL_A, whi_at_front) == 5


def test_fractional_widths():
    # Fractional gauges produce fractional-inch costs from the same formula.
    a = _roll(("FG", 176.5), ("WHI", 5.5))
    b = _roll(("FG", 182.0))
    assert rs.transition_cost(a, b) == 5.5


def test_adjacent_same_colour_segments_merge():
    # Two ways of describing the same physical layout must cost 0 against
    # each other: 182 FG vs 100 FG + 82 FG.
    a = _roll(("FG", 182))
    b = _roll(("FG", 100), ("FG", 82))
    assert rs.transition_cost(a, b) == 0


def test_sequence_cost_is_additive():
    # A -> B (5) then B -> A (5) totals 10.
    assert rs.sequence_cost([ROLL_A, ROLL_B, ROLL_A]) == 10


def test_sequence_cost_trivial_cases():
    assert rs.sequence_cost([]) == 0
    assert rs.sequence_cost([ROLL_A]) == 0


def test_transition_breakdown():
    # Sequence A, A, B: first transition 0 (identical), second 5.
    breakdown = rs.transition_breakdown([ROLL_A, _roll(("FG", 182)), ROLL_B])
    assert breakdown["roll_count"] == 3
    assert breakdown["transition_count"] == 2
    assert breakdown["total_cost"] == 5
    assert breakdown["zero_cost_transitions"] == 1
    assert breakdown["max_transition_cost"] == 5
    assert breakdown["transition_costs"] == [0, 5]


def test_parse_signature_matches_segments():
    # The signature string form and the segment form must yield the same
    # profile, so Phase 2 grouping and this cost model agree.
    profile = rs.parse_signature("177FG|5WHI")
    assert rs.profile_cost(rs.roll_profile(ROLL_A), profile) == 5


def test_parse_signature_round_trip_from_extractor_form():
    # "5WHI|177LIM" is the example signature from the README.
    profile = rs.parse_signature("5WHI|177LIM")
    assert rs.profile_width(profile) == 182


def test_differing_total_widths_warn_and_count_tail():
    # Not expected within an order, but must be handled: a 182" roll against a
    # 180" roll differs on the 2" tail that only one of them threads.
    a = _roll(("FG", 182))
    b = _roll(("FG", 180))
    assert rs.transition_cost(a, b) == 2
    warnings = []
    rs.sequence_cost([a, b], warnings)
    assert warnings, "expected a width-mismatch warning"


# --- join_orders (combining extraction results) ---------------------------
def _extraction(name, rolls, mfg_summary=None, warnings=(), po=None):
    """Minimal extraction-result dict in the shape the extractor emits (only
    the fields join_orders reads)."""
    out = {"source_file": name, "rolls": rolls, "roll_count": len(rolls)}
    if mfg_summary is not None:
        out["mfg_summary"] = mfg_summary
    if warnings:
        out["warnings"] = list(warnings)
    if po is not None:
        out["general_information"] = {"purchase_order_number": po}
    return out


def test_join_orders_concatenates_and_tags_copies():
    # Rolls come out concatenated in input order, each tagged with the file it
    # came from — on a copy, so the inputs stay untouched.
    rolls_a = [_roll(("FG", 182)), _roll(("FG", 177), ("WHI", 5))]
    rolls_b = [_roll(("WHI", 182))]
    combined = rs.join_orders([_extraction("A.xlsx", rolls_a),
                               _extraction("B.xlsx", rolls_b)])
    assert combined["roll_count"] == 3
    assert [r["segments"] for r in combined["rolls"]] == \
        [r["segments"] for r in rolls_a + rolls_b]
    assert [r["source_file"] for r in combined["rolls"]] == \
        ["A.xlsx", "A.xlsx", "B.xlsx"]
    # The original roll dicts gained no key.
    for original in rolls_a + rolls_b:
        assert set(original) == {"segments"}


def test_join_orders_clears_file_local_layout_group():
    # The extractor numbers layout groups per file, so the same id in two
    # workbooks names two different layouts. Joining clears the id on the
    # copies — grouping uses the threading profile alone — so the Phase 2
    # extractor-consistency check is not tripped by ids that were never
    # meant to be compared across files. The originals keep theirs.
    roll_a = dict(_roll(("FG", 182)), layout_group=3)
    roll_b = dict(_roll(("WHI", 182)), layout_group=3)
    combined = rs.join_orders([_extraction("A.xlsx", [roll_a]),
                               _extraction("B.xlsx", [roll_b])])
    assert all(r["layout_group"] is None for r in combined["rolls"])
    assert roll_a["layout_group"] == 3
    assert roll_b["layout_group"] == 3


def test_join_orders_tags_rolls_with_their_files_purchase_order():
    # Each joined roll copy carries its own file's PO, so a combined run sheet
    # can say which purchase order every roll belongs to. The originals gain
    # no key.
    rolls_a = [_roll(("FG", 182)), _roll(("FG", 177), ("WHI", 5))]
    rolls_b = [_roll(("WHI", 182))]
    combined = rs.join_orders([_extraction("A.xlsx", rolls_a, po="PO-1001"),
                               _extraction("B.xlsx", rolls_b, po="PO-2002")])
    assert [r["purchase_order_number"] for r in combined["rolls"]] == \
        ["PO-1001", "PO-1001", "PO-2002"]
    for original in rolls_a + rolls_b:
        assert set(original) == {"segments"}


def test_join_orders_purchase_order_none_when_unstated():
    # No general_information (or a bare roll list) -> the tag is None, not a
    # crash; downstream renders it as blank.
    combined = rs.join_orders([_extraction("A.xlsx", [_roll(("FG", 182))]),
                               [_roll(("WHI", 182))]])
    assert [r["purchase_order_number"] for r in combined["rolls"]] == \
        [None, None]


def test_join_orders_keeps_existing_roll_po_tag_when_file_states_none():
    # Re-joining an already-joined result must not wipe the per-roll tags its
    # rolls already carry (the joined dict itself has no general_information).
    tagged = dict(_roll(("FG", 182)), purchase_order_number="PO-1001")
    combined = rs.join_orders([_extraction("A.xlsx", [tagged])])
    assert combined["rolls"][0]["purchase_order_number"] == "PO-1001"


def test_join_orders_combined_name():
    combined = rs.join_orders([_extraction("A.xlsx", []),
                               _extraction("B.xlsx", [])])
    assert combined["source_file"] == "A.xlsx + B.xlsx"
    assert combined["source_files"] == ["A.xlsx", "B.xlsx"]


def test_join_orders_accepts_bare_roll_lists():
    # A bare list of rolls (no extraction dict) is a valid order; it is named
    # by its 1-based position.
    combined = rs.join_orders([[_roll(("FG", 182))],
                               _extraction("B.xlsx", [_roll(("WHI", 182))]),
                               [_roll(("FG", 100), ("WHI", 82))]])
    assert combined["source_files"] == ["order 1", "B.xlsx", "order 3"]
    assert [r["source_file"] for r in combined["rolls"]] == \
        ["order 1", "B.xlsx", "order 3"]


def test_join_orders_sums_mfg_summary():
    combined = rs.join_orders([
        _extraction("A.xlsx", [], mfg_summary={"mfg_rolls": 4, "mfg_lf": 100.5,
                                               "mfg_sf": 1500}),
        _extraction("B.xlsx", [], mfg_summary={"mfg_rolls": 6, "mfg_lf": 99.5,
                                               "mfg_sf": 2500}),
    ])
    # 100.5 + 99.5 comes back as the clean whole number 200.
    assert combined["mfg_summary"] == {"mfg_rolls": 10, "mfg_lf": 200,
                                       "mfg_sf": 4000}


def test_join_orders_partial_summary_fields_become_none():
    # A field is summed only when every input states it numerically. A partial
    # sum would raise a spurious mismatch downstream instead of catching a
    # real one, so missing / non-numeric anywhere -> None for that field.
    combined = rs.join_orders([
        _extraction("A.xlsx", [], mfg_summary={"mfg_rolls": 4, "mfg_lf": 100,
                                               "mfg_sf": "n/a"}),
        _extraction("B.xlsx", [], mfg_summary={"mfg_rolls": 6}),
    ])
    assert combined["mfg_summary"]["mfg_rolls"] == 10
    assert combined["mfg_summary"]["mfg_lf"] is None  # missing in B
    assert combined["mfg_summary"]["mfg_sf"] is None  # non-numeric in A


def test_join_orders_empty_input():
    combined = rs.join_orders([])
    assert combined["rolls"] == []
    assert combined["roll_count"] == 0
    assert combined["mfg_summary"] == {"mfg_rolls": None, "mfg_lf": None,
                                       "mfg_sf": None}


def test_join_orders_prefixes_warnings_with_source():
    combined = rs.join_orders([
        _extraction("A.xlsx", [], warnings=["width mismatch"]),
        _extraction("B.xlsx", [], warnings=["odd colour code"]),
    ])
    assert combined["warnings"] == ["A.xlsx: width mismatch",
                                    "B.xlsx: odd colour code"]


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
        except Exception as exc:  # noqa: BLE001 - surface any error in the runner
            failures += 1
            print(f"  ERROR {test.__name__}: {exc!r}")
    print(f"\n{len(tests) - failures}/{len(tests)} passed")
    return 1 if failures else 0


if __name__ == "__main__":
    import sys
    sys.exit(_run_standalone())
