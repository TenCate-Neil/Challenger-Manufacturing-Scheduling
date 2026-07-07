# Roll Sequencing Optimisation — Planning Document

Status: **draft for review**. No development starts until the logic and
assumptions below are agreed.

## 1. The challenge

The extraction step produces the roll layout data for a single customer order
(one Excel file). Within that order we want to decide the sequence in which the
rolls are manufactured on the tufting machine so that the total setup change
effort is as low as reasonably achievable.

Reordering must not change what is produced. Every roll and every quantity from
the original order is preserved; only the order of manufacture changes.

Combining multiple orders into one sequence is explicitly out of scope for this
phase.

## 2. Context: how a roll is described

Each roll is defined left to right from the front of the machine using the DIM
columns. Each DIM segment carries a width (inches) and a colour/type code
(e.g. `FG`, `WHI`). A roll therefore occupies a fixed width — commonly 182" —
made up of one or more coloured segments.

Examples:

- Roll A: 182" `FG`.
- Roll B: 177" `FG` + 5" `WHI`.

## 3. How setup change cost is understood

We treat each roll layout as a positional profile across the roll width: for
every inch position `x` from the front, the profile records which colour/type
is threaded at that position.

The setup change cost between two consecutive rolls is the number of inch
positions whose colour/type differs between the two profiles:

```
cost(A, B) = count of positions x where profile_A(x) != profile_B(x)
```

For the example above, A → B changes only the final 5 inches (`FG` → `WHI`), so
the cost is **5 inches**. Positions that already carry the same colour/type need
no change and cost nothing.

Two useful consequences:

- The cost is **symmetric**: `cost(A, B) == cost(B, A)`.
- Two identical layouts have a cost of **0** — they should always be produced
  back to back.

The cost of a full sequence is the sum of the costs of each adjacent pair. Cost
depends only on neighbouring rolls, not on earlier history.

Because each creel sits at a fixed location and the gauge is constant across an
order, an inch position maps to a fixed set of creel ends. Changing the
colour/type at a position means re-threading those ends. Re-threading is roughly
fixed time per inch, so total inches changed across the sequence is proportional
to total changeover time. We therefore minimise total inches changed and add
**no** fixed per-stoppage penalty: a single large change is not disproportionately
worse than several small ones, and minimising total inches already minimises the
whole changeover time.

## 4. Problem shape and proposed approach

Reordering rolls to minimise the sum of adjacent setup costs is a
minimum-cost path through all rolls where the "distance" between any two rolls
is the positional inch mismatch above. This is the path form of the symmetric
Travelling Salesman Problem. Finding the guaranteed optimum is NP-hard in
general, so we plan a tiered approach and are honest that large orders will be
solved to *near*-optimal rather than provably optimal.

**Step 1 — Collapse identical layouts.**
Group rolls that share an exact layout signature. Within a group the internal
order is free (cost 0), so we sequence the *distinct* layouts and expand each
back to its full quantity at the end. This usually shrinks the problem from
"number of rolls" to a much smaller "number of distinct layouts", which is what
makes exact solving feasible for many orders.

**Step 2 — Build the distance graph.**
Compute the pairwise setup cost between every pair of distinct layouts.

**Step 3 — Sequence the layouts.**

- Small orders (few distinct layouts): solve exactly with a Held–Karp dynamic
  program, which guarantees the minimum-cost sequence.
- Larger orders: construct a starting sequence with a greedy nearest-neighbour
  pass, then improve it with local search (2-opt / Or-opt). These are standard,
  well-understood methods and behave reliably at this scale.

**Step 4 — Expand and emit.**
Replace each distinct layout with its physical rolls and quantities to produce
the final manufacturing sequence.

## 5. Grouping identical and similar layouts

Identical layouts are handled exactly by Step 1 above.

For *similar* (not identical) layouts, we do not need a separate clustering
stage to get good results — the distance-minimising sequence already tends to
place similar layouts next to each other, because that is what lowers cost.
Optional clustering of near-identical layouts can still be added later as a way
to seed the construction heuristic or to make the output easier to read. We
propose to treat it as an optional accelerator, not a core requirement.

## 6. How the result will be evaluated

The order in which rolls appear in the Excel file is assigned by sales with no
attention paid to manufacturing sequence, so it is not a meaningful baseline. We
do **not** compare against it. The optimised sequence is evaluated on its own
merits:

1. **Conservation.** The set of rolls, their quantities, and totals (linear
   feet, square feet) in the optimised sequence must exactly match the order as
   extracted. Nothing is added, dropped, or altered. Each roll's own layout is
   never modified — we only reorder.
2. **Achieved cost.** We report the total setup change cost (inches re-threaded)
   of the optimised sequence in absolute terms.
3. **Solution quality.** For small orders solved exactly, the result is the
   proven minimum. For larger orders solved heuristically, we report the gap
   between the heuristic cost and a computed lower bound (and, where feasible,
   against the exact optimum on small instances) so the quality of the sequence
   can be judged rather than taken on trust.
4. **Transition breakdown.** We report how many transitions are zero-cost
   (identical consecutive rolls) and the distribution of the remaining
   transition costs, so the result can be inspected.

## 7. Assumptions

These are the confirmed assumptions the plan rests on.

1. Optimisation is within a single order only (current scope).
2. **Fixed orientation.** The tufting machine has a fixed orientation and
   direction of tufting, which affects the finished product. Rolls are never
   flipped or reversed.
3. **Fixed widths and creel locations.** Roll width is constant across the order
   and each creel sits at a fixed location, so layouts always align positionally
   from the front of the machine.
4. **Cost unit is inches.** Cost is the positional inch mismatch — symmetric,
   additive over adjacent pairs only, and linear in inches (no fixed
   per-stoppage penalty). Gauge and pile height are identical for all rolls in a
   single order, so they do not affect within-order sequencing; they become
   relevant only for grouping across orders in a later phase.
5. The colour/type code at a position fully identifies what is threaded there.

## 8. Remaining open question

- **Known start state.** We currently assume there is no fixed starting machine
  state — the sequence is free to start anywhere. If the machine's current
  threading is known at planning time, the first transition should be costed
  from it, which is a small extension (fix the first node). Please confirm
  whether the start state is known; otherwise we proceed with a free start.

## 9. Proposed phases

- **Phase 0 (this document):** agree logic and assumptions.
- **Phase 1:** build the cost model and a sequence scorer; verify the cost
  function reproduces the 5-inch example.
- **Phase 2:** collapse duplicates and build the distance graph.
- **Phase 3:** sequencing engine (exact for small orders, heuristic for large).
- **Phase 4:** evaluation and reporting (conservation, before/after cost,
  transition breakdown); emit the optimised sequence.
- **Phase 5 (out of scope now):** combining multiple orders.
