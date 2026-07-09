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

### 3.2 Tiered per-inch costs

For a transition from roll A to roll B, classify every mismatched inch of B
by where its bobbins come from:

- **Zero cost** — same (item, batch) at the same position.
- **c_move** — the (item, batch) B needs is on the creel elsewhere in A's
  layout. Bobbins are repositioned: cut the yarn, move the bobbin, tie the
  new bobbin's yarn to the existing yarn feeding the tufter.
- **c_new** — the (item, batch) is not on the creel (or not in sufficient
  width). Bobbins must be fetched from inventory and mounted.
- Optionally **c_remove** — inches of A that leave the creel entirely,
  covering takedown and placing the bobbin on a storage rack for inventory
  or for another tufter (if planning assigned the same batch there).

```
cost(A → B) = c_move × inches_moved + c_new × inches_new (+ c_remove × inches_removed)
```

Per-pair computation: positions matching exactly are free; for the rest,
match B's remaining demand per (item, batch) against A's remaining supply of
the same (item, batch) — matched inches are moves, unmatched demand is new,
unmatched supply is removal. The current model is the special case
c_move = c_new with batch ignored, so this is a strict generalisation.

Notes:

- Only the **ratio** c_move : c_new matters for sequencing decisions, not
  absolute times. A rough time study or the floor's own estimate is enough
  to start.
- If c_new ≠ c_remove the cost becomes **asymmetric** (A→B ≠ B→A), turning
  the problem from symmetric TSP into asymmetric TSP. Held–Karp handles
  this unchanged; 2-opt needs adaptation. Starting point: set c_remove = 0
  and confirm with the floor whether takedown time is material.
- **Kitting lever:** if new bobbins are fetched in advance during the
  current run and staged creel-side, c_new collapses toward mount-only and
  the ratio compresses. Whether this already happens informally is a data
  question (§7). If it does, the value of batch sharing shifts from time
  saved toward material waste avoided.
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

Priority if limited: items 1–3 (the c_move : c_new ratio) and item 11
(leftover accuracy) — the first makes the cost model concrete, the second
is the go/no-go evidence for planned sharing.

## 10. Open questions

1. Length of the frozen planning window.
2. Calibrated values (or ratio) for c_move, c_new, c_remove; whether
   asymmetry (c_remove > 0) must be modelled.
3. The assignment objective when several feasible sharings exist: creel
   changes saved vs batch fragmentation vs discarded-partial waste
   (sharpens open question 2 of `docs/batch_assignment_context.md` §7).
4. Whether combined-mode "one batch per item" is enforced per original
   order or across a combined run (carried over from
   `docs/batch_assignment_context.md` §7).
5. How partial-bobbin discards are recorded so actual waste can be compared
   against the simulator's prediction.
