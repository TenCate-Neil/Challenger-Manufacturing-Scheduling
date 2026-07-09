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
    # Likewise no (truthy) bobbin_usage key -> no Item bobbin usage card.
    assert not any("Item bobbin usage" in m.value for m in at.markdown)


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


def _bobbin_usage_block(fresh):
    """A synthetic `bobbin_usage` block shaped exactly like the frozen Phase 4
    report contract — built by hand, never by the bobbin module, so these
    tests pin the app's rendering of the schema and nothing else. With a
    known fresh bobbin weight the third roll needs a swap first (the
    cumulative draw would exceed the fresh weight), and only the 531 bobbins
    of the 177" depletion class run short — `bobbins_swapped` is deliberately
    smaller than the roll's 546 hanging so the tests can tell which count the
    renderers use. With `fresh=None` the swap figures are None and no roll is
    flagged, exactly as the contract states."""
    known = fresh is not None
    return {
        "items": [{
            "item_number": "121051",
            "yarn_type": "5040 XP+ 6Pin",
            "color": "FIELD GREEN",
            "weight_lb_per_sqft": 0.00123,
            "fresh_bobbin_weight_lb": fresh,
            "rolls": [
                {"position": 1, "navision_lot": "L2", "item_width_in": 177.0,
                 "bobbins_hanging": 531, "length_lf": 95.0,
                 "lb_per_bobbin": 0.0032, "cumulative_lb_per_bobbin": 0.0032,
                 "swap_before": False,
                 "bobbins_swapped": 0 if known else None},
                {"position": 2, "navision_lot": "L1", "item_width_in": 182.0,
                 "bobbins_hanging": 546, "length_lf": 120.0,
                 "lb_per_bobbin": 0.0041, "cumulative_lb_per_bobbin": 0.0073,
                 "swap_before": False,
                 "bobbins_swapped": 0 if known else None},
                {"position": 3, "navision_lot": "L1", "item_width_in": 182.0,
                 "bobbins_hanging": 546, "length_lf": 120.0,
                 "lb_per_bobbin": 0.0041,
                 "cumulative_lb_per_bobbin": 0.0041 if known else 0.0114,
                 "swap_before": known,
                 "bobbins_swapped": 531 if known else None},
            ],
            "bobbin_groups": [
                {"width_in": 177.0, "bobbin_count": 531, "rolls_fed": 3,
                 "lb_drawn_per_bobbin": 0.0114,
                 "swap_count": 1 if known else None,
                 "fresh_bobbins_consumed": 1062 if known else None,
                 "final_remaining_lb": (fresh - 0.0041) if known else None},
                {"width_in": 5.0, "bobbin_count": 15, "rolls_fed": 2,
                 "lb_drawn_per_bobbin": 0.0082,
                 "swap_count": 0 if known else None,
                 "fresh_bobbins_consumed": 15 if known else None,
                 "final_remaining_lb": (fresh - 0.0082) if known else None},
            ],
            "totals": {
                "rolls_with_item": 3,
                "total_lb_per_bobbin": 0.0114,
                "swap_count": 1 if known else None,
                "estimated_fresh_bobbins_consumed": 546 if known else None,
                "final_remaining_lb_per_bobbin":
                    (fresh - 0.0041) if known else None,
            },
        }],
        "assumptions": "Positions align from the front of the machine, "
                       "with no swap margin.",
        "warnings": ["Synthetic bobbin warning for the tests."],
    }


def _report_with_bobbin_usage(fresh):
    """A real evaluate() report over synthetic rolls, with the frozen-contract
    `bobbin_usage` block injected by hand — the rest of the run sheet stays
    exercised end to end without depending on how the evaluator populates
    the key."""
    rolls = [_roll("L1", ("FG", 182), sort=1, qty=2, lf=120),
             _roll("L2", ("FG", 177), ("WHI", 5), sort=2, lf=95)]
    report = evaluate(rolls, extraction={"source_file": "SAMPLE.xlsx"})
    report["bobbin_usage"] = _bobbin_usage_block(fresh)
    return report


def test_bobbin_usage_rows_and_totals_formatting():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    # The pure row builder: widths lose a trailing .0, per-bobbin pounds get
    # four decimals, and the swap flag stays a bool for the renderers.
    item = _bobbin_usage_block(0.01)["items"][0]
    rows = app._bobbin_usage_rows(item)
    assert rows[0] == {"position": "1", "navision_lot": "L2",
                       "item_width_in": "177", "bobbins_hanging": "531",
                       "lb_per_bobbin": "0.0032",
                       "cumulative_lb_per_bobbin": "0.0032",
                       "swap_before": False, "bobbins_swapped": "0"}
    assert rows[2]["swap_before"] is True
    # The swap replaces just the positions that run short, not the roll's
    # full hanging count.
    assert rows[2]["bobbins_swapped"] == "531"
    assert rows[2]["bobbins_hanging"] == "546"
    parts = app._bobbin_usage_totals_parts(item)
    assert "rolls with this item: 3" in parts
    assert "total drawn per bobbin (deepest position): 0.0114 lb" in parts
    assert "bobbin swaps: 1" in parts
    assert "fresh bobbins consumed: 546" in parts
    assert "left per bobbin at the end (deepest position): 0.0059 lb" in parts
    # With no fresh weight the swap figures are None and drop out entirely.
    parts_none = app._bobbin_usage_totals_parts(
        _bobbin_usage_block(None)["items"][0])
    assert parts_none == ["rolls with this item: 3",
                          "total drawn per bobbin (deepest position): "
                          "0.0114 lb"]


def test_bobbin_groups_rows_formatting():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    # The depletion-groups row builder: widths lose a trailing .0, pounds get
    # four decimals, and the swap-dependent columns render as "-" when the
    # fresh bobbin weight is unknown.
    groups = app._bobbin_groups_rows(_bobbin_usage_block(0.01)["items"][0])
    assert groups[0] == {"width_in": "177", "bobbin_count": "531",
                         "rolls_fed": "3",
                         "lb_drawn_per_bobbin": "0.0114",
                         "swap_count": "1",
                         "fresh_bobbins_consumed": "1062",
                         "final_remaining_lb": "0.0059"}
    assert groups[1]["width_in"] == "5"
    assert groups[1]["swap_count"] == "0"
    groups_none = app._bobbin_groups_rows(
        _bobbin_usage_block(None)["items"][0])
    assert groups_none[0]["lb_drawn_per_bobbin"] == "0.0114"
    assert groups_none[0]["swap_count"] == "-"
    assert groups_none[0]["fresh_bobbins_consumed"] == "-"
    assert groups_none[0]["final_remaining_lb"] == "-"
    # An item without the key (older reports) renders no groups table.
    assert app._bobbin_groups_rows({"rolls": []}) == []


def test_run_sheet_pdf_bobbin_usage_section_and_swap_band():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    if not _have_fpdf():
        return  # skip: fpdf2 unavailable (or its install is broken)
    # A report carrying bobbin_usage with a known fresh weight: the PDF gains
    # the Item bobbin usage section — item metadata, the roll table, the
    # depletion-groups table, and a red BOBBIN SWAP band before the one roll
    # flagged swap_before.
    report = _report_with_bobbin_usage(0.01)
    content = _pdf_content(bytes(app.build_run_sheet_pdf("SAMPLE.xlsx",
                                                         report)))
    assert b"Item bobbin usage" in content
    assert b"121051" in content
    assert b"FIELD GREEN" in content
    assert b"0.00123" in content   # weight_lb_per_sqft, five decimals
    assert b"Fresh bobbin: 0.01 lb" in content
    assert b"0.0041" in content    # lb/bobbin, four decimals
    # The band names the bobbins actually replaced (`bobbins_swapped`, 531),
    # not the roll's full hanging count (546).
    assert content.count(b"BOBBIN SWAP - replace 531 bobbins") == 1
    assert b"BOBBIN SWAP - replace 546 bobbins" not in content
    # The band is drawn in red, like the SETUP CHANGE band: the red
    # non-stroking colour operator is set just before its text.
    idx = content.index(b"BOBBIN SWAP")
    assert b"0.7843 0 0 rg" in content[max(0, idx - 400):idx]
    # The depletion-groups table: heading, headers, and the per-group
    # figures (1062 fresh bobbins on the deep class).
    assert b"Bobbin depletion groups" in content
    assert b"Rolls fed" in content
    assert b"1062" in content
    # The totals line and the assumptions sentence both land in the PDF.
    assert b"bobbin swaps: 1" in content
    assert b"no swap margin" in content


def test_run_sheet_pdf_bobbin_usage_without_fresh_weight():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    if not _have_fpdf():
        return  # skip: fpdf2 unavailable (or its install is broken)
    # No fresh bobbin weight: the section still renders (with a note that the
    # weight is not yet filled in), but no swap can be planned — no BOBBIN
    # SWAP band anywhere and the None swap totals drop out. The depletion
    # groups still render, with their swap-dependent columns dashed.
    report = _report_with_bobbin_usage(None)
    content = _pdf_content(bytes(app.build_run_sheet_pdf("SAMPLE.xlsx",
                                                         report)))
    assert b"Item bobbin usage" in content
    assert b"not yet filled in" in content
    assert b"BOBBIN SWAP" not in content
    assert b"bobbin swaps" not in content
    assert b"rolls with this item: 3" in content
    assert b"Bobbin depletion groups" in content
    assert b"1062" not in content  # fresh bobbins per group is unknown


def test_run_sheet_pdf_without_bobbin_usage_key_unchanged():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    if not _have_fpdf():
        return  # skip: fpdf2 unavailable (or its install is broken)
    # Without the key the run sheet must build exactly as before: no Item
    # bobbin usage section and no swap bands. The key is popped explicitly so
    # the test stays valid even once the evaluator starts adding it.
    rolls = [_roll("L1", ("FG", 182), sort=1, qty=2, lf=120),
             _roll("L2", ("FG", 177), ("WHI", 5), sort=2, lf=95)]
    report = evaluate(rolls, extraction={"source_file": "SAMPLE.xlsx"})
    report.pop("bobbin_usage", None)
    pdf = app.build_run_sheet_pdf("SAMPLE.xlsx", report)
    assert bytes(pdf[:5]) == b"%PDF-"
    content = _pdf_content(bytes(pdf))
    assert b"Item bobbin usage" not in content
    assert b"BOBBIN SWAP" not in content
    assert b"Bobbin depletion groups" not in content


# The render harness with a frozen-contract bobbin_usage block injected into
# the report, so the on-screen Item bobbin usage card must render.
_BOBBIN_USAGE_HARNESS = """
import streamlit as st
import app
from evaluate import evaluate

def _roll(lot, *segs, sort=None, lf=100):
    return {"navision_lot": lot, "sort": sort, "roll_type": "FIELD",
            "roll_qty": 1, "mfg_roll_length_lf": lf, "total_mfg_sf": 1500,
            "layout_signature": "|".join(f"{w}{c}" for c, w in segs),
            "layout_group": None,
            "segments": [{"color_code": c, "width_in": w} for c, w in segs]}

rolls = [_roll("L1", ("FG", 182), sort=1, lf=120),
         _roll("L2", ("FG", 177), ("WHI", 5), sort=2, lf=95)]
report = evaluate(rolls, extraction={"source_file": "SAMPLE.xlsx"})
report["bobbin_usage"] = {
    "items": [{"item_number": "121051", "yarn_type": "5040 XP+ 6Pin",
               "color": "FIELD GREEN", "weight_lb_per_sqft": 0.00123,
               "fresh_bobbin_weight_lb": 0.005,
               "rolls": [{"position": 1, "navision_lot": "L1",
                          "item_width_in": 182.0, "bobbins_hanging": 546,
                          "length_lf": 120.0, "lb_per_bobbin": 0.0041,
                          "cumulative_lb_per_bobbin": 0.0041,
                          "swap_before": False, "bobbins_swapped": 0},
                         {"position": 2, "navision_lot": "L2",
                          "item_width_in": 177.0, "bobbins_hanging": 531,
                          "length_lf": 95.0, "lb_per_bobbin": 0.0032,
                          "cumulative_lb_per_bobbin": 0.0032,
                          "swap_before": True, "bobbins_swapped": 531}],
               "bobbin_groups": [
                   {"width_in": 177.0, "bobbin_count": 531, "rolls_fed": 2,
                    "lb_drawn_per_bobbin": 0.0073, "swap_count": 1,
                    "fresh_bobbins_consumed": 1062,
                    "final_remaining_lb": 0.0018},
                   {"width_in": 5.0, "bobbin_count": 15, "rolls_fed": 1,
                    "lb_drawn_per_bobbin": 0.0041, "swap_count": 0,
                    "fresh_bobbins_consumed": 15,
                    "final_remaining_lb": 0.0009}],
               "totals": {"rolls_with_item": 2,
                          "total_lb_per_bobbin": 0.0073,
                          "swap_count": 1,
                          "estimated_fresh_bobbins_consumed": 531,
                          "final_remaining_lb_per_bobbin": 0.0018}}],
    "assumptions": "Positions align from the front of the machine, "
                   "with no swap margin.",
    "warnings": ["Synthetic bobbin warning for the tests."]}
app._render_report(st, "SAMPLE.xlsx", {"source_file": "SAMPLE.xlsx"}, report)
"""


def test_render_report_shows_item_bobbin_usage():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    at = AppTest.from_string(_BOBBIN_USAGE_HARNESS).run(timeout=30)
    assert not at.exception, at.exception
    markdown = [m.value for m in at.markdown]
    assert any("Item bobbin usage" in v for v in markdown)
    # The per-item subheading carries the item number, yarn type and colour.
    assert any("Item 121051" in v and "FIELD GREEN" in v for v in markdown)
    # The metadata line: weight to five decimals, fresh bobbin weight shown.
    assert any("0.00123 lb/sqft" in v and "0.005 lb" in v for v in markdown)
    # The roll table: positions, bobbins, four-decimal pounds, and the SWAP
    # marker on the flagged roll only.
    table = next(v for v in markdown if "| Position |" in v)
    assert "| 531 |" in table
    assert "0.0041" in table
    assert table.count("SWAP") == 1
    # The depletion-groups table: one row per class, swap-dependent columns
    # filled in (fresh weight known).
    assert any("Bobbin depletion groups" in v for v in markdown)
    groups_table = next(v for v in markdown if "| Width (in) |" in v)
    assert "| 177 |" in groups_table and "| 1062 |" in groups_table
    assert "| 5 |" in groups_table and "| 15 |" in groups_table
    # The totals line and the caption-text assumptions sentence.
    assert any("bobbin swaps: 1" in v for v in markdown)
    assert any("no swap margin" in c.value for c in at.caption)
    # Its warnings are shown as warnings, like the report's own.
    assert any("Synthetic bobbin warning" in w.value for w in at.warning)


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
