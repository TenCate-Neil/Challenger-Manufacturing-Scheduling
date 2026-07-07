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
