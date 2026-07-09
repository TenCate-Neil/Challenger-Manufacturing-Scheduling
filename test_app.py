#!/usr/bin/env python3
"""
Tests for the Phase 5 Streamlit front end.

The front end depends on Streamlit (and the extractor on openpyxl), which the
dependency-free core suites do not require. So these tests skip cleanly when
Streamlit or the extractor cannot be imported, and exercise the app when they
can. They drive the app headlessly through Streamlit's own `AppTest` harness —
no browser involved.

Runs with pytest, or standalone:

    python test_app.py
"""

try:
    from streamlit.testing.v1 import AppTest
    import app  # noqa: F401  (also pulls in the extractor -> openpyxl)
    from evaluate import evaluate
    _HAVE_DEPS = True
except Exception as _exc:  # noqa: BLE001
    _HAVE_DEPS = False
    _IMPORT_ERROR = _exc


def _skip_reason():
    return f"streamlit/extractor not available ({_IMPORT_ERROR})"


def _have_fpdf():
    """True when fpdf2 imports cleanly. A broken install can blow up with a
    BaseException-derived error (e.g. pyo3_runtime.PanicException from a bad
    cryptography build), which a plain `except Exception` guard would let
    escape — so this catches BaseException, re-raising the control-flow
    exceptions. PDF-dependent tests skip cleanly either way."""
    try:
        import fpdf  # noqa: F401
        return True
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException:  # noqa: BLE001
        return False


def _roll(lot, *segs, sort=None, qty=1, lf=100, sf=1500, panels=None):
    return {"navision_lot": lot, "sort": sort, "roll_type": "FIELD",
            "roll_qty": qty, "mfg_roll_length_lf": lf, "total_mfg_sf": sf,
            "panel_numbers": panels,
            "layout_signature": "|".join(f"{w}{c}" for c, w in segs),
            "layout_group": None,
            "segments": [{"color_code": c, "width_in": w} for c, w in segs]}


# A tiny harness script that renders a real evaluate() report through the
# app's own rendering code, so the render path is exercised without a workbook.
_RENDER_HARNESS = """
import streamlit as st
import app
from evaluate import evaluate

def _roll(lot, *segs, sort=None):
    return {"navision_lot": lot, "sort": sort, "roll_type": "FIELD",
            "roll_qty": 1, "mfg_roll_length_lf": 100, "total_mfg_sf": 1500,
            "layout_signature": "|".join(f"{w}{c}" for c, w in segs),
            "layout_group": None,
            "segments": [{"color_code": c, "width_in": w} for c, w in segs]}

rolls = [_roll("L1", ("FG", 182), sort=1),
         _roll("L2", ("FG", 177), ("WHI", 5), sort=2),
         _roll("L3", ("FG", 177), ("WHI", 5), sort=3),
         _roll("L4", ("FG", 100), ("WHI", 82), sort=4)]
report = evaluate(rolls, extraction={"source_file": "SAMPLE.xlsx"})
app._render_report(st, "SAMPLE.xlsx", {"source_file": "SAMPLE.xlsx"}, report)
"""


def test_app_empty_state_runs():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    at = AppTest.from_file("app.py").run(timeout=30)
    assert not at.exception, at.exception
    assert at.title[0].value == "Roll sequencing"
    # Two sidebar controls (exact threshold, oracle threshold).
    assert len(at.number_input) == 2
    # With no upload the app prompts for a file rather than erroring.
    assert any("Upload one or more" in i.value for i in at.info)


def test_render_report_path():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    at = AppTest.from_string(_RENDER_HARNESS).run(timeout=30)
    assert not at.exception, at.exception
    labels = [m.label for m in at.metric]
    assert "Rolls" in labels
    assert "Achieved setup cost" in labels
    # The manufacturing sequence renders as a table.
    assert len(at.dataframe) == 1
    # Conservation holds for a faithful reordering -> success banner.
    assert any(s.value.startswith("Conservation check passed") for s in at.success)
    # The JSON report is always downloadable; the printable run sheet is too
    # wherever fpdf2 is installed (it degrades to a note otherwise).
    labels = [b.label for b in at.download_button]
    assert "Download sequence report (JSON)" in labels
    if _have_fpdf():
        assert "Download run sheet (PDF)" in labels
    # No yarn_lbs block in the extraction -> no item_requirements key in the
    # report -> the section renders nothing at all.
    assert not any("Item batch requirements" in m.value for m in at.markdown)


def test_sequence_view_carries_panel_numbers():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    # Panel numbers must be threaded from the roll dict into the sequence view
    # so the run sheet can print them.
    rolls = [_roll("L1", ("FG", 182), sort=1, panels="P1-P4"),
             _roll("L2", ("FG", 100), ("WHI", 82), sort=2, panels="P5")]
    report = evaluate(rolls, extraction={"source_file": "SAMPLE.xlsx"})
    panels = {e["navision_lot"]: e.get("panel_numbers")
              for e in report["manufacturing_sequence"]}
    assert panels == {"L1": "P1-P4", "L2": "P5"}


def test_run_sheet_rows_expand_qty_and_carry_fields():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    # A qty=2 entry must expand to two physical-roll rows (matching the "Full
    # manufacturing order" view), and each row must carry lot, panels, length,
    # and a parsed layout profile.
    rolls = [_roll("L1", ("FG", 182), sort=1, qty=2, panels="P1-P2", lf=120),
             _roll("L2", ("FG", 100), ("WHI", 82), sort=2, panels="P3")]
    report = evaluate(rolls, extraction={
        "source_file": "SAMPLE.xlsx",
        "general_information": {"purchase_order_number": "PO-7001"},
    })
    rows = app._run_sheet_rows(report)

    # qty=2 + qty=1 -> three physical rolls, numbered 1..3 (order is optimised,
    # so assertions below are order-independent).
    assert [r["position"] for r in rows] == [1, 2, 3]
    lots = [r["navision_lot"] for r in rows]
    assert lots.count("L1") == 2 and lots.count("L2") == 1
    # Single-file path: every row carries the extraction's PO.
    assert all(r["purchase_order_number"] == "PO-7001" for r in rows)
    # The L1 rows carry its panels, length, and parsed layout profile.
    l1_rows = [r for r in rows if r["navision_lot"] == "L1"]
    assert all(r["panel_numbers"] == "P1-P2" for r in l1_rows)
    assert all(r["length_lf"] == 120 for r in l1_rows)
    assert all(("FG", 182) in r["profile"] for r in l1_rows)
    # The first roll is a fresh start; every change is a human string.
    assert rows[0]["change"] == "start"
    assert all(isinstance(r["change"], str) for r in rows)


def test_build_run_sheet_pdf_bytes():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    if not _have_fpdf():
        return  # skip: fpdf2 unavailable (or its install is broken)
    rolls = [_roll("L1", ("FG", 182), sort=1, qty=2, panels="P1-P2"),
             _roll("L2", ("FG", 100), ("WHI", 82), sort=2, panels="P3")]
    report = evaluate(rolls, extraction={"source_file": "SAMPLE.xlsx"})
    pdf = app.build_run_sheet_pdf("SAMPLE.xlsx", report)
    assert isinstance(pdf, (bytes, bytearray))
    assert bytes(pdf[:5]) == b"%PDF-"
    # A non-trivial document (header + legend + three rows) is well over 1 KB.
    assert len(pdf) > 1000


def _pdf_content(pdf_bytes):
    """The PDF's stream contents with any zlib compression undone. The run
    sheet uses only the built-in core fonts, so its text appears as literal
    latin-1 strings in the content streams — enough to assert on without a
    PDF-parsing dependency."""
    import re
    import zlib

    out = bytearray()
    for match in re.finditer(rb"stream\r?\n(.*?)endstream", pdf_bytes,
                             re.DOTALL):
        data = match.group(1)
        try:
            data = zlib.decompress(data)
        except zlib.error:
            pass  # an uncompressed stream (or an image) — use as-is
        out.extend(data)
    return bytes(out)


def test_run_sheet_pdf_shows_per_roll_purchase_orders():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    if not _have_fpdf():
        return  # skip: fpdf2 unavailable (or its install is broken)
    # A combined run mixes files with different POs, so the run sheet needs a
    # per-roll "PO #" column: both PO values must land in the PDF text, not
    # just one header-level PO.
    ext_a = _extraction("A.xlsx", [_roll("A1", ("FG", 182), sort=1),
                                   _roll("A2", ("FG", 177), ("WHI", 5), sort=2)],
                        po="PO-1001")
    ext_b = _extraction("B.xlsx", [_roll("B1", ("FG", 100), ("WHI", 82), sort=1)],
                        po="PO-2002")
    _, report = app.evaluate_combined([ext_a, ext_b])
    pdf = app.build_run_sheet_pdf("combined", report)
    content = _pdf_content(bytes(pdf))
    assert b"PO #" in content
    assert b"PO-1001" in content
    assert b"PO-2002" in content


def test_breakdown_one_entry_per_physical_roll():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    if not _have_fpdf():
        return  # skip: fpdf2 unavailable (or its install is broken)
    # The bottom breakdown is per physical roll, not per distinct layout: a
    # qty=2 entry yields two breakdown entries (both carrying its lot), so
    # rolls that share a layout but differ in length each get their own block.
    rolls = [_roll("L1", ("FG", 182), sort=1, qty=2, lf=120),
             _roll("L2", ("FG", 177), ("WHI", 5), sort=2, lf=95)]
    report = evaluate(rolls, extraction={"source_file": "SAMPLE.xlsx"})
    content = _pdf_content(bytes(app.build_run_sheet_pdf("SAMPLE.xlsx", report)))
    # Three physical rolls -> three "Roll N of 3" entry headings.
    for n in (1, 2, 3):
        assert content.count(f"Roll {n} of 3".encode("latin-1")) == 1, n
    # The "Lot <x>" heading form is breakdown-specific (the table prints the
    # bare lot), so the qty=2 lot appears exactly twice, the qty=1 lot once.
    assert content.count(b"Lot L1") == 2
    assert content.count(b"Lot L2") == 1


def test_breakdown_shows_lengths_and_no_total_suffix():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    if not _have_fpdf():
        return  # skip: fpdf2 unavailable (or its install is broken)
    # Each breakdown entry states the roll's length in LF, and the segment
    # widths are spelled out without any "= 182 in total" suffix anywhere.
    rolls = [_roll("L1", ("FG", 182), sort=1, qty=2, lf=120),
             _roll("L2", ("FG", 177), ("WHI", 5), sort=2, lf=95)]
    report = evaluate(rolls, extraction={"source_file": "SAMPLE.xlsx"})
    content = _pdf_content(bytes(app.build_run_sheet_pdf("SAMPLE.xlsx", report)))
    # The "<n> LF" form is breakdown-specific (the table's length column
    # prints the bare number), so each length appears once per physical roll.
    assert content.count(b"120 LF") == 2
    assert content.count(b"95 LF") == 1
    # The old grouped section's total suffix is gone from the whole PDF.
    assert b"in total" not in content


def test_breakdown_setup_change_marker_between_differing_layouts():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    if not _have_fpdf():
        return  # skip: fpdf2 unavailable (or its install is broken)
    # Two distinct layouts -> the optimised run has exactly one costly
    # transition, so exactly one red SETUP CHANGE band separates the entries
    # (not one before every roll, and none between the identical-layout pair).
    rolls = [_roll("L1", ("FG", 182), sort=1, qty=2),
             _roll("L2", ("FG", 177), ("WHI", 5), sort=2)]
    report = evaluate(rolls, extraction={"source_file": "SAMPLE.xlsx"})
    content = _pdf_content(bytes(app.build_run_sheet_pdf("SAMPLE.xlsx", report)))
    assert content.count(b"SETUP CHANGE") == 1
    # The marker is drawn in red: the red non-stroking colour operator
    # (200, 0, 0 -> "0.7843 0 0 rg") is set just before its text.
    idx = content.index(b"SETUP CHANGE")
    assert b"0.7843 0 0 rg" in content[max(0, idx - 400):idx]


def test_breakdown_no_setup_change_when_layouts_identical():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    if not _have_fpdf():
        return  # skip: fpdf2 unavailable (or its install is broken)
    # Every roll shares one layout -> no costly transition anywhere, so the
    # breakdown shows the rolls back to back with no SETUP CHANGE band.
    rolls = [_roll("L1", ("FG", 182), sort=1, qty=2, lf=120),
             _roll("L2", ("FG", 182), sort=2, lf=80)]
    report = evaluate(rolls, extraction={"source_file": "SAMPLE.xlsx"})
    content = _pdf_content(bytes(app.build_run_sheet_pdf("SAMPLE.xlsx", report)))
    assert b"SETUP CHANGE" not in content
    # All three physical rolls still get their own entry.
    for n in (1, 2, 3):
        assert content.count(f"Roll {n} of 3".encode("latin-1")) == 1, n


def _extraction(name, rolls, po=None, yarn_lbs=None):
    # A synthetic extraction dict shaped like `extract_workbook`'s output,
    # with a correct MFG summary derived from the rolls (roll_qty, LF and SF
    # summed the same way the evaluator's cross-check sums the sequence).
    # Pass `yarn_lbs` to carry the per-item yarn pounds block; extractions
    # without one omit the key, exactly as the extractor does.
    out = {
        "source_file": name,
        "rolls": rolls,
        "roll_count": len(rolls),
        "general_information": {"purchase_order_number": po},
        "mfg_summary": {
            "mfg_rolls": sum(r["roll_qty"] for r in rolls),
            "mfg_lf": sum(r["mfg_roll_length_lf"] for r in rolls),
            "mfg_sf": sum(r["total_mfg_sf"] for r in rolls),
        },
        "warnings": [],
    }
    if yarn_lbs is not None:
        out["yarn_lbs"] = yarn_lbs
    return out


def test_evaluate_combined_two_orders():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    # Two orders joined into one: the report must cover every roll from both
    # files, stay a faithful reordering (conservation), name both sources,
    # and — because the joined MFG summary sums are correct — raise no
    # cross-check mismatch warning.
    ext_a = _extraction("A.xlsx", [_roll("A1", ("FG", 182), sort=1, qty=2),
                                   _roll("A2", ("FG", 177), ("WHI", 5), sort=2)])
    ext_b = _extraction("B.xlsx", [_roll("B1", ("FG", 100), ("WHI", 82), sort=1)])
    combined, report = app.evaluate_combined([ext_a, ext_b])

    assert report["conservation"]["passed"], report["conservation"]
    assert "A.xlsx" in report["source_file"]
    assert "B.xlsx" in report["source_file"]
    # Every roll in the combined sequence traces back to an input lot.
    lots = [e["navision_lot"] for e in report["manufacturing_sequence"]]
    assert sorted(lots) == ["A1", "A2", "B1"]
    # The joined summary is the per-file sums; being correct, the evaluator's
    # cross-check against it stays silent.
    assert combined["mfg_summary"] == {"mfg_rolls": 4, "mfg_lf": 300,
                                       "mfg_sf": 4500}
    assert not any("MFG summary" in w for w in report["warnings"]), \
        report["warnings"]


def test_combined_collapses_shared_layouts():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    # The point of combined mode: a layout that appears in both files is one
    # distinct layout in the joined order, so those rolls are produced back
    # to back at no setup cost instead of being set up once per file.
    ext_a = _extraction("A.xlsx", [_roll("A1", ("FG", 177), ("WHI", 5), sort=1),
                                   _roll("A2", ("FG", 182), sort=2)])
    ext_b = _extraction("B.xlsx", [_roll("B1", ("FG", 177), ("WHI", 5), sort=1)])
    _, report = app.evaluate_combined([ext_a, ext_b])
    # A1 and B1 share a layout signature -> two distinct layouts, not three.
    assert report["distinct_layout_count"] == 2


def test_analyse_upload_rejects_non_workbook():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    # Random bytes are not a valid .xlsx; the pipeline must fail loudly, not
    # return a bogus result.
    raised = False
    try:
        app.analyse_upload("junk.xlsx", b"not a real workbook")
    except Exception:  # noqa: BLE001
        raised = True
    assert raised


def test_item_requirement_rows_formatting():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    # The pure row builder: pounds get one decimal and a thousands separator,
    # widths lose a trailing .0 but keep genuine fractions, colour names are
    # shown where they differ from the code, and SKUs are never coerced.
    items = [
        {"item_number": 121051, "yarn_position": "Y1",
         "yarn_type": "5040 XP+ (6Pin)", "color_code": "FG",
         "color_name": "FIELD GREEN", "lbs_needed": 4653.369708,
         "max_width_in": 182.0, "bobbins_required": 546},
        {"item_number": "145190A", "yarn_position": "Y2",
         "yarn_type": "MF TXT 7200/10", "color_code": "WHI",
         "color_name": "WHI", "lbs_needed": 950,
         "max_width_in": 12.5, "bobbins_required": 38},
    ]
    rows = app._item_requirement_rows(items)
    assert rows[0] == {"item_number": "121051",
                       "yarn_type": "5040 XP+ (6Pin)",
                       "colour": "FIELD GREEN", "lbs_needed": "4,653.4",
                       "max_width_in": "182", "bobbins_required": "546"}
    # A colour name equal to the code falls back to the bare code.
    assert rows[1] == {"item_number": "145190A",
                       "yarn_type": "MF TXT 7200/10",
                       "colour": "WHI", "lbs_needed": "950.0",
                       "max_width_in": "12.5", "bobbins_required": "38"}


# The render harness above has no yarn_lbs block; this twin carries one, so
# the report gains `item_requirements` and the section must render.
_ITEM_REQ_HARNESS = """
import streamlit as st
import app
from evaluate import evaluate

rolls = [{"navision_lot": "L1", "sort": 1, "roll_type": "FIELD",
          "roll_qty": 1, "mfg_roll_length_lf": 100, "total_mfg_sf": 1500,
          "layout_signature": "177FG|5WHI", "layout_group": None,
          "segments": [{"color_code": "FG", "width_in": 177},
                       {"color_code": "WHI", "width_in": 5}]}]
extraction = {
    "source_file": "SAMPLE.xlsx", "rolls": rolls,
    "yarn_lbs": [{"yarn_position": "Y1", "yarn_type": "5040 XP+ (6Pin)",
                  "colors": [{"color_code": "FG", "color_name": "FIELD GREEN",
                              "sku": 121051, "lbs_needed": 4653.37},
                             {"color_code": "WHI", "color_name": "WHITE",
                              "sku": "145190A", "lbs_needed": 1644.88}]}]}
report = evaluate(rolls, extraction=extraction)
app._render_report(st, "SAMPLE.xlsx", extraction, report)
"""


def test_render_report_shows_item_batch_requirements():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    at = AppTest.from_string(_ITEM_REQ_HARNESS).run(timeout=30)
    assert not at.exception, at.exception
    markdown = [m.value for m in at.markdown]
    assert any("Item batch requirements" in v for v in markdown)
    table = next(v for v in markdown if "| Item # |" in v)
    # FG spans 177" in the only roll -> ceil(177 x 3) = 531 bobbins; the
    # pounds are formatted to one decimal with a thousands separator, and the
    # string SKU passes through untouched.
    assert "| 121051 |" in table
    assert "4,653.4" in table
    assert "| 531 |" in table
    assert "145190A" in table
    assert "FIELD GREEN" in table
    # A single file is not a combined order -> no combined-mode caption.
    assert not any("summed across the input files" in c.value
                   for c in at.caption)


def test_render_item_requirements_empty_list_and_combined_caption():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    # Empty (but present) item_requirements: the heading and a "none found"
    # caption, no table. A `source_files` key marks a joined order and adds
    # the combined-mode caption under the table.
    harness = """
import streamlit as st
import app
app._render_item_requirements(
    st, {"source_file": "S.xlsx"}, {"item_requirements": []})
app._render_item_requirements(
    st, {"source_file": "A.xlsx + B.xlsx", "source_files": ["A.xlsx", "B.xlsx"]},
    {"item_requirements": [
        {"item_number": 121051, "yarn_position": "Y1", "yarn_type": "5040",
         "color_code": "FG", "color_name": "FIELD GREEN",
         "lbs_needed": 200.0, "max_width_in": 177, "bobbins_required": 531}]})
"""
    at = AppTest.from_string(harness).run(timeout=30)
    assert not at.exception, at.exception
    headings = [m.value for m in at.markdown
                if "Item batch requirements" in m.value]
    assert len(headings) == 2
    captions = [c.value for c in at.caption]
    assert any("No item requirements were found" in c for c in captions)
    assert any("summed across the input files" in c for c in captions)


def test_evaluate_combined_carries_item_requirements():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    # Two files, each stating a yarn_lbs block: the joined order carries the
    # merged block, so the combined report gets item_requirements with the
    # pounds summed and the max width taken across all combined rolls.
    yarn = [{"yarn_position": "Y1", "yarn_type": "5040 XP+",
             "colors": [{"color_code": "FG", "color_name": "FIELD GREEN",
                         "sku": 121051, "lbs_needed": 100.0},
                        {"color_code": "WHI", "color_name": "WHITE",
                         "sku": 121052, "lbs_needed": 10.0}]}]
    ext_a = _extraction("A.xlsx", [_roll("A1", ("FG", 182), sort=1)],
                        yarn_lbs=yarn)
    ext_b = _extraction("B.xlsx", [_roll("B1", ("FG", 177), ("WHI", 5), sort=1)],
                        yarn_lbs=yarn)
    _, report = app.evaluate_combined([ext_a, ext_b])
    reqs = {r["item_number"]: r for r in report["item_requirements"]}
    # Pounds doubled; FG's widest roll is A1's 182", WHI's is B1's 5".
    assert reqs[121051]["lbs_needed"] == 200
    assert reqs[121051]["max_width_in"] == 182
    assert reqs[121051]["bobbins_required"] == 546
    assert reqs[121052]["lbs_needed"] == 20
    assert reqs[121052]["bobbins_required"] == 15


def _run_standalone():
    if not _HAVE_DEPS:
        print(f"  SKIP  all tests: {_skip_reason()}")
        print("\n0 tests run (dependencies unavailable)")
        return 0
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
