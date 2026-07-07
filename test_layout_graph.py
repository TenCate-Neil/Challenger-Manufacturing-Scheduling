#!/usr/bin/env python3
"""
Tests for the Phase 2 layout collapsing and distance graph.

Runs with pytest, or standalone with no dependencies:

    python test_layout_graph.py
"""

import layout_graph as lg
import roll_sequencing as rs


def _roll(lot, *segments, roll_qty=1, layout_group=None):
    roll = {
        "navision_lot": lot,
        "roll_qty": roll_qty,
        "segments": [{"color_code": c, "width_in": w} for c, w in segments],
    }
    if layout_group is not None:
        roll["layout_group"] = layout_group
    return roll


# Three layouts, one of them appearing twice.
FULL_FG = ("FG", 182)
FG_WHI = [("FG", 177), ("WHI", 5)]
FULL_WHI = ("WHI", 182)


def _sample_rolls():
    return [
        _roll("L1", FULL_FG, layout_group=1),
        _roll("L2", *FG_WHI, layout_group=2),
        _roll("L3", *FG_WHI, layout_group=2),   # duplicate of L2's layout
        _roll("L4", FULL_WHI, layout_group=3),
    ]


# --- collapsing -----------------------------------------------------------
def test_collapse_finds_distinct_layouts():
    groups = lg.collapse_layouts(_sample_rolls())
    assert len(groups) == 3
    assert [g["layout_index"] for g in groups] == [0, 1, 2]


def test_collapse_groups_duplicates_together():
    groups = lg.collapse_layouts(_sample_rolls())
    fg_whi_group = groups[1]
    assert [r["navision_lot"] for r in fg_whi_group["rolls"]] == ["L2", "L3"]
    assert fg_whi_group["roll_entry_count"] == 2
    assert fg_whi_group["physical_roll_qty"] == 2


def test_collapse_signature_form():
    groups = lg.collapse_layouts(_sample_rolls())
    assert groups[0]["layout_signature"] == "182FG"
    assert groups[1]["layout_signature"] == "177FG|5WHI"
    assert groups[2]["layout_signature"] == "182WHI"


def test_collapse_preserves_first_appearance_order():
    # Reversing input order changes which layout is index 0.
    rolls = list(reversed(_sample_rolls()))
    groups = lg.collapse_layouts(rolls)
    assert groups[0]["layout_signature"] == "182WHI"


def test_collapse_merges_adjacent_same_colour():
    # A layout written as two adjacent FG runs is the same layout as one run.
    rolls = [_roll("A", ("FG", 182)), _roll("B", ("FG", 100), ("FG", 82))]
    groups = lg.collapse_layouts(rolls)
    assert len(groups) == 1
    assert groups[0]["roll_entry_count"] == 2


def test_every_roll_is_grouped_exactly_once():
    rolls = _sample_rolls()
    groups = lg.collapse_layouts(rolls)
    grouped = [r for g in groups for r in g["rolls"]]
    assert len(grouped) == len(rolls)
    assert {id(r) for r in grouped} == {id(r) for r in rolls}


# --- distance graph -------------------------------------------------------
def test_distance_matrix_shape_and_diagonal():
    groups = lg.collapse_layouts(_sample_rolls())
    matrix = lg.distance_matrix(groups)
    assert len(matrix) == 3
    assert all(len(row) == 3 for row in matrix)
    assert all(matrix[i][i] == 0 for i in range(3))


def test_distance_matrix_is_symmetric():
    groups = lg.collapse_layouts(_sample_rolls())
    matrix = lg.distance_matrix(groups)
    for i in range(3):
        for j in range(3):
            assert matrix[i][j] == matrix[j][i]


def test_distance_matrix_values():
    # 0: 182 FG, 1: 177 FG + 5 WHI, 2: 182 WHI
    groups = lg.collapse_layouts(_sample_rolls())
    matrix = lg.distance_matrix(groups)
    assert matrix[0][1] == 5      # only the last 5" change
    assert matrix[0][2] == 182    # completely different
    assert matrix[1][2] == 177    # the 177" FG portion changes


def test_distance_matches_phase1_cost():
    groups = lg.collapse_layouts(_sample_rolls())
    matrix = lg.distance_matrix(groups)
    for i, gi in enumerate(groups):
        for j, gj in enumerate(groups):
            assert matrix[i][j] == rs.profile_cost(gi["profile"], gj["profile"])


# --- expansion / conservation --------------------------------------------
def test_expand_default_order_recovers_all_rolls():
    rolls = _sample_rolls()
    groups = lg.collapse_layouts(rolls)
    expanded = lg.expand_sequence(groups)
    assert [r["navision_lot"] for r in expanded] == ["L1", "L2", "L3", "L4"]


def test_expand_reordered_is_a_permutation():
    rolls = _sample_rolls()
    groups = lg.collapse_layouts(rolls)
    expanded = lg.expand_sequence(groups, order=[2, 0, 1])
    # every original roll present exactly once, duplicates kept adjacent
    assert sorted(r["navision_lot"] for r in expanded) == ["L1", "L2", "L3", "L4"]
    assert [r["navision_lot"] for r in expanded] == ["L4", "L1", "L2", "L3"]


def test_expand_rejects_incomplete_order():
    groups = lg.collapse_layouts(_sample_rolls())
    try:
        lg.expand_sequence(groups, order=[0, 1])
        assert False, "expected ValueError for incomplete ordering"
    except ValueError:
        pass


def test_expand_rejects_repeated_layout():
    groups = lg.collapse_layouts(_sample_rolls())
    try:
        lg.expand_sequence(groups, order=[0, 0, 1])
        assert False, "expected ValueError for repeated layout"
    except ValueError:
        pass


def test_expand_rejects_unknown_index():
    groups = lg.collapse_layouts(_sample_rolls())
    try:
        lg.expand_sequence(groups, order=[0, 1, 99])
        assert False, "expected ValueError for unknown layout_index"
    except ValueError:
        pass


# --- extractor consistency check ------------------------------------------
def test_consistency_warns_when_one_group_id_spans_two_profiles():
    # Two genuinely different layouts mislabelled with the same layout_group.
    rolls = [
        _roll("A", ("FG", 182), layout_group=7),
        _roll("B", ("WHI", 182), layout_group=7),
    ]
    warnings = []
    groups = lg.collapse_layouts(rolls, warnings)
    assert len(groups) == 2
    assert any("layout_group 7 spans" in w for w in warnings)


def test_no_warning_on_clean_data():
    warnings = []
    lg.collapse_layouts(_sample_rolls(), warnings)
    assert warnings == []


def test_build_graph_convenience():
    groups, matrix = lg.build_graph(_sample_rolls())
    assert len(groups) == 3 and len(matrix) == 3


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
