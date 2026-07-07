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
    # Both the JSON report and the printable run sheet are downloadable
    # (WeasyPrint is a declared dependency, so the PDF button is present).
    labels = [b.label for b in at.download_button]
    assert "Download sequence report (JSON)" in labels
    assert "Download run sheet (PDF)" in labels


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


def test_build_run_sheet_html_content():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    # Two-roll entry (qty=2) must expand to two rows, matching the "Full
    # manufacturing order" view, and the header/columns must be present.
    rolls = [_roll("L1", ("FG", 182), sort=1, qty=2, panels="P1-P2"),
             _roll("L2", ("FG", 100), ("WHI", 82), sort=2, panels="P3")]
    report = evaluate(rolls, extraction={"source_file": "SAMPLE.xlsx"})
    html_doc = app.build_run_sheet_html("SAMPLE.xlsx", report)

    # Header facts.
    assert "SAMPLE.xlsx" in html_doc
    assert "Total setup cost" in html_doc
    assert "Navision lot #" in html_doc
    assert "Panel #s" in html_doc
    # Panel numbers and lots are printed.
    assert "P1-P2" in html_doc
    assert "P3" in html_doc
    # qty=2 expands to two physical rolls -> three rows in total.
    assert html_doc.count("<tr>") == 1 + 3  # header row + three body rows
    # Colour bars are rendered (reusing _bar_html): green field segment present.
    assert "background:#3f8f3a" in html_doc


def test_build_run_sheet_pdf_bytes():
    if not _HAVE_DEPS:
        return  # skip: dependency-free environment
    try:
        import weasyprint  # noqa: F401
    except Exception:  # noqa: BLE001
        return  # skip: WeasyPrint (or its native libs) unavailable
    rolls = [_roll("L1", ("FG", 182), sort=1),
             _roll("L2", ("FG", 100), ("WHI", 82), sort=2)]
    report = evaluate(rolls, extraction={"source_file": "SAMPLE.xlsx"})
    pdf = app.build_run_sheet_pdf("SAMPLE.xlsx", report)
    assert isinstance(pdf, (bytes, bytearray))
    assert pdf[:5] == b"%PDF-"


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
