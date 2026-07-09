#!/usr/bin/env python3
"""
Tests for the per-item bobbin data loader.

Runs with pytest, or standalone with no dependencies:

    python test_item_data.py
"""

import tempfile
from pathlib import Path

import item_data as idata


def _load_text(text):
    """Write `text` to a temp CSV and load it, returning (items, warnings)."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "items.csv"
        path.write_text(text, encoding="utf-8")
        return idata.load_item_data(path)


_HEADER = ("item_number,yarn_type,color_code,"
           "weight_lb_per_sqft,fresh_bobbin_weight_lb\n")


# --- the repo's own data file -----------------------------------------------
def test_default_path_points_at_repo_data_file():
    assert idata.DEFAULT_ITEM_DATA_PATH.name == "item_bobbin_data.csv"
    assert idata.DEFAULT_ITEM_DATA_PATH.parent.name == "data"


def test_seeded_repo_csv_loads():
    if not idata.DEFAULT_ITEM_DATA_PATH.exists():
        return  # skip: checkout without the data file
    items, warnings = idata.load_item_data()
    assert warnings == []
    item = items["121051"]
    assert item["yarn_type"] == "5040 XP+ (6Pin)"
    assert item["color_code"] == "FG"
    assert item["weight_lb_per_sqft"] == 0.04831
    # Fresh bobbin weight is deliberately blank until measured on the floor.
    assert item["fresh_bobbin_weight_lb"] is None


# --- happy path and blanks ---------------------------------------------------
def test_loads_rows_keyed_by_item_number_string():
    items, warnings = _load_text(
        _HEADER
        + "121051,5040 XP+ (6Pin),FG,0.04831,5.5\n"
        + "145190A,SXT 5400/6,WHI,0.02,\n")
    assert warnings == []
    assert set(items) == {"121051", "145190A"}
    assert items["121051"] == {
        "item_number": "121051", "yarn_type": "5040 XP+ (6Pin)",
        "color_code": "FG", "weight_lb_per_sqft": 0.04831,
        "fresh_bobbin_weight_lb": 5.5,
    }
    # Blank fresh weight -> None, row kept.
    assert items["145190A"]["fresh_bobbin_weight_lb"] is None


def test_blank_lines_and_comments_are_skipped_silently():
    items, warnings = _load_text(
        _HEADER
        + "\n"
        + "# fresh weights pending floor measurement\n"
        + "   \n"
        + "121051,T,FG,0.05,\n")
    assert warnings == []
    assert set(items) == {"121051"}


def test_header_only_file_loads_empty():
    items, warnings = _load_text(_HEADER)
    assert items == {}
    assert warnings == []


# --- malformed content -> warning, never an exception ------------------------
def test_wrong_field_count_warns_and_skips_row():
    items, warnings = _load_text(
        _HEADER
        + "121051,T,FG,0.05\n"          # 4 fields
        + "121052,T,WHI,0.05,,extra\n"  # 6 fields
        + "121053,T,BLK,0.05,\n")       # good
    assert set(items) == {"121053"}
    assert len([w for w in warnings if "fields" in w]) == 2


def test_unparsable_weight_warns_and_skips_row():
    items, warnings = _load_text(
        _HEADER
        + "121051,T,FG,heavy,\n"
        + "121052,T,WHI,0.05,light\n"
        + "121053,T,BLK,0.05,\n")
    assert set(items) == {"121053"}
    assert any("weight_lb_per_sqft" in w and "not a number" in w
               for w in warnings)
    assert any("fresh_bobbin_weight_lb" in w and "not a number" in w
               for w in warnings)


def test_non_positive_weights_warn_and_skip_row():
    items, warnings = _load_text(
        _HEADER
        + "121051,T,FG,0,\n"
        + "121052,T,WHI,-0.05,\n"
        + "121053,T,BLK,0.05,0\n"
        + "121054,T,LIM,0.05,2.5\n")
    assert set(items) == {"121054"}
    assert len([w for w in warnings if "must be positive" in w]) == 3


def test_duplicate_item_number_keeps_first_and_warns():
    items, warnings = _load_text(
        _HEADER
        + "121051,T,FG,0.05,\n"
        + "121051,T,WHI,0.07,\n")
    assert items["121051"]["color_code"] == "FG"
    assert items["121051"]["weight_lb_per_sqft"] == 0.05
    assert any("duplicate item number 121051" in w for w in warnings)


def test_blank_item_number_warns_and_skips_row():
    items, warnings = _load_text(_HEADER + ",T,FG,0.05,\n")
    assert items == {}
    assert any("blank item number" in w for w in warnings)


def test_missing_header_warns_but_reads_rows():
    items, warnings = _load_text("121051,T,FG,0.05,\n")
    assert set(items) == {"121051"}
    assert any("no header row" in w for w in warnings)


# --- missing file -------------------------------------------------------------
def test_missing_file_returns_empty_with_warning():
    missing = Path(tempfile.gettempdir()) / "no_such_dir_xyz" / "items.csv"
    assert not missing.exists()
    items, warnings = idata.load_item_data(missing)
    assert items == {}
    assert len(warnings) == 1
    assert "not available" in warnings[0]


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
