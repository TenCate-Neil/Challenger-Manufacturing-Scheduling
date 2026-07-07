#!/usr/bin/env python3
"""
Roll sequencing — MVP front end (Phase 5).

A thin Streamlit front end over the same core functions used by the CLI
(docs/optimisation_plan.md, Phase 5). It does no work of its own: it uploads an
order workbook, calls the existing extractor (`extract_turf_layout`) and the
Phase 4 evaluator (`evaluate`), and shows the ordered manufacturing sequence,
the achieved setup cost, the solution quality, the conservation result, and the
transition breakdown. The JSON report can be downloaded.

There is no logic duplicated here and nothing to deploy — it runs locally:

    streamlit run app.py

The extraction/optimisation pipeline is factored into `analyse_upload`, which
imports no Streamlit, so it can be exercised without a browser. Streamlit
itself is imported inside `main`, keeping this module importable for tests even
when Streamlit is not installed.
"""

from pathlib import Path

from evaluate import (
    DEFAULT_EXACT_MAX_LAYOUTS,
    DEFAULT_EXACT_ORACLE_MAX_LAYOUTS,
    evaluate,
    report_json,
)
from extract_turf_layout import TemplateMismatch, extract_workbook


# --------------------------------------------------------------------------
# Pipeline (no Streamlit) — reused by the UI and testable on its own
# --------------------------------------------------------------------------
def analyse_upload(filename, file_bytes,
                   exact_max_layouts=DEFAULT_EXACT_MAX_LAYOUTS,
                   oracle_max_layouts=DEFAULT_EXACT_ORACLE_MAX_LAYOUTS):
    """Run the full pipeline on one uploaded workbook's bytes.

    The workbook bytes are passed straight to the extractor, which reads them
    in memory. Nothing is written to disk, so there is no temporary file for
    Windows (or antivirus scanning it) to lock — earlier a temp-file approach
    failed with "[Errno 13] Permission denied". Returns `(extraction, report)`:
    the raw extraction dict from `extract_turf_layout` and the Phase 4
    evaluation report from `evaluate`. Raises `TemplateMismatch` if the
    workbook is not a recognised FIELD LAYOUT order."""
    extraction = extract_workbook(file_bytes, source_name=filename)

    report = evaluate(
        extraction.get("rolls", []),
        exact_max_layouts=exact_max_layouts,
        oracle_max_layouts=oracle_max_layouts,
        extraction=extraction,
    )
    return extraction, report


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
def _render_report(st, filename, extraction, report):
    quality = report["solution_quality"]
    conservation = report["conservation"]
    breakdown = report["transition_breakdown"]

    st.subheader(filename)

    # Headline numbers.
    top = st.columns(4)
    top[0].metric("Rolls", report["roll_count"])
    top[1].metric("Distinct layouts", report["distinct_layout_count"])
    top[2].metric("Achieved setup cost", f"{report['achieved_cost_in']} in")
    method_note = "proven optimum" if report["optimal"] else "near-optimal"
    top[3].metric("Method", method_note,
                  help=report["method"])

    # Conservation — the sequence must be a faithful reordering of the order.
    if conservation["passed"]:
        st.success("Conservation check passed: every roll, quantity and total "
                   "is preserved; only the manufacturing order changed.")
    else:
        st.error("Conservation check failed — the sequence is not a faithful "
                 "reordering of the order:")
        for discrepancy in conservation["discrepancies"]:
            st.write(f"- {discrepancy}")

    # Solution quality.
    st.markdown("**Solution quality**")
    quality_cols = st.columns(3)
    quality_cols[0].metric("Lower bound (MST)", f"{quality['lower_bound_in']} in")
    gap_ratio = quality["gap_ratio"]
    quality_cols[1].metric(
        "Gap to lower bound", f"{quality['gap_to_lower_bound_in']} in",
        delta=(f"{gap_ratio * 100:.1f}%" if gap_ratio is not None else None),
        delta_color="off")
    if quality["exact_optimum_in"] is not None:
        source = "proven" if quality["proven_optimal"] else "exact oracle"
        quality_cols[2].metric(
            "Exact optimum", f"{quality['exact_optimum_in']} in",
            delta=f"gap {quality['gap_to_exact_optimum_in']} in ({source})",
            delta_color="off")
    else:
        quality_cols[2].metric(
            "Exact optimum", "n/a",
            help="Too many distinct layouts to solve exactly as an oracle; "
                 "quality is reported against the lower bound.")

    st.caption(
        f"As-extracted order cost, for reference only (assigned by sales, not a "
        f"target): {report['reference_only_as_extracted_cost_in']} in.")

    # Manufacturing sequence.
    st.markdown("**Manufacturing sequence**")
    st.dataframe(report["manufacturing_sequence"], hide_index=True)

    # Transition breakdown.
    st.markdown("**Transition breakdown**")
    breakdown_cols = st.columns(4)
    breakdown_cols[0].metric("Transitions", breakdown["transition_count"])
    breakdown_cols[1].metric("Zero-cost (identical)",
                             breakdown["zero_cost_transitions"])
    breakdown_cols[2].metric("Max change", f"{breakdown['max_transition_cost']} in")
    breakdown_cols[3].metric("Mean change", f"{breakdown['mean_transition_cost']} in")
    if breakdown["transition_costs"]:
        st.caption("Per-transition setup change cost (inches), in "
                   "manufacturing order:")
        st.bar_chart(breakdown["transition_costs"])

    # Warnings and JSON download.
    for warning in report["warnings"]:
        st.warning(warning)

    st.download_button(
        "Download sequence report (JSON)",
        data=report_json(report),
        file_name=f"{Path(filename).stem}.sequence.json",
        mime="application/json",
    )


def main():
    import streamlit as st

    st.set_page_config(page_title="Roll Sequencing", layout="wide")
    st.title("Roll sequencing")
    st.write(
        "Upload a turf order workbook (the `FIELD LAYOUT` sheet). The app "
        "extracts the roll layouts and orders them for manufacture so the "
        "total machine setup change effort is as low as reasonably achievable. "
        "Reordering never changes what is produced — every roll and quantity "
        "is preserved.")

    with st.sidebar:
        st.header("Settings")
        exact_max_layouts = st.number_input(
            "Solve exactly up to N distinct layouts", min_value=1, max_value=22,
            value=DEFAULT_EXACT_MAX_LAYOUTS,
            help="At or below this many distinct layouts the sequence is solved "
                 "exactly (Held–Karp, proven optimum); above it a heuristic is "
                 "used.")
        oracle_max_layouts = st.number_input(
            "Report exact gap up to N layouts", min_value=1, max_value=22,
            value=DEFAULT_EXACT_ORACLE_MAX_LAYOUTS,
            help="For heuristic results, still solve exactly as an oracle to "
                 "report the true optimality gap up to this many layouts.")

    uploads = st.file_uploader(
        "Order workbook(s)", type=["xlsx"], accept_multiple_files=True)

    if not uploads:
        st.info("Upload one or more `.xlsx` order workbooks to begin.")
        return

    for upload in uploads:
        try:
            extraction, report = analyse_upload(
                upload.name, upload.getvalue(),
                exact_max_layouts=int(exact_max_layouts),
                oracle_max_layouts=int(oracle_max_layouts))
        except TemplateMismatch as exc:
            st.error(f"{upload.name}: not a recognised FIELD LAYOUT workbook "
                     f"({exc}).")
            continue
        except Exception as exc:  # noqa: BLE001
            st.error(f"{upload.name}: could not process ({exc}).")
            continue

        _render_report(st, upload.name, extraction, report)
        st.divider()


if __name__ == "__main__":
    main()
