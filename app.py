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
layout colour bars drawn with fpdf2 — pure Python, no system libraries needed).

There is no logic duplicated here and nothing to deploy — it runs locally:

    streamlit run app.py

The extraction/optimisation pipeline is factored into `analyse_upload`, and the
run sheet into `_run_sheet_rows` / `build_run_sheet_pdf`; none of these import
Streamlit, so they can be exercised without a browser. Streamlit itself is
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
    split_report,
)
from extract_turf_layout import TemplateMismatch, extract_workbook
from roll_sequencing import (
    _clean_number,
    join_orders,
    parse_signature,
    profile_cost,
)
from sequencer import split_guidance


# --------------------------------------------------------------------------
# Pipeline (no Streamlit) — reused by the UI and testable on its own
# --------------------------------------------------------------------------
def _extract_upload(filename, file_bytes):
    """Extract one uploaded workbook's bytes into an extraction dict.

    The extractor reads the workbook as a file path (it opens the underlying
    zip to resolve the embedded brand logo), so the bytes are written to a
    temporary file first. The temp file is closed before the extractor opens
    it and removed afterwards: on Windows a still-open `NamedTemporaryFile`
    cannot be reopened by another handle, which otherwise raises
    "Permission denied". The returned dict's `source_file` is the original
    upload name rather than the temp file's. Raises `TemplateMismatch` if the
    workbook is not a recognised FIELD LAYOUT order."""
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
    return extraction


def analyse_upload(filename, file_bytes,
                   exact_max_layouts=DEFAULT_EXACT_MAX_LAYOUTS,
                   oracle_max_layouts=DEFAULT_EXACT_ORACLE_MAX_LAYOUTS):
    """Run the full pipeline on one uploaded workbook's bytes.

    Extracts via `_extract_upload` (see there for the temp-file rationale),
    then evaluates. Returns `(extraction, report)`: the raw extraction dict
    from `extract_turf_layout` and the Phase 4 evaluation report from
    `evaluate`. Raises `TemplateMismatch` if the workbook is not a recognised
    FIELD LAYOUT order."""
    extraction = _extract_upload(filename, file_bytes)
    report = evaluate(
        extraction.get("rolls", []),
        exact_max_layouts=exact_max_layouts,
        oracle_max_layouts=oracle_max_layouts,
        extraction=extraction,
    )
    return extraction, report


def evaluate_combined(extractions,
                      exact_max_layouts=DEFAULT_EXACT_MAX_LAYOUTS,
                      oracle_max_layouts=DEFAULT_EXACT_ORACLE_MAX_LAYOUTS):
    """Join several extracted orders into one and evaluate them together.

    `join_orders` concatenates the rolls (each tagged with its source file,
    so the combined sequence stays traceable to its workbook) and sums the
    stated MFG totals, so `evaluate`'s cross-check compares the combined
    sequence against the combined stated totals rather than any single
    file's. Returns `(combined, report)`: the joined extraction-shaped dict
    and the evaluation report over all rolls at once."""
    combined = join_orders(extractions)
    report = evaluate(
        combined["rolls"],
        exact_max_layouts=exact_max_layouts,
        oracle_max_layouts=oracle_max_layouts,
        extraction=combined,
    )
    return combined, report


def analyse_uploads_combined(named_files,
                             exact_max_layouts=DEFAULT_EXACT_MAX_LAYOUTS,
                             oracle_max_layouts=DEFAULT_EXACT_ORACLE_MAX_LAYOUTS):
    """Run the combined pipeline on several uploaded workbooks' bytes.

    `named_files` is a list of `(filename, file_bytes)` pairs. Combined mode
    joins all the uploads into one order and sequences them together, so
    layouts shared between files are produced back to back instead of being
    set up once per file. Returns `(combined, report)` as `evaluate_combined`
    does. Raises `TemplateMismatch` if any workbook is not a recognised
    FIELD LAYOUT order."""
    extractions = [_extract_upload(filename, file_bytes)
                   for filename, file_bytes in named_files]
    return evaluate_combined(extractions,
                             exact_max_layouts=exact_max_layouts,
                             oracle_max_layouts=oracle_max_layouts)


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
# Printable run sheet (PDF) — testable without Streamlit
# --------------------------------------------------------------------------
# The manufacturing floor wants a paper run sheet, not JSON. It lists the rolls
# in manufacturing order — one row per physical roll (roll_qty expanded, exactly
# as the "Full manufacturing order" view does) — with a colour-bar visual of
# each layout so an operator can eyeball the threading. The bars are drawn from
# the same colour mapping (`_color_for_code`) as the on-screen `_bar_html`, only
# to the PDF page instead of to HTML.
#
# It is rendered with fpdf2, which is pure Python: a plain `pip install`, no
# system libraries or browser to install. `_run_sheet_rows` builds the row data
# with no Streamlit and no PDF library, so the content is testable on its own,
# and `build_run_sheet_pdf` returns bytes that can be asserted on directly.
def _hex_to_rgb(hex_color):
    """A #rrggbb colour as an (r, g, b) tuple of 0-255 ints, for fpdf."""
    h = hex_color.lstrip("#")
    return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)


def _run_sheet_rows(report):
    """The run sheet's body rows as plain data, in manufacturing order and one
    row per physical roll: each sequence entry's `roll_qty` is expanded so a
    two-roll entry becomes two rows, matching the "Full manufacturing order"
    view. Each row carries the position, Navision lot number, panel numbers,
    length (LF), the parsed layout profile, and the setup change cost incurred
    to switch to it (a human string; "start" for the first roll).

    No Streamlit and no PDF library — the shared, testable core of the run
    sheet."""
    rows = []
    position = 0
    prev_profile = None
    for entry in report.get("manufacturing_sequence", []):
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
            rows.append({
                "position": position,
                "navision_lot": lot,
                "panel_numbers": panels,
                "length_lf": length,
                "profile": profile,
                "change": change,
            })
            prev_profile = profile
    return rows


def _latin1(value):
    """Text safe for fpdf's built-in (latin-1) core fonts: unencodable
    characters are replaced rather than raising."""
    return str(value).encode("latin-1", "replace").decode("latin-1")


def _draw_run_sheet_bar(pdf, x, y, width, height, profile):
    """Draw a roll's threading profile as a horizontal segmented colour bar at
    (x, y), `width` x `height` mm — the PDF twin of `_bar_html`. Segment widths
    are proportional to inches; codes use the same colours as on screen, with a
    short label centred on any segment wide enough to hold it."""
    total = sum(seg_w for _, seg_w in profile)
    if total <= 0:
        pdf.set_draw_color(170, 170, 170)
        pdf.rect(x, y, width, height, style="D")
        return

    cursor = x
    last = len(profile) - 1
    for i, (code, seg_w) in enumerate(profile):
        # Close the bar exactly on the last segment so rounding leaves no sliver.
        seg = (x + width - cursor) if i == last else width * seg_w / total
        r, g, b = _hex_to_rgb(_color_for_code(code))
        pdf.set_fill_color(r, g, b)
        pdf.set_draw_color(140, 140, 140)  # thin separators between segments
        pdf.rect(cursor, y, seg, height, style="DF")

        label = _latin1(code)
        pdf.set_font("Helvetica", "B", 6.5)
        if seg >= pdf.get_string_width(label) + 1.5:
            tr, tg, tb = _hex_to_rgb(_text_on(_color_for_code(code)))
            pdf.set_text_color(tr, tg, tb)
            pdf.text(cursor + seg / 2 - pdf.get_string_width(label) / 2,
                     y + height / 2 + 1.1, label)
        cursor += seg


def build_run_sheet_pdf(filename, report):
    """Render the run sheet to PDF bytes with fpdf2 (pure Python; no system
    libraries or browser required). Imported lazily so the module stays
    importable — and the rest of the app keeps working — where fpdf2 is not
    installed; the caller degrades gracefully in that case.

    Rolls are listed in manufacturing order, one row per physical roll, with the
    position, Navision lot number, panel numbers, length (LF), a colour bar of
    the layout, and the per-step setup change cost. A header carries the source
    file, total setup cost, and the roll/layout counts, and a section at the
    bottom gives the exact threading width of each distinct layout."""
    from fpdf import FPDF

    rows = _run_sheet_rows(report)
    source = str(report.get("source_file") or filename)
    total_cost = report.get("achieved_cost_in")
    roll_count = report.get("roll_count")
    layout_count = report.get("distinct_layout_count")

    pdf = FPDF(orientation="L", unit="mm", format="A4")
    pdf.set_margins(14, 12, 14)
    pdf.set_auto_page_break(False)
    pdf.add_page()

    # Column widths (mm), summing to the effective page width.
    epw = pdf.epw
    w_pos, w_lot, w_panels, w_len, w_chg = 12, 40, 34, 24, 28
    w_bar = epw - (w_pos + w_lot + w_panels + w_len + w_chg)
    columns = [("#", w_pos, "R"), ("Navision lot #", w_lot, "L"),
               ("Panel #s", w_panels, "L"), ("Length (LF)", w_len, "R"),
               ("Layout", w_bar, "L"), ("Setup change", w_chg, "R")]
    row_h, bar_h = 8.5, 5.0

    def draw_header():
        pdf.set_xy(pdf.l_margin, pdf.t_margin)
        pdf.set_text_color(20, 20, 20)
        pdf.set_font("Helvetica", "B", 15)
        pdf.cell(0, 8, _latin1("Manufacturing run sheet"),
                 new_x="LMARGIN", new_y="NEXT")

        parts = [f"Source file: {source}"]
        if total_cost is not None:
            parts.append(f"Total setup cost: {total_cost} in")
        if roll_count is not None:
            parts.append(f"Rolls: {roll_count}")
        if layout_count is not None:
            parts.append(f"Distinct layouts: {layout_count}")
        pdf.set_font("Helvetica", "", 9)
        pdf.set_text_color(90, 90, 90)
        pdf.cell(0, 6, _latin1("      ".join(parts)),
                 new_x="LMARGIN", new_y="NEXT")

        _draw_legend(pdf, rows)

    def draw_table_header():
        pdf.set_font("Helvetica", "B", 8)
        pdf.set_fill_color(240, 240, 240)
        pdf.set_draw_color(180, 180, 180)
        pdf.set_text_color(40, 40, 40)
        pdf.set_x(pdf.l_margin)
        for title, width, align in columns:
            pdf.cell(width, 7, _latin1(title), border=1, align=align, fill=True)
        pdf.ln(7)

    draw_header()
    draw_table_header()

    for row in rows:
        if pdf.get_y() + row_h > pdf.h - pdf.b_margin:
            pdf.add_page()
            draw_table_header()

        y0 = pdf.get_y()
        pdf.set_x(pdf.l_margin)
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(20, 20, 20)
        pdf.set_draw_color(200, 200, 200)

        length = row["length_lf"]
        length_txt = (f"{_clean_number(length)}"
                      if isinstance(length, (int, float))
                      and not isinstance(length, bool) else "")
        pdf.cell(w_pos, row_h, _latin1(row["position"]), border=1, align="R")
        pdf.cell(w_lot, row_h, _latin1(row["navision_lot"] or ""),
                 border=1, align="L")
        panels = row["panel_numbers"]
        pdf.cell(w_panels, row_h,
                 _latin1(panels) if panels not in (None, "") else "",
                 border=1, align="L")
        pdf.cell(w_len, row_h, _latin1(length_txt), border=1, align="R")

        # The layout cell: a bordered box with the colour bar drawn inside it.
        bar_x = pdf.get_x()
        pdf.cell(w_bar, row_h, "", border=1)
        _draw_run_sheet_bar(pdf, bar_x + 1.5, y0 + (row_h - bar_h) / 2,
                            w_bar - 3, bar_h, row["profile"])

        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(20, 20, 20)
        pdf.set_draw_color(200, 200, 200)
        pdf.set_xy(bar_x + w_bar, y0)
        pdf.cell(w_chg, row_h, _latin1(row["change"]), border=1, align="R")
        pdf.ln(row_h)

    # Exact per-layout threading widths, at the bottom.
    _draw_layout_breakdowns(pdf, report)

    return bytes(pdf.output())


def _draw_legend(pdf, rows):
    """A compact colour legend under the header: a swatch and code for each
    colour used, in first-appearance order, wrapping within the page width."""
    codes = _ordered_codes(
        _profile_signature(row["profile"]) for row in rows)
    if not codes:
        pdf.ln(2)
        return

    pdf.ln(1)
    pdf.set_font("Helvetica", "", 8)
    swatch = 3.2
    x = pdf.l_margin
    y = pdf.get_y()
    for code in codes:
        label = _latin1(code)
        label_w = pdf.get_string_width(label) + 4
        if x + swatch + 1 + label_w > pdf.w - pdf.r_margin:
            x = pdf.l_margin
            y += 5.5
        r, g, b = _hex_to_rgb(_color_for_code(code))
        pdf.set_fill_color(r, g, b)
        pdf.set_draw_color(150, 150, 150)
        pdf.rect(x, y, swatch, swatch, style="DF")
        pdf.set_text_color(40, 40, 40)
        pdf.set_xy(x + swatch + 1, y - 0.7)
        pdf.cell(label_w, swatch + 1.2, label)
        x += swatch + 1 + label_w
    pdf.set_y(y + 6)


def _profile_signature(profile):
    """Rebuild a signature string from a parsed profile so `_ordered_codes`
    (which parses signatures) can be reused on already-parsed rows."""
    return "|".join(f"{width}{code}" for code, width in profile)


def _distinct_layouts(report):
    """The distinct layouts in the order, in first-appearance (manufacturing)
    order. Each entry carries the parsed profile, how many physical rolls use
    it (roll_qty summed), and the Navision lots that use it — the data behind
    the exact per-layout threading breakdown."""
    order = []
    seen = {}
    for entry in report.get("manufacturing_sequence", []):
        profile = _profile_of(entry.get("layout_signature"))
        key = _profile_signature(profile)
        info = seen.get(key)
        if info is None:
            info = {"profile": profile, "roll_count": 0, "lots": []}
            seen[key] = info
            order.append(key)
        info["roll_count"] += _reps_of(entry.get("roll_qty"))
        lot = entry.get("navision_lot")
        if lot is not None and lot not in info["lots"]:
            info["lots"].append(lot)
    return [seen[key] for key in order]


def _draw_layout_breakdowns(pdf, report):
    """Draw the exact threading breakdown of each distinct layout at the bottom
    of the run sheet: the colour bar plus its segment widths spelled out (e.g.
    "177 in FG    5 in WHI    = 182 in total"), so the floor has the precise
    tufting spec and not only the visual bar. One block per distinct layout, in
    manufacturing order, with the lots that use it."""
    layouts = _distinct_layouts(report)
    if not layouts:
        return

    bar_w = min(pdf.epw, 150.0)
    bar_h = 6.0
    max_lots = 20  # keep the lots line bounded on very large orders

    def section_heading():
        pdf.set_x(pdf.l_margin)
        pdf.set_font("Helvetica", "B", 11)
        pdf.set_text_color(20, 20, 20)
        pdf.cell(0, 7, _latin1("Layout threading breakdown"),
                 new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 8)
        pdf.set_text_color(90, 90, 90)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 4.5, _latin1(
            "Exact threading width of each distinct layout, measured front to "
            "back across the roll width."))
        pdf.ln(1)

    # Start the section on the current page if it fits, else a fresh page.
    pdf.ln(5)
    if pdf.get_y() + 34 > pdf.h - pdf.b_margin:
        pdf.add_page()
    section_heading()

    for index, layout in enumerate(layouts, start=1):
        profile = layout["profile"]
        total = sum(width for _, width in profile)
        breakdown = "      ".join(
            f"{_clean_number(width)} in {code}" for code, width in profile)
        breakdown = f"{breakdown}      =  {_clean_number(total)} in total"
        rolls = layout["roll_count"]
        roll_txt = "1 roll" if rolls == 1 else f"{rolls} rolls"
        lots = [str(lot) for lot in layout["lots"]]
        if len(lots) > max_lots:
            lots_txt = ("Lots: " + ", ".join(lots[:max_lots])
                        + f"  (+{len(lots) - max_lots} more)")
        elif lots:
            lots_txt = "Lots: " + ", ".join(lots)
        else:
            lots_txt = ""

        # Keep a whole block together: heading + bar + breakdown + lots.
        block_h = 6 + bar_h + 6 + (5 if lots_txt else 0) + 3
        if pdf.get_y() + block_h > pdf.h - pdf.b_margin:
            pdf.add_page()
            section_heading()

        pdf.set_x(pdf.l_margin)
        pdf.set_font("Helvetica", "B", 9)
        pdf.set_text_color(20, 20, 20)
        pdf.cell(0, 5.5, _latin1(f"Layout {index}   -   {roll_txt}"),
                 new_x="LMARGIN", new_y="NEXT")

        y_bar = pdf.get_y()
        _draw_run_sheet_bar(pdf, pdf.l_margin, y_bar, bar_w, bar_h, profile)
        pdf.set_y(y_bar + bar_h + 1.2)

        pdf.set_x(pdf.l_margin)
        pdf.set_font("Helvetica", "", 8.5)
        pdf.set_text_color(20, 20, 20)
        pdf.multi_cell(0, 5, _latin1(breakdown))

        if lots_txt:
            pdf.set_x(pdf.l_margin)
            pdf.set_font("Helvetica", "", 7.5)
            pdf.set_text_color(110, 110, 110)
            pdf.multi_cell(0, 4, _latin1(lots_txt))
        pdf.ln(2.5)


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
def _render_report(st, filename, extraction, report, download_stem=None):
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

    stem = download_stem or Path(filename).stem
    downloads = footer.columns(2)
    downloads[0].download_button(
        "Download sequence report (JSON)",
        data=report_json(report),
        file_name=f"{stem}.sequence.json",
        mime="application/json",
    )

    # The printable run sheet needs fpdf2; where it is unavailable, fall back to
    # a note rather than erroring so the rest of the report still works.
    try:
        pdf_bytes = build_run_sheet_pdf(filename, report)
    except Exception as exc:  # noqa: BLE001
        downloads[1].caption(
            "Run sheet PDF needs fpdf2 — run `pip install fpdf2` to enable it "
            f"({exc}).")
    else:
        downloads[1].download_button(
            "Download run sheet (PDF)",
            data=pdf_bytes,
            file_name=f"{stem}.run-sheet.pdf",
            mime="application/pdf",
        )


def _render_split_section(st, report):
    """Cut the evaluated sequence into separate manufacturing schedules at its
    most expensive changeovers. Each schedule restarts from a fresh machine
    state, so the total setup cost drops by exactly the changeovers removed —
    the best way to cut this fixed ordering into k runs, though not a re-solve
    of the whole assignment (see `sequencer.choose_cuts`). The elbow view from
    `split_guidance` shows what each extra schedule would save, so the choice
    of k is informed rather than guessed."""
    box = st.container(border=True)
    box.markdown("#### Split into schedules")

    costs = [e["change_cost_in"] for e in report["manufacturing_sequence"][1:]]
    if not costs:
        box.caption("The sequence has no transitions — there is nothing to "
                    "split.")
        return

    box.caption(
        "The combined sequence can be cut into separate manufacturing "
        "schedules at its most expensive changeovers. Each schedule restarts "
        "from a fresh machine state, so the total setup cost drops by exactly "
        "the changeovers removed. This is the best way to cut this fixed "
        "ordering into k runs — it is not a re-solve of the whole assignment "
        "across schedules.")

    # Elbow view: what each extra schedule saves, so k is chosen on evidence.
    guidance = split_guidance(costs)
    box.dataframe(
        [{"k": row["k"],
          "total cost (in)": row["total_cost_in"],
          "saving from next cut (in)": row["marginal_saving_in"]}
         for row in guidance],
        hide_index=True)
    box.caption("Total setup cost against the number of schedules (k) — the "
                "elbow shows where one more schedule stops paying its way:")
    box.line_chart(guidance, x="k", y="total_cost_in")

    method = box.radio(
        "Split method",
        ["No split", "Number of schedules (k)",
         "Changeover threshold (inches)"],
        horizontal=True)

    split = None
    if method == "Number of schedules (k)":
        k = box.number_input("Number of schedules (k)", min_value=1,
                             max_value=len(costs) + 1, value=1)
        split = split_report(report, k=int(k))
    elif method == "Changeover threshold (inches)":
        threshold = box.number_input(
            "Split where a changeover exceeds (inches)",
            min_value=0.0, value=float(max(costs)))
        split = split_report(report, threshold=float(threshold))

    if split is None:
        return
    if split["schedule_count"] < 2:
        box.caption("The chosen split leaves a single schedule (the threshold "
                    "sits at or above every changeover, or k is 1) — nothing "
                    "was cut.")
        return

    summary = box.columns(3)
    summary[0].metric("Cost before", f"{split['cost_before_in']} in")
    summary[1].metric("Cost after", f"{split['cost_after_in']} in")
    summary[2].metric("Total saving", f"{split['total_saving_in']} in")

    count = split["schedule_count"]
    for schedule in split["schedules"]:
        index = schedule["schedule_index"]
        sbox = box.container(border=True)
        sbox.markdown(f"#### Schedule {index} of {count}")
        metrics = sbox.columns(3)
        metrics[0].metric("Roll rows", schedule["roll_count"])
        metrics[1].metric("Distinct layouts",
                          schedule["distinct_layout_count"])
        metrics[2].metric("Setup cost", f"{schedule['achieved_cost_in']} in")
        sbox.dataframe(schedule["manufacturing_sequence"], hide_index=True)

        # The per-schedule run sheet needs fpdf2; where it is unavailable,
        # fall back to a note rather than erroring, as `_render_report` does.
        try:
            pdf_bytes = build_run_sheet_pdf(schedule["source_file"], schedule)
        except Exception as exc:  # noqa: BLE001
            sbox.caption(
                "Run sheet PDF needs fpdf2 — run `pip install fpdf2` to "
                f"enable it ({exc}).")
        else:
            sbox.download_button(
                "Download run sheet (PDF)",
                data=pdf_bytes,
                file_name=f"combined.schedule-{index}-of-{count}.run-sheet.pdf",
                mime="application/pdf",
                key=f"split-run-sheet-{index}-of-{count}",
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
        st.caption(
            "Both settings trade certainty for speed. The defaults suit "
            "almost every order — most of the time there is no need to "
            "change them.")
        exact_max_layouts = st.number_input(
            "Solve exactly up to N distinct layouts", min_value=1, max_value=22,
            value=DEFAULT_EXACT_MAX_LAYOUTS,
            help="Orders with this many distinct layouts or fewer get the "
                 "proven best sequence. Larger orders use a fast method that "
                 "comes very close but is not guaranteed to be the best.")
        oracle_max_layouts = st.number_input(
            "Report exact gap up to N layouts", min_value=1, max_value=22,
            value=DEFAULT_EXACT_ORACLE_MAX_LAYOUTS,
            help="Only matters when the fast method was used: orders up to "
                 "this size are also solved exactly in the background, purely "
                 "to show how close the fast answer came. The sequence itself "
                 "does not change.")
        with st.expander("When to change these"):
            st.markdown(
                "**Raise the first setting** when a report says "
                "*near-optimal* instead of *proven optimum* and the order has "
                "16–20 distinct layouts (common when combining files). Set it "
                "to cover the order's layout count to get the proven best "
                "sequence — expect a slower run near 20.\n\n"
                "**Lower both** (to around 12) if a run feels too slow. The "
                "fast answer is usually within a few inches of the best, and "
                "the *gap* figure in the report shows exactly how much was "
                "given up.\n\n"
                "Above 22 distinct layouts the exact method runs out of "
                "memory, so the fast method is always used and these settings "
                "have no effect.")

    uploads = st.file_uploader(
        "Order workbook(s)", type=["xlsx"], accept_multiple_files=True)

    if not uploads:
        st.info("Upload one or more `.xlsx` order workbooks to begin.")
        return

    mode = "Separate schedules"
    if len(uploads) >= 2:
        mode = st.radio(
            "Multiple workbooks",
            ["Separate schedules", "Combined"],
            horizontal=True,
            help="Separate schedules sequences each workbook on its own. "
                 "Combined joins every upload into one order and sequences "
                 "them together, so layouts shared between files are produced "
                 "back to back; the combined run can then be split back into "
                 "schedules at its most expensive changeovers.")

    if mode == "Combined":
        # Every workbook must extract cleanly before they can be joined; the
        # per-file error UX matches the separate-schedules loop below.
        extractions = []
        any_failed = False
        for upload in uploads:
            try:
                extractions.append(
                    _extract_upload(upload.name, upload.getvalue()))
            except TemplateMismatch as exc:
                st.error(f"{upload.name}: not a recognised FIELD LAYOUT "
                         f"workbook ({exc}).")
                any_failed = True
            except Exception as exc:  # noqa: BLE001
                st.error(f"{upload.name}: could not process ({exc}).")
                any_failed = True
        if any_failed:
            st.warning("Combined mode needs every workbook to extract "
                       "cleanly — fix or remove the files above, or switch "
                       "to separate schedules.")
            return

        try:
            combined, report = evaluate_combined(
                extractions,
                exact_max_layouts=int(exact_max_layouts),
                oracle_max_layouts=int(oracle_max_layouts))
        except Exception as exc:  # noqa: BLE001
            st.error(f"Could not sequence the combined order ({exc}).")
            return

        _render_report(st, combined["source_file"], combined, report,
                       download_stem="combined")
        _render_split_section(st, report)
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
