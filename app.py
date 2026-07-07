#!/usr/bin/env python3
"""
Roll sequencing — MVP front end (Phase 5).

A thin Streamlit front end over the same core functions used by the CLI
(docs/optimisation_plan.md, Phase 5). It does no work of its own: it uploads an
order workbook, calls the existing extractor (`extract_turf_layout`) and the
Phase 4 evaluator (`evaluate`), and shows the ordered manufacturing sequence,
the achieved setup cost, the solution quality, the conservation result, and the
transition breakdown. The JSON report can be downloaded, as can a printable PDF
"run sheet" for the manufacturing floor (one row per physical roll, with the
layout colour bars rendered via WeasyPrint).

There is no logic duplicated here and nothing to deploy — it runs locally:

    streamlit run app.py

The extraction/optimisation pipeline is factored into `analyse_upload`, and the
run sheet into `build_run_sheet_html` / `build_run_sheet_pdf`; none of these
import Streamlit, so they can be exercised without a browser. Streamlit itself is
imported inside `main`, keeping this module importable for tests even when
Streamlit is not installed.
"""

import hashlib
import html
import os
import tempfile
from pathlib import Path

from evaluate import (
    DEFAULT_EXACT_MAX_LAYOUTS,
    DEFAULT_EXACT_ORACLE_MAX_LAYOUTS,
    evaluate,
    report_json,
)
from extract_turf_layout import TemplateMismatch, extract_workbook
from roll_sequencing import _clean_number, parse_signature, profile_cost


# --------------------------------------------------------------------------
# Pipeline (no Streamlit) — reused by the UI and testable on its own
# --------------------------------------------------------------------------
def analyse_upload(filename, file_bytes,
                   exact_max_layouts=DEFAULT_EXACT_MAX_LAYOUTS,
                   oracle_max_layouts=DEFAULT_EXACT_ORACLE_MAX_LAYOUTS):
    """Run the full pipeline on one uploaded workbook's bytes.

    The extractor reads the workbook as a file path (it opens the underlying
    zip to resolve the embedded brand logo), so the bytes are written to a
    temporary file first. The temp file is closed before the extractor opens
    it and removed afterwards: on Windows a still-open `NamedTemporaryFile`
    cannot be reopened by another handle, which otherwise raises
    "Permission denied". Returns `(extraction, report)`: the raw extraction
    dict from `extract_turf_layout` and the Phase 4 evaluation report from
    `evaluate`. Raises `TemplateMismatch` if the workbook is not a recognised
    FIELD LAYOUT order."""
    suffix = Path(filename).suffix or ".xlsx"
    fd, tmp_path = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as tmp:
            tmp.write(file_bytes)
        extraction = extract_workbook(tmp_path)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    # Preserve the original file name rather than the temp file's.
    extraction["source_file"] = filename

    report = evaluate(
        extraction.get("rolls", []),
        exact_max_layouts=exact_max_layouts,
        oracle_max_layouts=oracle_max_layouts,
        extraction=extraction,
    )
    return extraction, report


# --------------------------------------------------------------------------
# Colour-bar visuals (Steps 1-3)
# --------------------------------------------------------------------------
# Yarn colour codes are drawn as literal colours so the bars read like the
# physical roll: field green stays green, white stays white. Known codes map to
# a sensible real colour; anything unrecognised gets a stable, distinct fallback
# colour so the same code is always the same colour across every bar and legend.
_KNOWN_COLORS = {
    "FG": "#3f8f3a", "GRN": "#2e7d32", "GRE": "#2e7d32", "GREEN": "#2e7d32",
    "DKG": "#1b5e20", "LTG": "#7cb342",
    "LIM": "#9ccc65", "LIME": "#9ccc65",
    "WHI": "#f4f4f4", "WHT": "#f4f4f4", "WHITE": "#f4f4f4",
    "BLK": "#2b2b2b", "BLACK": "#2b2b2b",
    "RED": "#d64541", "MAR": "#7b2b30", "MAROON": "#7b2b30",
    "BLU": "#2f6fb0", "BLUE": "#2f6fb0", "RYL": "#2f6fb0",
    "NVY": "#1f2a52", "NAV": "#1f2a52", "NAVY": "#1f2a52",
    "YEL": "#f2c33d", "YLW": "#f2c33d", "GLD": "#c9a227", "GOLD": "#c9a227",
    "ORG": "#e5852b", "ORA": "#e5852b", "ORANGE": "#e5852b",
    "PUR": "#7b4ea3", "PURPLE": "#7b4ea3",
    "BRN": "#6d4c41", "TAN": "#cbb994",
    "GRY": "#9aa0a6", "GRA": "#9aa0a6", "GRAY": "#9aa0a6", "SIL": "#c2c7cc",
    "TEA": "#1f8f86", "TEAL": "#1f8f86",
    "PNK": "#e57ba0", "PINK": "#e57ba0",
}

# Stable fallback colours for codes not in the map above.
_FALLBACK_PALETTE = [
    "#4e79a7", "#f28e2b", "#59a14f", "#e15759", "#b07aa1",
    "#76b7b2", "#edc948", "#ff9da7", "#9c755f", "#8cd17d",
]


def _color_for_code(code):
    """A stable display colour for a yarn colour/type code."""
    key = str(code).strip().upper()
    if key in _KNOWN_COLORS:
        return _KNOWN_COLORS[key]
    digest = int(hashlib.md5(key.encode("utf-8")).hexdigest(), 16)
    return _FALLBACK_PALETTE[digest % len(_FALLBACK_PALETTE)]


def _text_on(hex_color):
    """Black or white label text, whichever reads better on `hex_color`."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    luminance = 0.2126 * r + 0.7152 * g + 0.0722 * b
    return "#1b1b1b" if luminance > 140 else "#ffffff"


def _profile_of(signature):
    """Parse a layout signature string into an ordered (code, width) profile,
    tolerating a missing/empty signature."""
    if not signature:
        return []
    try:
        return parse_signature(signature)
    except ValueError:
        return []


def _bar_html(profile, height=24, labels=True, min_label_pct=7.0):
    """A roll's threading profile as a horizontal segmented colour bar. Segment
    widths are proportional to inches; a 1px gap between segments (the grey
    container showing through) keeps every boundary visible even between two
    similar colours."""
    total = sum(w for _, w in profile)
    if total <= 0:
        return (f'<div style="height:{height}px;border:1px dashed '
                'rgba(128,128,128,0.6);border-radius:4px;"></div>')
    segments = []
    for code, width in profile:
        pct = 100.0 * width / total
        bg = _color_for_code(code)
        fg = _text_on(bg)
        label = html.escape(str(code)) if labels and pct >= min_label_pct else ""
        title = html.escape(f"{code}: {_clean_number(width)} in")
        segments.append(
            f'<div title="{title}" style="width:{pct:.4f}%;background:{bg};'
            f'color:{fg};display:flex;align-items:center;justify-content:center;'
            'font-size:11px;font-weight:600;line-height:1;overflow:hidden;'
            f'white-space:nowrap;">{label}</div>')
    return (
        f'<div style="display:flex;height:{height}px;width:100%;gap:1px;'
        'background:rgba(128,128,128,0.55);border:1px solid rgba(128,128,128,0.7);'
        f'border-radius:4px;overflow:hidden;">{"".join(segments)}</div>')


def _row_html(left, bar, right, right_strong=False):
    """One labelled bar row: a left caption, the bar, and a right caption."""
    weight = "700" if right_strong else "400"
    return (
        '<div style="display:flex;align-items:center;gap:12px;margin:6px 0;">'
        '<div style="flex:0 0 96px;font-size:12px;font-family:'
        'ui-monospace,SFMono-Regular,Menlo,monospace;opacity:0.85;'
        f'text-align:right;overflow:hidden;white-space:nowrap;">{left}</div>'
        f'<div style="flex:1 1 auto;min-width:0;">{bar}</div>'
        f'<div style="flex:0 0 84px;font-size:12px;font-weight:{weight};'
        f'text-align:right;">{right}</div>'
        '</div>')


def _ordered_codes(signatures):
    """Unique colour codes across the given signatures, in first-appearance
    order, for a compact legend."""
    seen = []
    for signature in signatures:
        for code, _ in _profile_of(signature):
            if code not in seen:
                seen.append(code)
    return seen


def _legend_html(codes):
    items = []
    for code in codes:
        bg = _color_for_code(code)
        items.append(
            '<span style="display:inline-flex;align-items:center;gap:6px;'
            'margin:2px 14px 2px 0;font-size:12px;">'
            f'<span style="width:14px;height:14px;border-radius:3px;background:'
            f'{bg};border:1px solid rgba(128,128,128,0.6);"></span>'
            f'{html.escape(str(code))}</span>')
    return ('<div style="display:flex;flex-wrap:wrap;margin:2px 0 10px;">'
            + "".join(items) + "</div>")


def _reps_of(qty):
    """How many physical rolls a sequence entry stands for (its roll_qty),
    defaulting to 1 when the quantity is missing or not a whole number."""
    if isinstance(qty, bool):
        return 1
    if isinstance(qty, int) and qty > 0:
        return qty
    if isinstance(qty, float) and qty.is_integer() and qty > 0:
        return int(qty)
    return 1


def _render_distinct_layouts(st, layouts):
    """Step 2: each distinct threading profile as a colour bar, with how many
    rolls use it. Placed above Solution quality."""
    box = st.container(border=True)
    box.markdown("#### Distinct layouts")
    box.caption(
        "Every unique threading profile in this order, drawn left-to-right "
        "across the roll width, with how many rolls use each one. Identical "
        "layouts are produced back to back at no setup cost.")

    codes = _ordered_codes(g["layout_signature"] for g in layouts)
    if codes:
        box.markdown(_legend_html(codes), unsafe_allow_html=True)

    rows = []
    for group in layouts:
        profile = _profile_of(group["layout_signature"])
        count = group.get("physical_roll_qty")
        if count is None:
            count = group.get("roll_entry_count")
        noun = "roll" if count == 1 else "rolls"
        rows.append(_row_html(
            left=f'#{group["layout_index"]}',
            bar=_bar_html(profile),
            right=f'{count}&times; {noun}',
            right_strong=True))
    box.markdown('<div>' + "".join(rows) + '</div>', unsafe_allow_html=True)


def _render_combined_order(st, sequence):
    """Step 3: the full run in manufacturing order — one bar per physical roll,
    with the setup change cost incurred to switch to it. Placed at the bottom."""
    box = st.container(border=True)
    box.markdown("#### Full manufacturing order")
    box.caption(
        "The complete run in manufacturing order — one bar per physical roll. "
        "Consecutive identical layouts cost nothing to switch between; each "
        "change shows the inches of threading re-worked to reach it.")

    rows = []
    position = 0
    prev_profile = None
    for entry in sequence:
        profile = _profile_of(entry.get("layout_signature"))
        lot = entry.get("navision_lot")
        for _ in range(_reps_of(entry.get("roll_qty"))):
            position += 1
            if prev_profile is None:
                right, strong = "start", False
            else:
                change = _clean_number(profile_cost(prev_profile, profile))
                right = f"+{change} in" if change else "0 in"
                strong = bool(change)
            label = f'{position}.'
            if lot is not None:
                label += f' {html.escape(str(lot))}'
            rows.append(_row_html(label, _bar_html(profile), right,
                                  right_strong=strong))
            prev_profile = profile
    box.markdown('<div>' + "".join(rows) + '</div>', unsafe_allow_html=True)


# --------------------------------------------------------------------------
# Printable run sheet (HTML -> PDF) — testable without Streamlit
# --------------------------------------------------------------------------
# The manufacturing floor wants a paper run sheet, not JSON. It lists the rolls
# in manufacturing order — one row per physical roll (roll_qty expanded, exactly
# as the "Full manufacturing order" view does) — with the same colour-bar visual
# of each layout so an operator can eyeball the threading. It is built as HTML
# and rendered to PDF with WeasyPrint, which keeps the colour bars intact. The
# HTML builder takes no Streamlit and depends on no PDF library, so it can be
# asserted on directly in tests; only `build_run_sheet_pdf` needs WeasyPrint.
def _run_sheet_row_html(position, lot, panels, length, bar, change):
    """One table row of the run sheet: position, Navision lot, panel numbers,
    length in linear feet, the layout colour bar, and the setup change cost
    incurred to switch to this roll."""
    lot_txt = html.escape(str(lot)) if lot is not None else ""
    panels_txt = html.escape(str(panels)) if panels not in (None, "") else ""
    length_txt = (html.escape(f"{_clean_number(length)}")
                  if isinstance(length, (int, float)) and not isinstance(length, bool)
                  else "")
    return (
        "<tr>"
        f'<td class="pos">{position}</td>'
        f'<td class="lot">{lot_txt}</td>'
        f'<td class="panels">{panels_txt}</td>'
        f'<td class="len">{length_txt}</td>'
        f'<td class="bar">{bar}</td>'
        f'<td class="chg">{html.escape(change)}</td>'
        "</tr>")


def build_run_sheet_html(filename, report):
    """Build the printable run sheet as a standalone HTML document.

    Rolls are listed in manufacturing order, one row per physical roll: the
    `roll_qty` of each sequence entry is expanded so a two-roll entry becomes
    two rows, matching the "Full manufacturing order" view. Each row shows the
    position, Navision lot number, panel numbers, length (LF), a colour-bar
    visual of the layout (the same `_bar_html` mapping used on screen), and the
    per-step setup change cost in inches. A header carries the source file, the
    total achieved setup cost, and the roll/layout counts.

    Takes no Streamlit and no PDF library, so it is testable on its own."""
    sequence = report.get("manufacturing_sequence", [])
    source = report.get("source_file") or filename
    total_cost = report.get("achieved_cost_in")
    roll_count = report.get("roll_count")
    layout_count = report.get("distinct_layout_count")

    codes = _ordered_codes(entry.get("layout_signature") for entry in sequence)
    legend = _legend_html(codes) if codes else ""

    rows = []
    position = 0
    prev_profile = None
    for entry in sequence:
        profile = _profile_of(entry.get("layout_signature"))
        lot = entry.get("navision_lot")
        panels = entry.get("panel_numbers")
        length = entry.get("mfg_roll_length_lf")
        for _ in range(_reps_of(entry.get("roll_qty"))):
            position += 1
            if prev_profile is None:
                change = "start"
            else:
                delta = _clean_number(profile_cost(prev_profile, profile))
                change = f"+{delta} in" if delta else "0 in"
            rows.append(_run_sheet_row_html(
                position, lot, panels, length, _bar_html(profile), change))
            prev_profile = profile

    meta_items = [("Source file", html.escape(str(source)))]
    if total_cost is not None:
        meta_items.append(("Total setup cost", f"{html.escape(str(total_cost))} in"))
    if roll_count is not None:
        meta_items.append(("Rolls", html.escape(str(roll_count))))
    if layout_count is not None:
        meta_items.append(("Distinct layouts", html.escape(str(layout_count))))
    meta_html = "".join(
        f'<div class="meta-item"><span class="meta-label">{label}</span>'
        f'<span class="meta-value">{value}</span></div>'
        for label, value in meta_items)

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Run sheet — {html.escape(str(source))}</title>
<style>
  @page {{ size: A4 landscape; margin: 14mm; }}
  body {{ font-family: "Helvetica Neue", Arial, sans-serif; color: #1b1b1b;
          font-size: 11px; }}
  h1 {{ font-size: 18px; margin: 0 0 8px; }}
  .meta {{ display: flex; flex-wrap: wrap; gap: 6px 28px; margin: 0 0 10px; }}
  .meta-item {{ display: flex; flex-direction: column; }}
  .meta-label {{ font-size: 9px; text-transform: uppercase; letter-spacing: .04em;
                 color: #666; }}
  .meta-value {{ font-size: 13px; font-weight: 600; }}
  .legend {{ margin: 0 0 10px; }}
  table {{ width: 100%; border-collapse: collapse; table-layout: fixed; }}
  th, td {{ border: 1px solid #d0d0d0; padding: 4px 6px; vertical-align: middle;
            text-align: left; box-sizing: border-box; }}
  thead th {{ background: #f2f2f2; font-size: 9px; text-transform: uppercase;
              letter-spacing: .04em; }}
  tbody tr:nth-child(even) td {{ background: #fafafa; }}
  th.pos, td.pos {{ width: 5%; text-align: right;
                    font-variant-numeric: tabular-nums; }}
  th.lot, td.lot {{ width: 15%; font-family: ui-monospace, Menlo, monospace; }}
  th.panels, td.panels {{ width: 14%; }}
  th.len, td.len {{ width: 9%; text-align: right;
                    font-variant-numeric: tabular-nums; }}
  th.bar, td.bar {{ width: 45%; }}
  th.chg, td.chg {{ width: 12%; text-align: right;
                    font-variant-numeric: tabular-nums; }}
</style>
</head>
<body>
  <h1>Manufacturing run sheet</h1>
  <div class="meta">{meta_html}</div>
  {('<div class="legend">' + legend + "</div>") if legend else ""}
  <table>
    <thead>
      <tr>
        <th class="pos">#</th>
        <th class="lot">Navision lot #</th>
        <th class="panels">Panel #s</th>
        <th class="len">Length (LF)</th>
        <th class="bar">Layout</th>
        <th class="chg">Setup change</th>
      </tr>
    </thead>
    <tbody>
      {"".join(rows)}
    </tbody>
  </table>
</body>
</html>"""


def build_run_sheet_pdf(filename, report):
    """Render the run sheet to PDF bytes. WeasyPrint is imported lazily so this
    module (and the rest of the app) stays importable where WeasyPrint's native
    libraries are not installed; the caller degrades gracefully in that case."""
    from weasyprint import HTML

    document = build_run_sheet_html(filename, report)
    return HTML(string=document).write_pdf()


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
def _render_report(st, filename, extraction, report):
    quality = report["solution_quality"]
    conservation = report["conservation"]
    breakdown = report["transition_breakdown"]

    st.subheader(filename)

    # Each section sits in its own bordered card so the boundaries between them
    # are visible at a glance (Step 1).

    # Headline numbers.
    summary = st.container(border=True)
    summary.markdown("#### Summary")
    top = summary.columns(4)
    top[0].metric("Rolls", report["roll_count"])
    top[1].metric("Distinct layouts", report["distinct_layout_count"])
    top[2].metric("Achieved setup cost", f"{report['achieved_cost_in']} in")
    method_note = "proven optimum" if report["optimal"] else "near-optimal"
    top[3].metric("Method", method_note,
                  help=report["method"])

    # Conservation — the sequence must be a faithful reordering of the order.
    cons = st.container(border=True)
    cons.markdown("#### Conservation")
    if conservation["passed"]:
        cons.success("Conservation check passed: every roll, quantity and total "
                     "is preserved; only the manufacturing order changed.")
    else:
        cons.error("Conservation check failed — the sequence is not a faithful "
                   "reordering of the order:")
        for discrepancy in conservation["discrepancies"]:
            cons.write(f"- {discrepancy}")

    # Distinct layouts (Step 2) — sits just above Solution quality.
    _render_distinct_layouts(st, report["layouts"])

    # Solution quality.
    qbox = st.container(border=True)
    qbox.markdown("#### Solution quality")
    quality_cols = qbox.columns(3)
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

    qbox.caption(
        f"As-extracted order cost, for reference only (assigned by sales, not a "
        f"target): {report['reference_only_as_extracted_cost_in']} in.")

    # Manufacturing sequence.
    seqbox = st.container(border=True)
    seqbox.markdown("#### Manufacturing sequence")
    seqbox.dataframe(report["manufacturing_sequence"], hide_index=True)

    # Transition breakdown.
    tbox = st.container(border=True)
    tbox.markdown("#### Transition breakdown")
    breakdown_cols = tbox.columns(4)
    breakdown_cols[0].metric("Transitions", breakdown["transition_count"])
    breakdown_cols[1].metric("Zero-cost (identical)",
                             breakdown["zero_cost_transitions"])
    breakdown_cols[2].metric("Max change", f"{breakdown['max_transition_cost']} in")
    breakdown_cols[3].metric("Mean change", f"{breakdown['mean_transition_cost']} in")
    if breakdown["transition_costs"]:
        tbox.caption("Per-transition setup change cost (inches), in "
                     "manufacturing order:")
        tbox.bar_chart(breakdown["transition_costs"])

    # Full manufacturing order (Step 3) — the combined run, at the bottom.
    _render_combined_order(st, report["manufacturing_sequence"])

    # Warnings and downloads.
    footer = st.container(border=True)
    for warning in report["warnings"]:
        footer.warning(warning)

    stem = Path(filename).stem
    downloads = footer.columns(2)
    downloads[0].download_button(
        "Download sequence report (JSON)",
        data=report_json(report),
        file_name=f"{stem}.sequence.json",
        mime="application/json",
    )

    # The printable run sheet needs WeasyPrint; where it (or its native
    # libraries) is unavailable, fall back to a note rather than erroring so
    # the rest of the report still works.
    try:
        pdf_bytes = build_run_sheet_pdf(filename, report)
    except Exception as exc:  # noqa: BLE001
        downloads[1].caption(
            "Run sheet PDF needs WeasyPrint — install `weasyprint` to enable "
            f"it ({exc}).")
    else:
        downloads[1].download_button(
            "Download run sheet (PDF)",
            data=pdf_bytes,
            file_name=f"{stem}.run-sheet.pdf",
            mime="application/pdf",
        )


def main():
    import streamlit as st

    st.set_page_config(page_title="Roll Sequencing", layout="wide")

    # TenCate branding, pinned to the top-left of the app. `st.logo` (Streamlit
    # >= 1.35) places it in the top-left app chrome; on older versions there is
    # no such API, so the wordmark is rendered at the top-left of the page.
    logo_path = Path(__file__).resolve().parent / "assets" / "tencate_logo.png"
    if logo_path.exists():
        if hasattr(st, "logo"):
            st.logo(str(logo_path))
        else:
            st.columns([1, 4])[0].image(str(logo_path), width=160)

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
