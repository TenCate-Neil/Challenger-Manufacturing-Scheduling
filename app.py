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
)
from extract_turf_layout import TemplateMismatch, extract_workbook
from item_requirements import BOBBINS_PER_INCH
from roll_sequencing import (
    _clean_number,
    join_orders,
    parse_signature,
    profile_cost,
)


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


def _item_requirement_rows(items):
    """The Item batch requirements table's body cells as display strings, one
    dict per item (report `item_requirements` entries): pounds with one
    decimal and a thousands separator, the max width with any trailing .0
    trimmed, and the colour shown by name where the workbook gives one that
    differs from the code. SKUs pass through as-is — "145190A" stays a
    string. No Streamlit — the testable core of
    `_render_item_requirements`."""
    rows = []
    for item in items:
        code = item.get("color_code")
        name = item.get("color_name")
        colour = name if name not in (None, "") and str(name) != str(code) \
            else code
        lbs = item.get("lbs_needed")
        lbs_text = (f"{lbs:,.1f}"
                    if isinstance(lbs, (int, float))
                    and not isinstance(lbs, bool) else "-")
        width = item.get("max_width_in")
        width_text = (f"{_clean_number(width)}"
                      if isinstance(width, (int, float))
                      and not isinstance(width, bool) else "-")
        rows.append({
            "item_number": str(item.get("item_number")),
            "yarn_type": str(item.get("yarn_type")),
            "colour": str(colour),
            "lbs_needed": lbs_text,
            "max_width_in": width_text,
            "bobbins_required": str(item.get("bobbins_required")),
        })
    return rows


def _render_item_requirements(st, extraction, report):
    """Per-item batch requirements: one row per yarn type + colour item, with
    the pounds and bobbins a single inventory batch must cover. Rendered only
    when the report carries the `item_requirements` key (the extraction
    stated a yarn lbs block); when the key is absent this renders nothing at
    all, so older extractions keep their existing report."""
    if "item_requirements" not in report:
        return
    box = st.container(border=True)
    box.markdown("#### Item batch requirements")

    items = report["item_requirements"]
    if not items:
        box.caption("No item requirements were found in the workbook's "
                    "yarn lbs block.")
        return

    def cell(text):
        # A literal "|" in a value would split the markdown table cell.
        return str(text).replace("|", "\\|")

    lines = [
        "| Item # | Yarn type | Colour | Max width (in) | Lbs needed "
        "| Bobbins needed |",
        "| --- | --- | --- | ---: | ---: | ---: |",
    ]
    for row in _item_requirement_rows(items):
        lines.append(
            f"| {cell(row['item_number'])} | {cell(row['yarn_type'])} "
            f"| {cell(row['colour'])} | {row['max_width_in']} "
            f"| {row['lbs_needed']} | {row['bobbins_required']} |")
    box.markdown("\n".join(lines))

    box.caption(
        "Each item (yarn type + colour = item number) must be covered by a "
        "single inventory batch holding at least these pounds and bobbins; "
        "bobbins = the max total width of the item's colour in any one roll "
        f"× {BOBBINS_PER_INCH} per inch. Roll length drives the pounds, "
        "not the bobbins.")
    if isinstance(extraction, dict) and "source_files" in extraction:
        box.caption(
            "Combined order: the pounds are summed across the input files, "
            "and the max width is taken across all rolls of the combined "
            "run.")


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
    view. Each row carries the position, purchase order number, Navision lot
    number, panel numbers, length (LF), the parsed layout profile, and the
    setup change cost incurred to switch to it (a human string; "start" for
    the first roll). The PO is per row because a combined order mixes files
    with different POs; reports without one carry None, printed as blank.

    No Streamlit and no PDF library — the shared, testable core of the run
    sheet."""
    rows = []
    position = 0
    prev_profile = None
    for entry in report.get("manufacturing_sequence", []):
        profile = _profile_of(entry.get("layout_signature"))
        lot = entry.get("navision_lot")
        po = entry.get("purchase_order_number")
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
                "purchase_order_number": po,
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


def _draw_run_sheet_bar(pdf, x, y, width, height, profile, label_size=6.5):
    """Draw a roll's threading profile as a horizontal segmented colour bar at
    (x, y), `width` x `height` mm — the PDF twin of `_bar_html`. Segment widths
    are proportional to inches; codes use the same colours as on screen, with a
    short label centred on any segment wide enough to hold it. `label_size` is
    the label font size in points — the tall bars of the per-roll threading
    breakdown pass a much larger size than the table's compact bars."""
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
        pdf.set_font("Helvetica", "B", label_size)
        if seg >= pdf.get_string_width(label) + 1.5:
            tr, tg, tb = _hex_to_rgb(_text_on(_color_for_code(code)))
            pdf.set_text_color(tr, tg, tb)
            # Baseline sits just below the vertical centre; the offset scales
            # with the font size (0.17 mm/pt centres both 6.5 pt and 13 pt).
            pdf.text(cursor + seg / 2 - pdf.get_string_width(label) / 2,
                     y + height / 2 + label_size * 0.17, label)
        cursor += seg


def build_run_sheet_pdf(filename, report):
    """Render the run sheet to PDF bytes with fpdf2 (pure Python; no system
    libraries or browser required). Imported lazily so the module stays
    importable — and the rest of the app keeps working — where fpdf2 is not
    installed; the caller degrades gracefully in that case.

    Rolls are listed in manufacturing order, one row per physical roll, with the
    position, purchase order number (per roll, since a combined order mixes
    files with different POs), Navision lot number, panel numbers, length (LF),
    a colour bar of the layout, and the per-step setup change cost. A header
    carries the source file, total setup cost, and the roll/layout counts, and a
    large-print section at the bottom gives the exact threading width of every
    physical roll in manufacturing order, with a red SETUP CHANGE band wherever
    the layout changes."""
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

    # Column widths (mm), summing to the effective page width. The layout bar
    # takes whatever the fixed columns leave over.
    epw = pdf.epw
    w_pos, w_po, w_lot, w_panels, w_len, w_chg = 12, 26, 34, 30, 24, 28
    w_bar = epw - (w_pos + w_po + w_lot + w_panels + w_len + w_chg)
    columns = [("#", w_pos, "R"), ("PO #", w_po, "L"),
               ("Navision lot #", w_lot, "L"),
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
        po = row["purchase_order_number"]
        pdf.cell(w_po, row_h,
                 _latin1(po) if po not in (None, "") else "",
                 border=1, align="L")
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

    # Large-print per-roll threading widths, at the bottom.
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


def _draw_layout_breakdowns(pdf, report):
    """Draw the threading breakdown of every physical roll at the bottom of
    the run sheet, in manufacturing order — the same one-row-per-roll
    expansion as the main table (`_run_sheet_rows`). Each entry is sized for
    a non-technical floor worker reading at arm's length: a tall colour bar
    of the roll's threading with large segment labels, its segment widths
    spelled out below (e.g. "177 in FG    5 in WHI" — no total suffix), and
    the roll's lot number and length in LF in the entry heading. Wherever the
    layout changes from the previous roll (a setup/creel change — the row's
    change cost is > 0, printed as "+N in"), a prominent red "SETUP CHANGE"
    band separates the two entries. Page breaks keep each entry — and any
    marker with the entry after it — whole."""
    rows = _run_sheet_rows(report)
    if not rows:
        return

    # Sizes chosen for legibility from a distance: the bar is over three times
    # the table's bar height, and every font is 14 pt or larger.
    bar_w = pdf.epw
    bar_h = 16.0        # the table's bars are 5 mm
    heading_h = 8.0     # 14 pt bold roll heading
    widths_h = 9.0      # 14 pt bold segment-widths line
    entry_gap = 6.0     # breathing room after each entry
    marker_h = 14.0     # the red SETUP CHANGE band
    entry_h = heading_h + bar_h + 2.0 + widths_h + entry_gap
    red = (200, 0, 0)

    def section_heading():
        pdf.set_x(pdf.l_margin)
        pdf.set_font("Helvetica", "B", 16)
        pdf.set_text_color(20, 20, 20)
        pdf.cell(0, 10, _latin1("Layout threading breakdown"),
                 new_x="LMARGIN", new_y="NEXT")
        pdf.set_font("Helvetica", "", 11)
        pdf.set_text_color(90, 90, 90)
        pdf.set_x(pdf.l_margin)
        pdf.multi_cell(0, 6, _latin1(
            "Every roll in manufacturing order. Each bar is the roll's "
            "threading, front to back across the roll width."))
        pdf.ln(2)

    def draw_setup_change_marker():
        """A full-width red band: rules either side of "SETUP CHANGE"."""
        y_mid = pdf.get_y() + marker_h / 2
        pdf.set_font("Helvetica", "B", 18)
        pdf.set_text_color(*red)
        pdf.set_draw_color(*red)
        pdf.set_line_width(0.8)
        label = _latin1("SETUP CHANGE")
        label_w = pdf.get_string_width(label)
        centre = pdf.l_margin + pdf.epw / 2
        pdf.line(pdf.l_margin, y_mid, centre - label_w / 2 - 5, y_mid)
        pdf.line(centre + label_w / 2 + 5, y_mid, pdf.l_margin + pdf.epw, y_mid)
        pdf.text(centre - label_w / 2, y_mid + 2.2, label)
        pdf.set_line_width(0.2)
        pdf.set_y(y_mid + marker_h / 2)

    # Start the section on the current page if it fits, else a fresh page.
    pdf.ln(5)
    if pdf.get_y() + 30 + entry_h > pdf.h - pdf.b_margin:
        pdf.add_page()
    section_heading()

    total_rolls = len(rows)
    for i, row in enumerate(rows):
        # A setup/creel change: the change cost to reach this roll is > 0
        # ("+N in" in the row data; "start" and "0 in" mean no change).
        setup_change = i > 0 and row["change"].startswith("+")

        # Keep the entry — and its marker, if any — together on one page.
        needed = entry_h + (marker_h if setup_change else 0)
        if pdf.get_y() + needed > pdf.h - pdf.b_margin:
            pdf.add_page()
            section_heading()

        if setup_change:
            draw_setup_change_marker()

        length = row["length_lf"]
        length_txt = (f"{_clean_number(length)} LF"
                      if isinstance(length, (int, float))
                      and not isinstance(length, bool) else "")
        lot = row["navision_lot"]
        parts = [f"Roll {row['position']} of {total_rolls}"]
        if lot not in (None, ""):
            parts.append(f"Lot {lot}")
        if length_txt:
            parts.append(length_txt)
        pdf.set_x(pdf.l_margin)
        pdf.set_font("Helvetica", "B", 14)
        pdf.set_text_color(20, 20, 20)
        pdf.cell(0, heading_h, _latin1("   -   ".join(parts)),
                 new_x="LMARGIN", new_y="NEXT")

        y_bar = pdf.get_y()
        _draw_run_sheet_bar(pdf, pdf.l_margin, y_bar, bar_w, bar_h,
                            row["profile"], label_size=13)
        pdf.set_y(y_bar + bar_h + 2.0)

        widths_txt = "        ".join(
            f"{_clean_number(width)} in {code}"
            for code, width in row["profile"])
        pdf.set_x(pdf.l_margin)
        pdf.set_font("Helvetica", "B", 14)
        pdf.set_text_color(20, 20, 20)
        pdf.cell(0, widths_h, _latin1(widths_txt),
                 new_x="LMARGIN", new_y="NEXT")
        pdf.ln(entry_gap)


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

    # Item batch requirements — only when the workbook stated a yarn lbs
    # block (the report key is omitted otherwise).
    _render_item_requirements(st, extraction, report)

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
    # a note rather than erroring so the rest of the report still works. The
    # guard catches BaseException (re-raising the control-flow exceptions)
    # because a broken fpdf2/cryptography install can fail its import with a
    # BaseException-derived pyo3_runtime.PanicException, which would otherwise
    # escape and take the whole report down.
    try:
        pdf_bytes = build_run_sheet_pdf(filename, report)
    except (KeyboardInterrupt, SystemExit):
        raise
    except BaseException as exc:  # noqa: BLE001
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
                 "back to back.")

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
