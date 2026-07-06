# Turf Layout Extractor

Extracts manufacturing data from the `FIELD LAYOUT` sheet of turf order
workbooks (e.g. `LOWELL_HSFWREVA.xlsx`, `POLK_E.S.PIVOT_100.xlsx`,
`Richland_HS_FB.xlsx`) into structured JSON.

## Usage

```bash
pip install -r requirements.txt
python extract_turf_layout.py FILE1.xlsx FILE2.xlsx ... -o output_dir/
```

One JSON file is written per input workbook.

## What gets extracted

- **General Information** (`B2:K10`): project name, customer, PO#, ship date,
  logo fabricator, pre-shipment testing.
- **Product Specifications** (`M2:U12`): product name, brand, pile height,
  face weight, gauge, thread-up, stitch rate, every active yarn type and its
  face-weight contribution, primary backing, secondary coating, product type,
  perforations, roll width, total weight.
- **MFG summary** (`M15:T16`): total rolls, linear feet, square feet, weight,
  and truck counts (LTL/FTL).
- **Yarn SKUs** (rows 671-676): each active creel position, its yarn type,
  and every color that yarn is available in with its SKU.
- **Roll layout** (row 684 onward, until the `Creel Change` marker): one
  entry per roll — lot number, panel numbers, quantity, length, and the
  left-to-right width/color segment layout across the 182" roll. Rolls that
  produce multiple panels from an identical layout (no new lot number on the
  follow-up row) are folded into `additional_panel_layouts` instead of being
  miscounted as separate rolls. `setup_group` increments on every
  `SETUP CHANGE` marker, matching the physical creel reconfiguration count.

## The Brand field

`Brand` is not stored as text in the source file — it's an embedded logo
image using Excel's "Picture in Cell" feature. The script resolves it by
walking the workbook's rich-data XML layer (`xl/richData/...`) to find the
underlying image, hashing it, and matching it against `KNOWN_BRAND_LOGOS`.
Only three brand logos have been seen so far (TenCate Grass, TigerTurf, GEO
Surfaces). If a workbook uses an unrecognized logo, the output will contain
`"UNKNOWN_LOGO:<hash>"` and a warning — add the new hash/name pair to
`KNOWN_BRAND_LOGOS` in `extract_turf_layout.py`.

## Validation built in

- Every fixed-position label (`PRODUCT SPECIFICATIONS`, `Creel Position`,
  `Sort`, etc.) is checked against what's actually in the cell; a mismatch
  produces a warning instead of silently extracting wrong data.
- Each roll's width segments are checked to sum to its stated roll width;
  a mismatch is reported as a warning with the row number.
- Unresolved brand logos and orphaned continuation rows are also reported
  as warnings rather than failing silently.

Warnings are returned per-file in the `warnings` list and also printed to
the console when running the script.
