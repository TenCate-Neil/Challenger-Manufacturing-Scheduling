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
- **Yarn lbs** (rows 638-647, the `Yarn SKUs & Total Lbs. Needed` block): per
  yarn type and colour, the item/SKU number and the total pounds of that yarn
  the order requires, as already calculated inside the workbook. Emitted as
  `yarn_lbs`; this is the demand side of batch assignment (see below).
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

## MVP front end (Phase 5)

`app.py` is a thin [Streamlit](https://streamlit.io) front end over the same
core functions the CLI uses (`docs/optimisation_plan.md`, Phase 5). It adds no
logic of its own: it uploads an order workbook, runs the existing extractor and
the Phase 4 evaluator, and shows the ordered manufacturing sequence, the
achieved setup cost, the solution quality, the conservation result, and the
transition breakdown. The JSON report can be downloaded, as can a printable PDF
run sheet for the manufacturing floor.

It runs locally — there is nothing to host or deploy:

```bash
pip install -r requirements.txt   # now includes streamlit and fpdf2
streamlit run app.py
```

Upload one or more `.xlsx` order workbooks (the `FIELD LAYOUT` sheet). The
sidebar exposes the same two thresholds as the CLI: the distinct-layout count
below which the sequence is solved exactly, and the count below which an exact
oracle is still run to report the true optimality gap.

Each report is laid out as a set of bordered section cards, and two of them draw
the layouts as colour bars — a roll's threading profile shown left to right
across the roll width, with a 1px separator between segments so every boundary
is visible:

- **Distinct layouts** (above solution quality): one bar per unique threading
  profile in the order, with how many rolls use it and a colour legend. Yarn
  codes map to sensible real colours (field green stays green, white stays
  white); unrecognised codes get a stable fallback colour, so the same code is
  the same colour in every bar.
- **Full manufacturing order** (at the bottom): one bar per physical roll in
  manufacturing order — `roll_qty` is expanded, so an entry for five rolls draws
  five bars — with the setup change cost (inches re-threaded) incurred to reach
  each roll. Runs of identical consecutive layouts read as a block of matching
  bars at zero cost.

These are display-only: they read the layout signatures already in the Phase 4
report and add no sequencing logic.

Alongside the JSON download there is a **Download run sheet (PDF)** button — a
printable sheet for the manufacturing floor. It lists the rolls in manufacturing
order, one row per physical roll (`roll_qty` expanded, exactly as the full
manufacturing order view), and shows the position, purchase order number,
Navision lot number, panel numbers, length (LF), the same layout colour bar,
and the per-step setup change cost. A header carries the source file, total
setup cost, and roll/layout counts, and a **Layout threading breakdown**
section at the bottom is written for the manufacturing floor: one large-print
entry per physical roll in manufacturing order, each with a tall colour bar of
its threading, the segment widths spelled out (e.g. `177 in FG   5 in WHI`),
the roll's length in LF and its lot number. Wherever consecutive rolls differ
in layout the section inserts a red **SETUP CHANGE** band between them, so
creel changes are impossible to miss. It is rendered with
[fpdf2](https://py-pdf.github.io/fpdf2/), which is
pure Python — a plain `pip install`, with no system libraries or browser to set
up — and the colour bars are drawn from the same colour mapping as the on-screen
ones. Where fpdf2 is not installed the app falls back to a note and the rest of
the report still works.

The extraction/optimisation pipeline is factored into `analyse_upload`, and the
run sheet into `_run_sheet_rows` / `build_run_sheet_pdf`; none import Streamlit,
so they can be exercised without a browser. Streamlit is imported inside `main`,
keeping the module importable for tests even when Streamlit is not installed.

Tests drive the app headlessly through Streamlit's `AppTest` harness and skip
cleanly when Streamlit (or the extractor's `openpyxl`) is not installed:

```bash
python test_app.py                 # standalone runner
pytest test_app.py                 # if pytest is installed
```

## Item batch requirements

`item_requirements.py` computes, per item (a yarn type + colour combination,
identified by its item/SKU number), what an inventory batch must cover for an
order. The rules are recorded in `docs/batch_assignment_context.md`; a single
batch must satisfy both figures, and an item is never split across two batches
within an order:

- **Pounds needed**: taken directly from the workbook's `Yarn SKUs & Total
  Lbs. Needed` block (rows 638-647), extracted as `yarn_lbs` — not recomputed.
- **Bobbins needed**: `ceil(max width × 3)`, where max width is the largest
  total width in inches of that item's colour within any single roll of the
  order. Same-colour segments in one roll are summed (5" + 5" needs the full
  10" covered); roll length and quantity play no part — length drives pounds,
  width drives bobbins.

`item_requirements(extraction)` returns one entry per item with the item
number, yarn type, colour, pounds, max width, and bobbins. When an extraction
carries `yarn_lbs`, the Phase 4 report gains an `item_requirements` key and
the app shows an **Item batch requirements** table after the distinct
layouts. Colours that appear on one side only (in the lbs block but never in
a roll, or the reverse) are reported as warnings rather than dropped
silently.

```bash
python item_requirements.py EXTRACTED.json [EXTRACTED2.json ...]
```

Tests:

```bash
python test_item_requirements.py  # standalone runner
pytest test_item_requirements.py  # if pytest is installed
```

## Item bobbin data

`data/item_bobbin_data.csv` holds the per-item weight data behind the bobbin
usage model: one row per item, with columns `item_number`, `yarn_type`,
`color_code`, `weight_lb_per_sqft` (the yarn's tufted weight per square foot
of turf) and `fresh_bobbin_weight_lb` (the pounds of yarn on a fresh bobbin —
may be left blank until it has been measured). The table is meant to be edited
directly on GitHub: add a row to cover a new item, or fill in a fresh bobbin
weight once weighed, and the next analysis picks it up.

The pounds each hanging bobbin loses while one roll is tufted follow from the
threading geometry: one inch of item width covers 1/12 sqft per foot of roll
length, and that inch is fed by 3 bobbins, so each bobbin supplies

```
lb per bobbin = weight_lb_per_sqft × length_lf / 36
```

— the item's width cancels out, so bobbins fed by the same rolls drain at the
same rate. Because real orders vary an item's width across rolls, consumption
is tracked per inch position from the front of the machine (layouts align at
the front — the same fixed-creel assumption the setup cost model uses):
positions that join late, on the extra inches of the wider rolls, carry their
own, lower depletion instead of inheriting the full-run total.

When an order's items match rows in the table, the Phase 4 report gains a
`bobbin_usage` key and the app shows an **Item bobbin usage** section — on
screen after the item batch requirements, and appended to the run sheet PDF.
Per item it lists every roll the item appears on, in manufacturing order, with
the pounds drawn per bobbin and the running cumulative of the deepest-drawn
covered position, plus a **bobbin depletion groups** table — one row per set
of positions fed by the same rolls, with its width, bobbin count, rolls fed,
pounds drawn per bobbin, swaps, fresh bobbins consumed, and the pounds left
on the hanging bobbin at order end. Once the item's fresh bobbin weight is
filled in, the run sheet also plans **BOBBIN SWAP** bands (red, like the
SETUP CHANGE bands) before any roll that some positions' remaining yarn
cannot cover, naming how many bobbins to replace — just the positions that
run short, not the roll's full hanging count; without it, usage is still
reported but no swaps are planned.

The model's assumptions are stated alongside the figures: layouts align from
the front of the machine (so an inch position keeps its bobbins across rolls
and setup changes, and rolls that do not cover a position leave it
untouched), and there is no safety margin — a position's bobbins are swapped
the moment the next roll's draw would exceed what remains on them.

## Batch ledger

`batch_ledger.py` brings the batch side of planning into the pipeline: it
takes the batches available in inventory, assigns one batch to each item of
an order (one batch per item, never split — the rule in
`docs/batch_assignment_context.md` §4), and simulates the optimised run
bobbin by bobbin, so the leftover at order end is a predicted number instead
of something discovered on the floor.

The batch inventory is a small workbook, one row per batch, with columns
`batch_number`, `item_number`, `number_of_bobbins`, `weight_per_bobbin` and
`total_batch_weight` (column order does not matter). Every bobbin is taken
to be full at `weight_per_bobbin`; a stated total that disagrees with
bobbins × weight is warned about, and bobbins × weight is what the ledger
uses. This upload is the interim shape of the Business Central feed the
next-phase brief plans for — when that connection lands, it replaces the
manual export, not the ledger.

What the simulation does, per item:

- **Requirement check.** The assigned batch must hold the order's pounds
  (from the workbook's own yarn lbs block) **plus a planning buffer**
  (default 10% — the planners' informal margin made an explicit, tunable
  parameter, per the next-phase brief §8) and the required bobbin count
  (max item width × 3). Among feasible batches the smallest wins, keeping
  large batches for demands that need them; when none is feasible the
  largest is simulated anyway and the shortfall is reported, not hidden.
- **Consumption.** The rate needs no new measurement: it is derived as
  item lbs ÷ the item's tufted area across the order, and each roll draws
  `rate × length / 36` per bobbin — the same physics as the bobbin usage
  model, so total draw equals the workbook's stated pounds by construction.
- **The creel rule.** Bobbins hang while their inch positions carry the
  item and come off at the setup change that gives those positions to
  another colour. Removed bobbins are kept creel-side and **re-mounted
  wherever their remaining yarn covers the position's upcoming demand plus
  the buffer, before any fresh bobbin is used** — so when 182" of field
  green narrows to 177" and later widens back, the 15 bobbins that came
  off are the ones that go back on. Selection is best-fit (smallest
  sufficient partial first), preserving fuller bobbins for deeper demands.
  A hanging bobbin that cannot cover the next roll plus buffer is swapped
  out beforehand — bobbins never run dry mid-roll.
- **End state.** How many untouched full bobbins remain in the batch, and
  every partial's remaining pounds — grouped by weight, split into still
  hanging on the creel vs removed creel-side. This is exactly the leftover
  picture the planned cross-order batch sharing needs.

In the app, a second (optional) uploader takes the batch workbook and the
sidebar gains a **Planning buffer (%)** setting; each report then shows a
**Batch ledger** card — assignment and feasibility per item, the
roll-by-roll mounts (fresh vs re-used), releases and swaps, and the
leftover table. The Phase 4 JSON report carries the same data under a
`batch_ledger` key (omitted when no batch matches, so existing reports keep
their shape). In combined mode one batch serves an item across the whole
combined run — the combined-mode semantics question in the docs, resolved
provisionally in the "across the run" direction.

```bash
python batch_ledger.py EXTRACTED.json --batches BATCHES.xlsx [--buffer 0.1]
python evaluate.py EXTRACTED.json --batches BATCHES.xlsx   # adds the key
```

Tests:

```bash
python test_batch_ledger.py        # standalone runner
pytest test_batch_ledger.py       # if pytest is installed
```

## Combined mode

Several orders can be joined and sequenced as one combined run.
`join_orders(extractions)` (in `roll_sequencing.py`) concatenates the rolls of
each extraction, tagging every roll with the `source_file` it came from, and
carries all input file names in the combined `source_file`. Nothing else needs
reconciling: Phase 2 groups on the canonical threading profile, so layouts
shared between files collapse together automatically, and Navision lot numbers
are globally unique across files, so conservation carries over unchanged. The
combined `mfg_summary` sums `mfg_rolls` / `mfg_lf` / `mfg_sf` across the
inputs, and Phase 4's cross-check compares those summed stated totals against
the combined sequence — the same second-source conservation check as for a
single file. When every input carries a `yarn_lbs` block those are merged too
— pounds summed per item, max width taken across all combined rolls — so a
combined report includes item batch requirements for the whole run; if any
input lacks the block, the key is omitted with a warning, and an item number
disagreement between files for the same yarn/colour is also warned about.

```bash
python evaluate.py A.json B.json --combine        # one combined report
python sequencer.py A.json B.json --combine       # one combined sequence
```

In the app, uploading two or more workbooks shows a toggle: **Separate
schedules** (each workbook sequenced on its own, the default) or **Combined**
(all rolls joined into one order and sequenced together). A combined order is
one manufacturing schedule: it renders as a single report with its own PDF run
sheet, exactly like a single-file order.

### How far the exact solver goes

Held–Karp is O(2^n · n²) time and O(2^n · n) memory in the number of
*distinct layouts* (not rolls). In practice it is comfortable to ~15–18
distinct layouts, tolerable to ~20, and ~22 is the ceiling — the limit is
memory for the 2^n DP table, not time. Above the configured threshold the
multi-start nearest-neighbour + 2-opt/Or-opt heuristic runs instead, and the
report quotes its quality gap against the minimum-spanning-tree lower bound
(and, where the instance is small enough, against an exact oracle).
