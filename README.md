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
  Each roll also carries a `layout_signature` (the ordered width/colour
  segments, e.g. `5WHI|177LIM`) and a `layout_group` id: rolls sharing
  the same signature have an identical physical layout - length
  (`mfg_roll_length_lf`) plays no part in this - and are interchangeable for
  sequencing purposes. `distinct_layout_count` at the top level reports how
  many distinct layout groups the order contains.

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

## Roll sequencing cost model (Phase 1)

`roll_sequencing.py` is the costing layer described in
`docs/optimisation_plan.md`. It measures how expensive a manufacturing
sequence is to set up; it does not reorder anything (choosing a cheaper
sequence is a later phase).

Each roll is treated as a positional profile across the roll width. The
setup change cost between two consecutive rolls is the total width, in
inches, of the positions whose colour/type code differs:

```
cost(A, B) = sum of widths where profile_A(x) != profile_B(x)
```

The cost is symmetric, and two identical layouts cost 0. The cost of a whole
sequence is the sum of its adjacent-pair costs. For the worked example in the
plan — a 182" `FG` roll followed by a 177" `FG` + 5" `WHI` roll — only the
final 5 inches change, so the cost is 5.

Score an extraction result in the order its rolls appear:

```bash
python roll_sequencing.py EXTRACTED.json [EXTRACTED2.json ...]
```

This prints the total setup cost and a transition breakdown (how many
transitions are zero-cost and the distribution of the rest). The as-extracted
order is assigned by sales and is not a target to beat; scoring it is just a
way to exercise the model.

The module exposes the same functions for reuse by later phases and a future
front end: `transition_cost(roll_a, roll_b)`, `sequence_cost(rolls)`, and
`transition_breakdown(rolls)`.

Run the tests (no extra dependencies required):

```bash
python test_roll_sequencing.py     # standalone runner
pytest test_roll_sequencing.py     # if pytest is installed
```

## Collapse duplicates and distance graph (Phase 2)

`layout_graph.py` prepares the sequencing problem described in
`docs/optimisation_plan.md` section 4, steps 1 and 2. It does not choose an
order (that is Phase 3); it collapses the order into distinct layouts and
computes the distances between them.

- **Collapse duplicates** (`collapse_layouts`): rolls that share an identical
  threading profile can be produced in any internal order at zero cost, so
  they are grouped into distinct layouts. This shrinks the problem from
  "number of rolls" to "number of distinct layouts". Grouping is on the
  canonical threading profile — the exact condition under which the Phase 1
  cost between two rolls is 0 — and the extractor's `layout_group` /
  `layout_signature` are carried through, with any disagreement reported as a
  warning.
- **Distance graph** (`distance_matrix`): the pairwise setup change cost
  between every pair of distinct layouts, using the Phase 1 cost. The matrix
  is symmetric with a zero diagonal.
- **Expansion** (`expand_sequence`): the inverse of collapsing. Given an order
  of distinct layouts it expands each back into its member rolls, recovering a
  full roll sequence. Because grouping keeps every roll, expansion always
  reproduces exactly the rolls that went in (conservation).

Inspect the collapsed layouts and distance graph for an order:

```bash
python layout_graph.py EXTRACTED.json [EXTRACTED2.json ...]
```

Tests:

```bash
python test_layout_graph.py        # standalone runner
pytest test_layout_graph.py        # if pytest is installed
```

## Sequencing engine (Phase 3)

`sequencer.py` chooses the order in which the distinct layouts are
manufactured to minimise total setup change cost — Step 3 of
`docs/optimisation_plan.md` section 4.

The problem is the **open-path** form of the symmetric Travelling Salesman
Problem: order the distinct layouts so the summed positional inch mismatch is
lowest. It is an open path, not a cycle — the sequence may start and end
anywhere and never returns to its start, matching the "fresh start" assumption
(no fixed current machine threading to cost against).

Tiered solver:

- **Small orders** (at most `--exact-max-layouts`, default 15 distinct
  layouts): solved exactly with a **Held–Karp** dynamic program, returning the
  proven minimum-cost order.
- **Larger orders**: a **multi-start nearest-neighbour** construction improved
  by **2-opt** and **Or-opt** local search, returning a near-optimal order.

`optimise(rolls)` ties it together: collapse duplicates and build distances
(Phase 2), sequence the distinct layouts, then expand back to the full roll
sequence (conserving every roll). It returns the layout order, the expanded
`sequence`, the achieved `cost`, the `method`, and whether the result is a
proven `optimal`.

```bash
python sequencer.py EXTRACTED.json [EXTRACTED2.json ...]
python sequencer.py EXTRACTED.json --exact-max-layouts 12
```

Tests (includes a brute-force optimum oracle for small instances and a
heuristic-vs-exact quality check):

```bash
python test_sequencer.py           # standalone runner
pytest test_sequencer.py           # if pytest is installed
```

## Evaluation and reporting (Phase 4)

`evaluate.py` takes the optimised sequence from Phase 3 and reports whether it
can be trusted and how good it is — the four criteria in
`docs/optimisation_plan.md` section 6. It adds no sequencing logic; it measures
and explains the Phase 3 result, and emits the optimised sequence as JSON.

- **Conservation** (`check_conservation`): the optimised sequence must be a
  faithful reordering of the order as extracted — same rolls, same quantities,
  same linear/square-foot totals, and no roll's own layout altered. Each check
  records the original and sequenced value; any discrepancy is listed rather
  than hidden. When the full extraction is available its MFG summary totals are
  cross-checked as a second, independent source.
- **Achieved cost**: the total setup change cost (inches re-threaded) of the
  optimised sequence, in absolute terms, carried straight from Phase 3.
- **Solution quality** (`solution_quality`): for orders solved exactly the
  result is the proven minimum (gap 0). Otherwise we report the gap to a
  **minimum spanning tree lower bound** — a valid floor, because any
  Hamiltonian path is itself a spanning tree and so cannot cost less than the
  cheapest one. Where the instance is small enough (`--oracle-max-layouts`,
  default 16) we also solve it exactly as an oracle and report the *true* gap.
- **Transition breakdown**: how many transitions are zero-cost (identical
  consecutive rolls) and the distribution of the rest (reused from Phase 1).

`evaluate(rolls)` returns a single JSON-serialisable report tying these
together, including the ordered `manufacturing_sequence` with the setup change
cost incurred at each step. `report_json` renders it to JSON.

```bash
python evaluate.py EXTRACTED.json [EXTRACTED2.json ...]
python evaluate.py EXTRACTED.json -o reports/   # also write <stem>.sequence.json
```

The CLI prints a summary and, with `-o`, writes the full report per input. It
exits non-zero if any order fails its conservation check.

Tests (conservation pass/fail, the lower bound never exceeding the optimum, and
JSON-serialisability):

```bash
python test_evaluate.py            # standalone runner
pytest test_evaluate.py            # if pytest is installed
```
