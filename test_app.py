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
    try:
        import fpdf  # noqa: F401
        assert "Download run sheet (PDF)" in labels
    except ImportError:
        pass


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
    report = evaluate(rolls, extraction={"source_file": "SAMPLE.xlsx"})
    rows = app._run_sheet_rows(report)

    # qty=2 + qty=1 -> three physical rolls, numbered 1..3 (order is optimised,
    # so assertions below are order-independent).
    assert [r["position"] for r in rows] == [1, 2, 3]
    lots = [r["navision_lot"] for r in rows]
    assert lots.count("L1") == 2 and lots.count("L2") == 1
    # The L1 rows carry its panels, length, and parsed layout profile.
    l1_rows = [r for r in rows if r["navision_lot"] == "L1"]
    assert all(r["panel_numbers"] == "P1-P2" for r in l1_rows)
    assert all(r["length_lf"] == 120 for r in l1_rows)
    assert all(("FG", 182) in r["profile"] for r in l1_rows)
    # The first roll is a fresh start; every change is a human string.
    assert rows[0]["change"] == "start"
    assert all(isinstance(r["change"], str) for r in rows)


def test_distinct_layouts_group_count_and_lots():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    # The bottom breakdown groups by distinct layout, sums the physical rolls
    # that use each, and lists the lots — so the exact threading widths can be
    # spelled out per layout.
    rolls = [_roll("L1", ("FG", 182), sort=1, qty=2, panels="1"),
             _roll("L2", ("FG", 182), sort=2, panels="2"),
             _roll("L3", ("FG", 177), ("WHI", 5), sort=3, panels="3")]
    report = evaluate(rolls, extraction={"source_file": "SAMPLE.xlsx"})
    layouts = app._distinct_layouts(report)

    by_profile = {tuple(l["profile"]): l for l in layouts}
    # L1 (qty=2) and L2 share the all-FG layout -> three physical rolls, two lots.
    full_fg = by_profile[(("FG", 182),)]
    assert full_fg["roll_count"] == 3
    assert set(full_fg["lots"]) == {"L1", "L2"}
    # The FG/WHI layout carries its exact segment widths for the breakdown text.
    fg_whi = by_profile[(("FG", 177), ("WHI", 5))]
    assert fg_whi["roll_count"] == 1
    assert fg_whi["lots"] == ["L3"]
    # There are exactly two distinct layouts in this order.
    assert len(layouts) == 2


def test_build_run_sheet_pdf_bytes():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    try:
        import fpdf  # noqa: F401
    except Exception:  # noqa: BLE001
        return  # skip: fpdf2 unavailable
    rolls = [_roll("L1", ("FG", 182), sort=1, qty=2, panels="P1-P2"),
             _roll("L2", ("FG", 100), ("WHI", 82), sort=2, panels="P3")]
    report = evaluate(rolls, extraction={"source_file": "SAMPLE.xlsx"})
    pdf = app.build_run_sheet_pdf("SAMPLE.xlsx", report)
    assert isinstance(pdf, (bytes, bytearray))
    assert bytes(pdf[:5]) == b"%PDF-"
    # A non-trivial document (header + legend + three rows) is well over 1 KB.
    assert len(pdf) > 1000


def _extraction(name, rolls):
    # A synthetic extraction dict shaped like `extract_workbook`'s output,
    # with a correct MFG summary derived from the rolls (roll_qty, LF and SF
    # summed the same way the evaluator's cross-check sums the sequence).
    return {
        "source_file": name,
        "rolls": rolls,
        "roll_count": len(rolls),
        "mfg_summary": {
            "mfg_rolls": sum(r["roll_qty"] for r in rolls),
            "mfg_lf": sum(r["mfg_roll_length_lf"] for r in rolls),
            "mfg_sf": sum(r["total_mfg_sf"] for r in rolls),
        },
        "warnings": [],
    }


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
