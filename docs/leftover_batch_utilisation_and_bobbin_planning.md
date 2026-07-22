# Leftover Batch Utilisation and Bobbin-Level Planning

- **Purpose:** working brief for the current phase — cross-order batch
  sharing, per-bobbin planning, and the cost-model extensions they need.
- **Audience:** planners agreeing the approach; developers building the
  order-pool phase.
- **Scope:** everything downstream of within-order sequencing: leftover
  batches, per-bobbin feasibility and simulation, cross-order seams,
  scheduling across multiple tufting stations, and the data still to be
  collected from the floor.
- **Depends on:** `docs/batch_assignment_context.md` (domain model this
  builds on) and `docs/optimisation_plan_Stage1.md` (the cost model §3
  generalises). See `docs/README.md` for the full document map.
- **Status:** decisions and cost-model extensions agreed in planning
  discussion (July 2026); first steps implemented (§11), the cross-order
  pipeline not yet built.

## 1. The challenge

The goal of this phase — and of the project — is to **increase
manufacturing throughput**. Downtime is the main lever: the tufting
machine only produces while it runs, so fewer stops and fewer bobbins
changed per stop translate directly into output. Optimising the usage of
leftover yarn is beneficial and falls out of the same planning, but it is
a secondary benefit, not the main goal.

After the rolls of an order are manufactured, yarn is usually left over
from the batches assigned to its items, and those bobbins are still
hanging on the creel. The floor's response today is to jump between
orders based on what is on the creel: rather than following the planned
schedule, operators pick whichever order's rolls need the fewest bobbin
changes given the current threading, and tuft those next. The intent is
exactly right — they are trying to minimise downtime — but judging
bobbin-change counts across the open order book by eye is close to
impossible, so the result is approximate, and the schedule as planned is
not the schedule that runs.

Done reactively — on the floor, after a roll finishes — this creates a
carry-over effect: every jump rewrites the schedule, invalidates a batch
assignment made earlier, and makes it hard to track which rolls are tufted
and which are pending. The schedule is ever-changing.

A second complication: colour match alone (the current combined-mode
assumption) is not sufficient across orders. What hangs at a creel
position is an **item** — a yarn type + colour combination with its own
item number (`docs/batch_assignment_context.md` §3) — so two positions
only avoid a change if the item numbers match, not merely the colours.
And even a colour match forces a partial creel change when the
**5040 XP+ (6Pin)** yarn — the one batch-sensitive yarn type — comes from
different batches, because a 5040 item within an order must come from
exactly one batch. Cross-order identity is therefore
(colour, 5040 batch) — see §3.1.

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
   requirement, and let the scheduler place their rolls in close proximity
   (adjacency is emergent at roll level — §3.4). The same material and
   creel savings as reactive reuse, decided once at planning time and
   frozen into the schedule.

Planned sharing computes what the floor already tries to approximate by
eye — the order-to-order transition that changes the fewest bobbins — and
does it across the whole order pool at once, producing a schedule that
captures the downtime savings *and* can actually be followed.

Sharing is the primary way scheduling reduces downtime. A later-stage
complement sits upstream: pushing extrusion for larger batch sizes of
items that are used a lot, so a single batch can cover more orders and
create more sharing opportunities (§3.3).

The key observation making this possible: feasibility is computable at
planning time. Because of the one-batch-per-item-per-order rule, a
leftover is only usable if it covers the receiving order's *full* item
requirement — a property of the orders as sets of rolls, derivable from
data the pipeline already produces (`item_requirements.py`). Where the
paired orders draw on the batch in sequence and may splice within it
(§7), the check additionally depends on the planned manufacturing order —
which order consumes first and what is predicted to remain — but that too
is a simulation the pipeline runs before anything is tufted. Nothing
requires waiting until a roll is tufted.

A frozen planning window (length still to be decided) is required for this
to hold. Rush orders entering mid-window either wait or trigger a re-plan.

## 3. Cost model generalisation

The Phase 1 cost model (positional inch mismatch on colour) needs two
extensions for cross-order sequencing.

### 3.1 Identity is (colour, 5040 batch), not colour alone

Colour is only part of what identifies a threaded position. The unit that
hangs there is an item — a yarn type + colour combination with a unique
item number (`docs/batch_assignment_context.md` §3). Within a single
order, colour is a safe shorthand: every colour segment implies all three
yarn types, and each item carries exactly one batch. Across orders neither
holds, so the full identity is needed.

Batch sensitivity is not uniform across the three yarn types (confirmed
in planning discussion, July 2026): only the **5040 XP+ (6Pin)** yarn
cannot mix batches. The other two, MF TXT 7200/10 and SXT 5400/6, may mix
across batches freely — all three feed into the same tufted inch, but
batch mixing in those two is not visible in the finished field. The full
identity of a threaded position is therefore **(colour, batch of the
5040 XP+ item)**: the two batch-insensitive yarn types never force a
change at a colour-matched position.

Batch 101A → batch 102B of the same 5040 item is consequently *not* a
full mismatch: only the 5040 bobbins come off — a third of the position's
bobbins — while the other two yarn types keep hanging (§3.2 for the
tiered cost). Layout signatures in combined mode must incorporate the
5040 batch identity. Batch assignment and cross-order sequencing remain
coupled: a shared 5040 batch is what turns a colour-matched seam from a
one-third change into a zero-cost one.

Telling the sensitive items apart is a data lookup, not a heuristic.
Every yarn type + colour combination is an item number
(`docs/batch_assignment_context.md` §3), so a **catalogue of all item
numbers with their yarn type** identifies exactly which items are
5040 XP+ (6Pin). That catalogue already has a home:
`data/item_bobbin_data.csv` carries `item_number, yarn_type, color_code`
alongside the weights the simulator needs — populating it across all
items delivers the sensitivity flag, the layout join (colour code), and
the consumption rates in one table. The workbook's Yarn SKUs block gives
a per-order cross-check, since it lists each order's SKUs under their
yarn type. If products with other batch-sensitive yarns appear later, an
explicit per-yarn-type sensitivity flag would replace inferring it from
the 5040 name.

The coupling also runs in the deciding direction: batch assignment is not
a pre-step with its own separate objective. The optimisation algorithm
assigns batches to (order, item) pairs so that the pooled schedule needs
the **least number of bobbin changes** — which is where batch sharing is
maximised. Sharing pays exactly when both conditions hold at once: many
orders' items can draw on the same batch (it covers their combined
requirement), *and* their roll layouts for the shared item are similar,
so the shared positions stay threaded across the seam (§3.4).

The one-batch-per-item-per-order rule also works in the solver's favour:
it shrinks the feasible space. A zero-change run can only extend across
rolls whose positions carry the same (colour, 5040 batch) — even an exact
colour match is a one-third changeover if the 5040 batch differs — so a
run can only be so long, and far fewer candidate sequences are worth
considering.

### 3.2 Cost simplification: bobbins changed (pending floor confirmation)

A tiered cost model (separate rates for moving a hanging bobbin vs
fetching and mounting a new one, plus takedown) was considered and set
aside. The working position from planning discussion is that **moving an
existing bobbin to another creel location costs the same as removing the
old bobbin and mounting a new one** — either way the yarn is cut and the
new bobbin's yarn is tied to the yarn feeding the tufter. Under that
equality the tiers collapse and the cost of a transition is simply the
number of bobbins changed. An inch of roll width carries 3 bobbins per
yarn type — 9 in total (§6) — and the per-yarn-type batch rule (§3.1)
gives each inch position three possible states in a transition:

```
per inch position, cost(A → B) =
  9 bobbins   colour differs                    (all three yarn types change)
  3 bobbins   colour same, 5040 batch differs   (only the 5040 XP+ bobbins)
  0 bobbins   colour and 5040 batch both match
```

This is the existing Phase 1 cost model with two adjustments, only one of
which is substantive:

- per-inch mismatch is two-tier — weight 3 for a colour change, weight 1
  for a 5040-batch-only change — instead of binary;
- the remaining ×3 is a constant scale factor: it changes time estimates
  but never which sequence wins, so the optimiser can keep working in
  inches, with a colour-changed inch costing 1 and a batch-only-changed
  inch costing ⅓.

Within a single order the batch tier never fires (each item has exactly
one batch), so Phase 1 behaviour — and everything already built — is
unchanged: the distance matrix, Held–Karp, 2-opt/Or-opt, symmetry (both
tiers are symmetric), and the MST lower bound all survive. Combined mode
needs 5040-batch-aware layout signatures and the tiered per-inch weight.

One assumption carries this simplification and must be verified with the
floor (questions 1–3 in §9):

- **Move ≈ remove-plus-mount in time.** If moving turns out much faster,
  the tiered model returns.

A second assumption originally listed here — no material fixed overhead
per stop beyond the bobbin work itself — has been overtaken by decision:
the model carries no per-stop penalty regardless (§3.3).

### 3.3 Objective: long manufacturing runs

The stated business objective (CEO direction) is that tufting should be a
long-running process: minimise the number of stops, so runs between creel
changes are as long as possible. This is the throughput goal of §1 stated
as a model requirement — downtime is where throughput is lost, and stops
are where downtime happens.

A structural fact shapes where this can be won. **Within a single order,
stop count is invariant**: once identical layouts are clustered (which the
sequencer already does), every sequence has exactly (distinct layouts − 1)
stops. Sequencing inside an order reduces how many bobbins each stop
changes, never how many stops there are. Long runs are therefore won at
the **pool level**:

- batch sharing plus positional layout alignment creating zero-change
  seams between orders (§3.4),
- at a later stage, pushing extrusion for larger batch sizes of items
  that are used a lot, so one batch can meet the requirements of several
  orders combined and create more sharing opportunities, and
- longer term, the amount of layout commonality in the order book itself —
  set upstream by field design, which scheduling can only harvest, not
  create.

**Decision: no fixed per-stop penalty.** A fixed stopping cost was
considered and set aside. Minimising bobbins changed already favours not
stopping: a zero-change transition costs nothing, so when a batch meets
the requirements of two orders combined and their roll layouts for that
item are similar, the optimiser will chase exactly that seam — a fixed
penalty would re-express a preference the objective already has. The plan
doc's original no-stoppage-penalty assumption therefore extends across
orders unchanged. For the record, the variant that was set aside was
`cost = STOP_PENALTY (if any mismatch) + bobbins changed` — a
lexicographic "stops, then bobbins" objective that preserves the TSP
structure (a constant added to nonzero edges) and is inert within a
single order; it can be revisited if evidence ever demands it, and a
measured per-stop overhead can still refine absolute downtime
*predictions* without changing which sequence wins.

Reporting implication: evaluation should report the **run-length
distribution** (linear feet between stops) and stop count alongside the
existing inch/bobbin cost, since run length is the quantity the business
objective is stated in.

### 3.4 Where batch sharing pays: aligned seams

At a seam between orders the saving is binary before it is linear. If the
shared item sits at the **same creel positions** in both orders' rolls and
comes from the shared 5040 batch, the seam costs zero and the machine
keeps running — the actual prize. Any mismatch means a stop regardless;
sharing then only reduces the number of bobbins changed at that stop.

The per-yarn-type batch rule (§3.1) makes colour-matched seams cheap even
*without* sharing. Worked example: a roll of 182" field green followed by
the same layout from another order with a different 5040 batch changes
only the 5040 bobbins — 182 × 3 = 546 instead of 182 × 9 = 1,638; the
MF TXT 7200/10 and SXT 5400/6 bobbins stay hanging. Sharing the 5040
batch removes the remaining third and takes the seam to zero. So layout
alignment alone earns two-thirds of the saving, and batch sharing earns
the rest — plus the run-through (no stop at all).

A seam does not have to sit at the end of an order. The pool is sequenced
at the **roll level**: orders are broken down into their rolls, and the
one-batch-per-item-per-order rule is a constraint on those rolls, not an
instruction to finish one order before starting the next. In practice the
constraint pulls an order's rolls together — they share (colour,
5040 batch) identity at their positions, so transitions among them are the cheap
ones — but contiguity is an emergent outcome, not a hard rule. Rolls of
different orders may interleave wherever that changes fewer bobbins; an
order's rolls will most likely end up sequential, or at least in close
proximity, without being forced to be.

Roll-level pooling requires every roll to carry its **order identity**,
and the `navision_lot` number already encodes it: the first part is
shared by every roll of the order and the second part distinguishes the
rolls — M2603558-03, M2603558-04, M2603558-05, M2603558-06 are four
rolls of one order. The pool can therefore derive each roll's order id
from its lot prefix and cross-check it against the workbook the roll
came from (PO number / source file); extraction should verify that all
rolls in a workbook share one prefix and flag any exception. That link
is what makes the
one-batch-per-item-per-order rule checkable per roll: a roll's demand for
an item is served by the batch assigned to (its order, that item), so all
of an order's rolls tuft the item from the same batch no matter where in
the pooled sequence they land, and no matter which rolls of other orders
sit between them.

Sharing a batch between two orders therefore only makes sense when their
roll layouts align positionally (or overlap substantially — a long common
stretch such as 177" of field green at identical positions stays untouched
even if 5" at the edge changes). The pairing metric is the **best seam
cost**: the cheapest transition between any roll of order A and any roll of
order B under shared-batch identity — precisely the saving on offer,
wherever in the pooled sequence that seam ends up.

Candidate pairs are found by cheap filters using data the extractor
already produces. Two filters matter, and they are really two faces of
one measure:

1. **Layout alignment** — shared layout signatures, or low positional
   mismatch between the best roll pair.
2. **Batch feasibility** — for the items at the aligned positions: lbs
   with margin and the per-bobbin checks of §4–§7, evaluated against the
   planned manufacturing order of the two orders (§7).

They combine because the (colour, 5040 batch) combination is what
determines the changeover cost, and that cost is computed from the layout
alignment — the inches that change, weighted per the two tiers of §3.2. **Spec compatibility (product, gauge,
pile height) is not a gate**: a spec change is a very short stop made on
the tufting machine itself, not creel work, so it does not disqualify a
pair. This refines the Stage 1 plan's expectation (its assumption 4) that
gauge and pile height would matter for cross-order grouping.

Only pairs surviving both filters become sharing candidates; everything
else keeps its own batch. Pairing couples order B's schedule to order A's
progress through the shared batch, so the pair list is re-confirmed at
each planning cycle rather than standing indefinitely.

Notes retained from the tiered-model discussion, still relevant:

- **Kitting** (fetching new bobbins in advance and staging them
  creel-side) no longer affects sequencing under the simplified model, but
  still matters for absolute changeover-time predictions. Whether it
  already happens informally is a data question (§9).
- **Staffing is not modelled.** How many people work a creel change is
  considered nowhere in the model: the objective is the optimal
  manufacturing ordering, and bobbins changed measures the changeover
  work a transition creates. Wall-clock downtime per stop depends on
  staffing, but staffing does not change which ordering is optimal.

### 3.5 Multiple tufting stations and the starting state

Scheduling is not for a single machine. A few tufting stations are
available, and the pool schedule must decide **which station tufts which
rolls** as well as the sequence on each station.

Each station also enters the planning window with a known state: the last
roll of the previous week's schedule is still its current threading. The
transition from that last roll to the first roll of the new plan is a real
changeover, costed exactly like any other transition — so each station's
sequence is seeded from its known start rather than a fresh start. This
specifies what the Stage 1 plan deferred (its assumption 7: fresh start
within one order; known-start seeding left to the cross-order phase).

Practical station facts — how many stations there are, whether their
creels are identical, and whether hanging bobbins can move between
stations — are still to be collected (§9, §10).

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

The bind is on the 5040 XP+ items. For the two batch-insensitive yarn
types (§3.1) the spare pool spans *every* batch of the item — swaps and
splices may draw from any of them — so their feasibility is usually
settled by the aggregate checks alone; the per-bobbin matching above is
what constrains the 5040 assignment.

## 5. Rules confirmed from the floor

Two rules shape how the per-bobbin problem is handled:

1. **Within-batch splicing is allowed; cross-batch is not — for the
   5040 XP+ (6Pin) yarn.** A new bobbin from the *same batch* can be tied
   in; a 5040 bobbin from a different batch cannot. The two
   batch-insensitive yarn types (§3.1) may splice across batches freely.
   So a single bobbin need not cover a position's full demand, as long as
   spares exist for planned swaps — same-batch spares for 5040, any batch
   for the other two.
2. **Bobbins are never allowed to run dry mid-roll.** Replacement is
   proactive. Since bobbins are not tracked today, this is currently
   managed by operator judgement.

Consequence: what is needed is per-bobbin **prediction**, not shop-floor
tracking. Feasibility for a batch becomes: total lbs (with buffer) +
width-coverage bobbins hanging + enough same-batch spares for the planned
swaps.

Rule 1 bounds splicing by *batch*, not by order. Whether within-batch
splicing should also cross order boundaries when a batch is shared is a
planning refinement, not a floor rule — see the splicing-aware pairing
policy in §7.

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

That end state is what planned sharing needs. Two pairing policies exist,
a conservative one and a splicing-aware one.

**Conservative: partition and discard.** Partition the batch's bobbins
between the paired orders before tufting starts — designate the bobbin set
the first order will consume, use its partials within that order where
possible, and discard remaining partials so the second order's requirement
is guaranteed in **full** bobbins (the only kind countable without
weighing). Sharing feasibility then reduces to a static check:

```
predicted full bobbins remaining ≥ paired order's bobbin requirement
AND remaining lbs ≥ paired order's lbs requirement
```

**Splicing-aware (preferred direction, to be confirmed).** Splicing is
allowed within a batch (§5 rule 1), and there is no obvious reason that
should stop at an order boundary when the batch is shared: an untouched
"brand new" bobbin from the same batch is itself a changeover — it still
has to be mounted on the creel to be used — so insisting the second order
start on fresh bobbins saves nothing over splicing in the first order's
partials. Under this policy, feasibility is checked against the planned
manufacturing order of the two orders: simulate the first order's
consumption (§6), take the predicted remainders — partials included — and
run the per-position matching check of §4 for the second order, with
same-batch spares covering the planned swaps (any-batch spares for the
two batch-insensitive yarn types, §3.1). This check is
sequence-dependent where the conservative one is not: whether enough is
left depends on which order runs first, and the batch assignment must be
re-checked if the planned sequence changes.

Which policy to trust hinges on how accurate the per-bobbin predictions
prove to be (§9 item 10): full bobbins can be counted without weighing;
partial remainders can only be predicted.

Refinements:

1. **Use partials before discarding.** Partials can feed the same order's
   later rolls at short positions, or be spliced in within-batch — and,
   under the splicing-aware policy, feed the paired order. Discard is the
   fallback, not the default.
2. **Discards are a cost the optimiser must see.** Pairing an order onto a
   leftover batch saves a creel change but may cost N lbs of discarded
   partials; the trade is weighed, not assumed.

### 7.1 Future project (noted, out of scope): numbered creel map

A later project (noted July 2026) will map every inch of roll width to
physical bobbin numbers on the creel — e.g. the first inch at the front
of the tufting machine is bobbins 1–9 — and record which numbered creel
locations hold which yarn type.

This is a presentation-layer translation, not a model change: the
pipeline already computes everything per inch position from the front of
the machine (the cost model's mismatch, the depletion groups, the swap
points), so once the numbering scheme exists, inch positions translate
directly into named bobbins. The payoff is a schedule whose changeover
and swap instructions are exact — "change bobbins 511–537", not "re-thread
inches 57–65" — making creel changes unambiguous to execute and auditable
afterwards. Prerequisite: the numbering scheme per station, which ties
into the station facts of §3.5 (whether creels are identical across
tufters).

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
4. How much of a batch is typically left over after an order, and its
   composition — mostly untouched full bobbins, or mostly semi-used
   partials (this decides how much the conservative full-bobbins-only
   pairing policy of §7 gives away).
5. How often orders are mixed today (leftover of one order's batch used to
   tuft another order's rolls), and whether that is usually one item or a
   mix.
6. Standard new-bobbin net yarn weight per yarn type, and its bobbin-to-
   bobbin variation.
7. The swap margin: how much yarn must remain on a bobbin before an
   operator will start the next roll on it.
8. What happens to bobbins taken off the creel today — where they are
   stored, whether they are often put back on or discarded, and whether
   partials are kept creel-side or returned to inventory (quantifies the
   waste the simulator would reduce).
9. A few whole-changeover timings (total minutes, inches changed, bobbins
   moved vs new) to validate the linear cost model.
10. Leftover accuracy: for several recent orders, computed leftover vs
    actual measured leftover per batch.
11. Batch data shape from Business Central: per batch — item number, batch
    id, available lb, bobbin count, receipt date; whether bobbin count is
    tracked per batch; export vs live query, and refresh frequency.
12. Station facts (§3.5): how many tufting stations are available and how
    many of them run Pivot, whether their creels are identical, and
    whether hanging bobbins can move between stations.
13. How removed bobbins are identified when re-creeling — whether anyone
    tracks, even informally, which bobbins have how much yarn left. §5
    assumes bobbins are not tracked today; this checks that assumption
    and tells us what the printed swap plan must replace.
14. Whether a bobbin that runs dry can be tied over smoothly — one cone's
    end tied to the next cone's beginning so the transition happens
    without stopping. If so, planned run-dry tie-overs become an
    alternative to the proactive swaps of §5 rule 2.

Priority if limited: items 1–3 (they verify the assumption the simplified
cost model rests on, §3.2) and item 10 (leftover accuracy — the go/no-go
evidence for planned sharing and for the splicing-aware policy, §7).

Three earlier asks are closed and kept for the record:

- *How many people work a creel change* — dropped: staffing is not
  modelled (§3.4); the objective is the optimal manufacturing ordering.
- *Fixed time lost per stop beyond the bobbin changes* — resolved by
  decision: the cost model carries no per-stop penalty (§3.3). A measured
  per-stop overhead would still refine absolute downtime predictions, but
  it no longer decides the model.
- *Whether all three yarn types are equally batch-sensitive* — answered
  (July 2026): only 5040 XP+ (6Pin) cannot mix batches; MF TXT 7200/10
  and SXT 5400/6 can. Folded into the identity and cost model as the
  tiered per-inch weight (§3.1–§3.2).

## 10. Open questions

1. Length of the frozen planning window.
2. Floor confirmation of the assumption behind the simplified cost model
   (§3.2): move ≈ remove-plus-mount timing. (The per-stop penalty question
   is resolved — no fixed stopping cost, §3.3.)
3. Weighing the secondary concerns in batch assignment. The primary
   objective is decided (§3.1): the optimiser assigns batches to
   (order, item) pairs so the pooled schedule needs the least number of
   bobbin changes — maximal sharing where roll layouts align. Still open
   is how batch fragmentation and discarded-partial waste weigh against a
   marginal bobbin saving (sharpens open question 2 of
   `docs/batch_assignment_context.md` §7).
4. Whether combined-mode "one batch per item" is enforced per original
   order or across a combined run (carried over from
   `docs/batch_assignment_context.md` §7).
5. How partial-bobbin discards are recorded so actual waste can be compared
   against the simulator's prediction.
6. Whether the splicing-aware pairing policy (§7) is adopted, and the
   accuracy the per-bobbin predictions must reach to justify it
   (§9 item 10).
7. Station assignment (§3.5): how rolls are allocated across the available
   tufting stations, and how each station's seeded start state interacts
   with the pooled sequence.

## 11. First step — implemented

The per-item weight data and a first cut of the consumption model are
implemented:

- `data/item_bobbin_data.csv` maps each item number to its yarn type,
  colour code, weight in lb per square foot and fresh bobbin weight from
  extrusion. It is edited directly on GitHub; it is seeded with item
  121051 (5040 XP+ (6Pin), FG, 0.04831 lb/sqft) and the remaining items
  and fresh bobbin weights are to be filled in. Once populated across all
  item numbers it doubles as the item catalogue of §3.1 — the yarn-type
  column is what identifies the batch-sensitive 5040 XP+ items.
- `bobbin_usage.py` computes, per matched item, each roll's consumption per
  bobbin (`w × length_ft / 36`, §6) and the depletion along the optimised
  sequence, tracked per inch position from the front of the machine (fixed
  creel alignment) and reported as depletion groups — one entry per set of
  positions with an identical coverage history. When the fresh bobbin
  weight is filled in it also places planned swap points (zero margin for
  now; the swap margin from §9 item 7 becomes a parameter later) and
  counts the fresh bobbins each group consumes.
- The Phase 4 report carries this under a `bobbin_usage` key, the web app
  shows an "Item bobbin usage" card, and the downloadable run-sheet PDF
  gains a matching section with red BOBBIN SWAP bands alongside the
  existing SETUP CHANGE bands.

Since then, the batch ledger is implemented (`batch_ledger.py`, July 2026):

- The batch-availability side has an interim shape: a batch inventory
  workbook (`batch_number`, `item_number`, `number_of_bobbins`,
  `weight_per_bobbin`, `total_batch_weight`), uploaded in the app or passed
  via `--batches`, standing in for the Business Central feed (§9 item 11)
  until that connection is decided.
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
connection, the sharing feasibility pipeline across orders (§3.4, §7),
5040-batch-aware layout signatures with the tiered per-inch cost in
combined mode and each roll tagged by its source order (§3.1–§3.2, §3.4),
relaxing the ledger's one-batch rule for the two batch-insensitive yarn
types (§3.1 — `batch_ledger.py` currently enforces it for all items,
which is stricter than needed for MF TXT 7200/10 and SXT 5400/6), and
multi-station scheduling seeded from each tufting station's last roll
(§3.5).
