# Leftover Batch Utilisation and Bobbin-Level Planning — Next Phase Context

Status: **context captured, not yet implemented**. This document records the
decisions, cost-model extensions, and domain rules agreed in planning
discussion (July 2026), building on `docs/batch_assignment_context.md`. It is
the working brief for the next phase; nothing here is built yet.

## 1. The challenge

After the rolls of an order are manufactured, yarn is usually left over from
the batches assigned to its items. Because those bobbins are already hanging
on the creel, planners today try to find another order whose item
requirements the leftover can satisfy, and tuft rolls from that order next.
This saves creel changes (the item is already threaded) and reduces waste in
material and movement.

Done reactively — on the floor, after a roll finishes — this creates a
carry-over effect: every reuse rewrites the schedule, invalidates a batch
assignment made earlier, and makes it hard to track which rolls are tufted
and which are pending. The schedule is ever-changing.

A second complication: a creel change occurs even between two *identical*
layouts if their items come from different batches, because an item within
an order must come from exactly one batch. Colour match alone (the current
combined-mode assumption) is therefore not sufficient across orders.

## 2. Decision: planned sharing, not reactive reuse

Three options were considered:

1. **Reactive reuse (current practice).** Chase leftovers on the fly.
   Rejected: unstable schedule, tracking problems, carry-over effects.
2. **No sharing.** Leftovers return to inventory as new batches with new
   specs; orders adhere to the original schedule. Kept as the *fallback*,
   not the strategy — returning remnants carries handling and relabelling
   cost, and a re-received remnant may not be trusted as a consistent batch.
3. **Planned sharing (chosen direction).** During the global
   batch-assignment phase (the planned order-pool phase in
   `docs/batch_assignment_context.md` §6), allow one batch to be assigned to
   the same item across *multiple* orders when it covers each order's
   requirement, and let the scheduler place those orders adjacent. The same
   material and creel savings as reactive reuse, decided once at planning
   time and frozen into the schedule.

The key observation making this possible: the feasibility test for reuse is
static. Because of the one-batch-per-item-per-order rule, a leftover is only
usable if it covers the receiving order's *full* item requirement — a
property of the orders as sets of rolls, computable upfront from data the
pipeline already produces (`item_requirements.py`). Nothing requires waiting
until a roll is tufted.

A frozen planning window (length still to be decided) is required for this
to hold. Rush orders entering mid-window either wait or trigger a re-plan.

## 3. Cost model generalisation

The Phase 1 cost model (positional inch mismatch on colour) needs two
extensions for cross-order sequencing.

### 3.1 Identity is (item, batch), not colour

A position matches only if both the item and the batch are the same. This
captures the batch rule automatically: batch 101A → batch 102B of the same
item is a full mismatch, exactly like a different item — the bobbins come
off either way. Layout signatures in combined mode must incorporate batch
identity. Batch assignment and cross-order sequencing are therefore coupled:
shared batches are what create the zero-cost seams between orders.

### 3.2 Cost simplification: bobbins changed (pending floor confirmation)

A tiered cost model (separate rates for moving a hanging bobbin vs
fetching and mounting a new one, plus takedown) was considered and set
aside. The working position from planning discussion is that **moving an
existing bobbin to another creel location costs the same as removing the
old bobbin and mounting a new one** — either way the yarn is cut and the
new bobbin's yarn is tied to the yarn feeding the tufter. Under that
equality the tiers collapse and the cost of a transition is simply the
number of bobbins changed:

```
cost(A → B) = bobbins changed = 3 × mismatched inches, on (item, batch) identity
```

This is exactly the existing Phase 1 cost model with two adjustments, only
one of which is substantive:

- identity is (item, batch) instead of colour;
- the ×3 is a constant scale factor — it changes time estimates but never
  which sequence wins, so the optimiser can keep working in inches.

Everything already built survives verbatim: the distance matrix, Held–Karp,
2-opt/Or-opt, symmetry (no asymmetric TSP), and the MST lower bound.
Combined mode needs only batch-aware layout signatures.

Two assumptions carry this simplification and must be verified with the
floor (they are questions 1–3 and 13 in §9):

1. **Move ≈ remove-plus-mount in time.** If moving turns out much faster,
   the tiered model returns.
2. **No material fixed overhead per stop** beyond the bobbin work itself
   (restart, re-tension, quality check). See §3.3 for what happens if this
   fails.

### 3.3 Objective: long manufacturing runs

The stated business objective (CEO direction) is that tufting should be a
long-running process: minimise the number of stops, so runs between creel
changes are as long as possible.

A structural fact shapes where this can be won. **Within a single order,
stop count is invariant**: once identical layouts are clustered (which the
sequencer already does), every sequence has exactly (distinct layouts − 1)
stops. Sequencing inside an order reduces how many bobbins each stop
changes, never how many stops there are. Long runs are therefore won at
the **pool level**:

- batch sharing plus positional layout alignment creating zero-change
  seams between orders (§ below), and
- longer term, the amount of layout commonality in the order book itself —
  set upstream by field design, which scheduling can only harvest, not
  create.

Model implication: if the floor confirms a stop carries fixed overhead
beyond the bobbin changes, add a fixed penalty to every non-zero-cost
transition:

```
cost = STOP_PENALTY (if any mismatch) + bobbins changed
```

This preserves the TSP structure (a constant added to nonzero edges).
Within an order it adds a constant total and changes nothing; across
orders it makes the solver prefer perfect-alignment seams first and bobbin
savings second — the lexicographic "stops, then bobbins" objective. If the
floor reports no fixed overhead, the plan doc's original
no-stoppage-penalty assumption extends across orders unchanged.

Reporting implication: evaluation should report the **run-length
distribution** (linear feet between stops) and stop count alongside the
existing inch/bobbin cost, since run length is the quantity the business
objective is stated in.

### 3.4 Where batch sharing pays: aligned seams

At a seam between orders the saving is binary before it is linear. If the
shared item sits at the **same creel positions** in both orders' rolls and
comes from the shared batch, the seam costs zero and the machine keeps
running — the actual prize. Any mismatch means a stop regardless; sharing
then only reduces the number of bobbins changed at that stop.

Sharing a batch between two orders therefore only makes sense when their
roll layouts align positionally (or overlap substantially — a long common
stretch such as 177" of field green at identical positions stays untouched
even if 5" at the edge changes). The pairing metric is the **best seam
cost**: the cheapest transition between any roll of order A and any roll of
order B under shared-batch identity — the sequencer is free to end A and
start B on exactly those rolls, so this is precisely the saving on offer.

Candidate pairs are found by cheap filters, each using data the extractor
already produces:

1. **Spec compatibility** — same product, gauge, pile height.
2. **Layout alignment** — shared layout signatures, or low positional
   mismatch between the best roll pair.
3. **Batch feasibility** — for the items at the aligned positions: lbs
   with margin, full-bobbin count, and the per-bobbin demand check (§6).

Only pairs surviving all three become sharing candidates; everything else
keeps its own batch. Pairing couples order B's schedule to order A's
completion, so the pair list is re-confirmed at each planning cycle rather
than standing indefinitely.

Notes retained from the tiered-model discussion, still relevant:

- **Kitting** (fetching new bobbins in advance and staging them
  creel-side) no longer affects sequencing under the simplified model, but
  still matters for absolute changeover-time predictions. Whether it
  already happens informally is a data question (§9).
- If several people work a creel change in parallel, wall-clock changeover
  is not linear in bobbin count; the additive model predicts labour
  (person-minutes), not downtime directly. To be validated against a few
  whole-changeover timings.

## 4. Why the aggregate constraints are insufficient for used batches

The two constraints in `docs/batch_assignment_context.md` §4 (total lbs,
bobbin count) implicitly assume all bobbins are full and interchangeable.
That holds for a fresh batch, not for a leftover.

Worked example: batch 456 of item 123 holds 10 bobbins at 5.5 lb each
(55 lb). Order A consumes 10 lb across 2 bobbins, leaving those two with
0.5 lb each. The batch now has 8 full bobbins and 2 near-empty ones —
45 lb and 10 bobbins in aggregate. Order B needs 9 bobbins and 45 lb
(5 lb per bobbin). Both aggregate checks pass, but only 8 bobbins hold
enough yarn: the assignment is infeasible.

The true constraint is per position: each inch position carrying the item
has its own demand (the summed lengths of every roll with that colour at
that position), and each of its bobbins must hold at least that much. For a
known set of bobbin remainders, feasibility is a simple matching check:
sort position demands descending, sort bobbin remainders descending, pair
them off — feasible iff the k-th largest bobbin covers the k-th largest
demand.

## 5. Rules confirmed from the floor

Two rules shape how the per-bobbin problem is handled:

1. **Within-batch splicing is allowed; cross-batch is not.** A new bobbin
   from the *same batch* can be tied in; a bobbin from a different batch
   cannot. So a single bobbin need not cover a position's full demand, as
   long as same-batch spares exist for planned swaps.
2. **Bobbins are never allowed to run dry mid-roll.** Replacement is
   proactive. Since bobbins are not tracked today, this is currently
   managed by operator judgement.

Consequence: what is needed is per-bobbin **prediction**, not shop-floor
tracking. Feasibility for a batch becomes: total lbs (with buffer) +
width-coverage bobbins hanging + enough same-batch spares for the planned
swaps.

## 6. Per-bobbin consumption formula

Known quantities:

- w_item — the item's weight rate in lb per square inch, derivable from the
  workbook's `Yarn SKUs & Total Lbs. Needed` block (item lbs ÷ the item's
  total colour area across the order). No new measurement needed.
- **3 bobbins per inch of width, per item** (per
  `docs/batch_assignment_context.md` §4.2 — e.g. 8" max width ⇒ 24
  bobbins). A full inch of roll width therefore carries 9 bobbins across
  the three yarn types.
- Per roll: the item's width in the roll and the roll length.

For a section of width W inches and length L:

```
yarn used        = w_item × W × L
bobbins feeding  = 3 × W
lb per bobbin    = (w_item × W × L) / (3 × W) = (1/3) × w_item × L
```

Two properties follow:

- **Width cancels.** Per-bobbin consumption depends only on the length
  tufted, never on the section width. All bobbins of an item in a roll
  deplete at the same pace regardless of position.
- **Cumulative across rolls.** A bobbin's remaining yarn is its initial
  weight minus (1/3) × w_item × Σ(lengths of the rolls it has fed).

Units caveat: extracted roll lengths are in linear feet; a per-square-inch
rate needs L × 12. Alternatively derive w_item directly as lb per inch-width
per linear foot (item lbs ÷ Σ(width × length_LF)) and skip the conversion.

## 7. The bobbin consumption simulator (planned artefact)

Given a manufacturing sequence, the pipeline already knows, per inch
position, which rolls tuft there and for how long. With the formula in §6
plus one new number — standard new-bobbin net yarn weight per yarn type —
the whole run can be simulated:

- total bobbins the order consumes;
- **swap points** (which roll, which position), turning "never run dry"
  into a printed plan instead of operator vigilance;
- the end state: how many untouched full bobbins remain in each batch and
  the approximate remainder on each partial.

That end state is what planned sharing needs. The pairing policy: partition
the batch's bobbins between the paired orders before tufting starts —
designate the bobbin set the first order will consume, use its partials
within that order where possible, and discard remaining partials so the
second order's requirement is guaranteed in **full** bobbins (the only kind
countable without weighing). Sharing feasibility then reduces to:

```
predicted full bobbins remaining ≥ paired order's bobbin requirement
AND remaining lbs ≥ paired order's lbs requirement
```

Refinements:

1. **Use partials before discarding.** Partials can feed the same order's
   later rolls at short positions, or be spliced in within-batch. Discard
   is the fallback, not the default.
2. **Discards are a cost the optimiser must see.** Pairing an order onto a
   leftover batch saves a creel change but may cost N lbs of discarded
   partials; the trade is weighed, not assumed.

## 8. Buffers and uncertainty

Planning currently applies an informal buffer of roughly 10% (e.g. 900 →
1000 lb; 23,200 → 24,000–24,500 lb). The workbook lb figures themselves are
accurate. This buffer should be formalised as an explicit, tunable margin
parameter — e.g. leftovers counted at 90% of computed — rather than a
rounding habit. Per-bobbin estimates are approximate ("more or less");
swap thresholds carry the same margin. Consumption uncertainty is exactly
what the existing buffer culture already absorbs; the simulator moves it
from the planner's head into a visible number.

Validation ask: for one upcoming order, compare the simulator's predicted
bobbin count and swap points against what actually happens on the floor.

## 9. Information to collect from manufacturing

Update (July 2026): items 1–3, 12 and 13 are answered, and item 11 is
answered in the negative — see §12 for the answers and their consequences.

Ask for numbers in minutes, not descriptions — the answers become c_move
and c_new directly.

1. Time to move one bobbin to another creel location (cut the yarn, tie the
   new bobbin's yarn to the yarn feeding the tufter).
2. Whether new bobbins are fetched in advance of a creel change and staged
   next to the creel (kitting).
3. If fetched in advance, how the timing of replacing a bobbin compares
   with mounting into an empty position — including the time to place a
   removed bobbin on a storage rack for inventory or another tufter.
4. How much of a batch is typically left over after an order.
5. How often orders are mixed today (leftover of one order's batch used to
   tuft another order's rolls), and whether that is usually one item or a
   mix.
6. How many people work a creel change (parallelism vs the additive model).
7. Standard new-bobbin net yarn weight per yarn type, and its bobbin-to-
   bobbin variation.
8. The swap margin: how much yarn must remain on a bobbin before an
   operator will start the next roll on it.
9. What happens to partial bobbins today — discarded, kept creel-side,
   returned to inventory (quantifies the waste the simulator would reduce).
10. A few whole-changeover timings (total minutes, inches changed, bobbins
    moved vs new) to validate the linear cost model.
11. Leftover accuracy: for several recent orders, computed leftover vs
    actual measured leftover per batch.
12. Batch data shape from Business Central: per batch — item number, batch
    id, available lb, bobbin count, receipt date; whether bobbin count is
    tracked per batch; export vs live query, and refresh frequency.
13. When the machine stops for a creel change, is there fixed time lost
    beyond the bobbin changes themselves (restart, re-tension, quality
    check)? This decides whether a per-stop penalty is added to the cost
    model (§3.3).

Priority if limited: items 1–3 and 13 (they verify the two assumptions the
simplified cost model rests on, §3.2) and item 11 (leftover accuracy, the
go/no-go evidence for planned sharing).

## 10. Open questions

Update (July 2026): questions 2 and 3 are resolved and question 1 has a
provisional answer — see §12.

1. Length of the frozen planning window.
2. Floor confirmation of the two assumptions behind the simplified cost
   model (§3.2): move ≈ remove-plus-mount timing, and whether a stop
   carries fixed overhead requiring the per-stop penalty (§3.3).
3. The assignment objective when several feasible sharings exist: creel
   changes saved vs batch fragmentation vs discarded-partial waste
   (sharpens open question 2 of `docs/batch_assignment_context.md` §7).
4. Whether combined-mode "one batch per item" is enforced per original
   order or across a combined run (carried over from
   `docs/batch_assignment_context.md` §7).
5. How partial-bobbin discards are recorded so actual waste can be compared
   against the simulator's prediction.

## 11. First step — implemented

The per-item weight data and a first cut of the consumption model are
implemented:

- `data/item_bobbin_data.csv` maps each item number to its weight in lb per
  square foot and its fresh bobbin weight from extrusion. It is edited
  directly on GitHub; it is seeded with item 121051 (5040 XP+ (6Pin), FG,
  0.04831 lb/sqft) and the remaining items and fresh bobbin weights are to
  be filled in.
- `bobbin_usage.py` computes, per matched item, each roll's consumption per
  bobbin (`w × length_ft / 36`, §6) and the depletion along the optimised
  sequence, tracked per inch position from the front of the machine (fixed
  creel alignment) and reported as depletion groups — one entry per set of
  positions with an identical coverage history. When the fresh bobbin
  weight is filled in it also places planned swap points (zero margin for
  now; the swap margin from §9 question 8 becomes a parameter later) and
  counts the fresh bobbins each group consumes.
- The Phase 4 report carries this under a `bobbin_usage` key, the web app
  shows an "Item bobbin usage" card, and the downloadable run-sheet PDF
  gains a matching section with red BOBBIN SWAP bands alongside the
  existing SETUP CHANGE bands.

Since then, the batch ledger is implemented (`batch_ledger.py`, July 2026):

- The batch-availability side has an interim shape: a batch inventory
  workbook (`batch_number`, `item_number`, `number_of_bobbins`,
  `weight_per_bobbin`, `total_batch_weight`), uploaded in the app or passed
  via `--batches`, standing in for the Business Central feed (§9 question
  12) until that connection is decided.
- One batch is assigned per item (smallest feasible; largest with a warning
  when none covers), checked against the order's pounds **with the buffer**
  and bobbin count — the §8 buffer is now the explicit, tunable parameter
  this document asked for (default 10%).
- The whole optimised run is simulated per bobbin using the §6 formula with
  the rate derived from the workbook's yarn lbs block, and the report
  carries the **end state** §7 needs: untouched full bobbins remaining and
  every partial's remaining pounds, on creel and creel-side.
- Partial-bobbin reuse within an order is implemented as the ledger's
  mounting rule: removed partials are re-mounted best-fit (smallest
  sufficient, buffered) before any fresh bobbin is drawn, and a hanging
  bobbin that cannot cover the next roll plus buffer is swapped proactively
  (§5 — never dry mid-roll; within-batch splicing only, which the
  one-batch-per-item rule guarantees).

Not yet implemented from this document: the live Business Central
connection, the sharing feasibility pipeline across orders (§3.4), and
batch-aware layout signatures in combined mode (§3.1). The per-stop
penalty is no longer pending — it is ruled out by the July 2026 answers
(§12, item 1).

## 12. Planning answers — July 2026

Answers obtained in planning discussion (July 2026), resolving several of
the questions in §9–10. Recorded here with their model consequences.

1. **No fixed per-stop overhead; no move-vs-new time difference**
   (§9 questions 1–3 and 13). The only cost of a stop is the time taken to
   make the changes themselves. Because scheduling this project produces a
   known manufacturing order in advance, bobbins are fetched and staged
   before the stoppage (kitting), so replacing a bobbin and mounting a
   fresh one cost the same. Consequences: the per-stop penalty of §3.3 is
   **not** added; the simplified cost model of §3.2 stands — cost equals
   bobbins changed, 3 × mismatched inches per item on (item, batch)
   identity — and everything already built survives unchanged. Long runs
   emerge from minimising bobbins changed, not from a lexicographic
   stops-first objective. A partial seam match (e.g. field green matched
   across two orders, a 5" white stripe changed) therefore retains almost
   all of a full match's value; full-item-set matching is preferred only
   by its bobbin saving.

2. **Fresh-bobbin weights are obtainable; leftover actuals are not**
   (§9 questions 7 and 11). Fresh bobbin net weight per yarn type will be
   collected. Leftover quantities are not tracked on the floor today and
   will not be, so the computed-vs-actual leftover comparison this
   document asked for is unavailable. Replacement validation path: for one
   upcoming order, check the plan at the **swap-point level** (do
   operators swap where the run sheet's BOBBIN SWAP bands say) and
   reconcile **fresh bobbins consumed** per order — both countable without
   weighing anything. Until validated, the receiving order's guarantee
   stays in full bobbins only (§7) and the buffer carries the prediction
   risk.

3. **Business Central batch fields confirmed** (§9 question 12). Per
   batch: item number, batch weight, number of bobbins, and associated
   dates. Both aggregates the ledger needs exist natively; weight ÷
   bobbins cross-checks the fresh-bobbin weight, and the dates allow
   age-based tie-breaks. The interim upload workbook already matches this
   shape. Still open: export vs live connection and refresh cadence — an
   integration detail, not a design blocker.

4. **Planning window: provisionally one week** (§10 question 1). Orders
   carry a 21-day order-to-delivery guarantee, so a one-week frozen window
   leaves roughly two further cycles of slack. Default rush-order handling
   is therefore "join the next window", with mid-window re-planning as the
   exception. Not yet fixed.

5. **Assignment objective: bobbins changed first** (§10 question 3). When
   several feasible sharings compete for a batch, the number of
   stops/bobbins changed is the primary criterion. Small discarded
   partials are absorbed by logo tufting elsewhere in the facility, so
   small discards are priced at or near zero; only large discards need
   weighing against a pairing's saving. Batch sizes are fixed as received
   from extrusion — assignment is pure allocation over the given
   inventory, never resizing.

Remaining open after these answers: the fresh-bobbin weight values
themselves (data pending), Business Central export-vs-live and refresh
cadence, confirmation of the one-week window, and the combined-run
one-batch semantics (§10 question 4, provisionally "across the run" in
the batch ledger).

## 13. Multiple tufting machines

The facility runs more than one tufting machine, each with its own creel
and bobbins (noted July 2026). The single-machine framing in this document
and the optimisation plan generalises by decomposition rather than by
changing the model:

1. **Two-level structure.** An outer step assigns orders to machines; the
   existing pipeline (sequencing, seams, ledger simulation) then runs
   unchanged as the inner loop, once per machine over that machine's
   queue. Nothing already built needs modifying to survive this.

2. **Batch sharing is machine-local.** A zero-change seam exists only on
   one physical creel, so two orders sharing a batch for their seam must
   be assigned to the *same* machine. The §3.4 pairing graph therefore
   becomes an input to machine assignment: the value of co-assigning two
   orders is their best seam saving.

3. **Machine eligibility comes first.** Spec compatibility (product,
   gauge/thread-up, pile height, roll width) gates which machines an
   order can physically run on, before any optimisation.

4. **Clustering vs delivery balance.** Dedicating a machine to a layout
   family maximises seam savings, but the 21-day delivery promise bounds
   how much work can pile onto one machine. The outer objective weighs
   bobbin changes saved against due-date balance across machines — the
   first place delivery dates enter the model.

5. **Per-machine creel state.** The known-start-state seeding deferred to
   Phase 6 of the optimisation plan applies per machine: a machine that
   keeps a common layout threaded across planning windows becomes that
   family's "home", a standing saving the assignment step should see.

6. **Leftovers are per creel.** The ledger simulates each machine's queue
   separately. Bobbins can physically move between machines (they are
   already racked for "another tufter", §9 question 3), but only the yarn
   moves — the threading saving is machine-local, so a transferred
   leftover is inventory, not a seam.

Questions to confirm with manufacturing:

- how many machines, and whether they are identical (roll width,
  gauge/thread-up, yarn types they can carry);
- which products run on which machines;
- whether one order's rolls are ever split across machines, and whether
  one batch may feed two machines at once (the one-batch-per-item rule
  does not obviously forbid it, but it fragments the batch and its
  leftover prediction);
- how work is allocated to machines today, and by whom.
