# Manufacturing Scheduling — Documentation Overview

This is the entry point to the project's planning and domain documentation.
It states the objective the whole project serves, sketches the end-to-end
picture, and maps the documents: what each one is for, who it is for, and
how they depend on each other. The root `README.md` is the separate,
code-level reference (how to run the tools, what each module does).

## 1. The objective

The goal is to **increase manufacturing throughput**. The tufting machine
only produces while it runs, and the dominant loss is **downtime**: the
stops for creel changes, and the number of bobbins changed at each stop.
Every phase of this project — sequencing rolls within an order, assigning
batches, planning bobbin swaps, sharing leftover batches across orders —
is a way of reducing that downtime.

Optimising the usage of yarn (leftover batches, partial bobbins) is
beneficial and falls out of the same planning, but it is a secondary
benefit, not the main goal. Where the two pull in different directions,
throughput wins; the trade-off is made visible rather than assumed (see
the leftover-batch document §7 on discards).

Sharing batches across orders is the primary way scheduling reduces
downtime. A later-stage complement sits upstream of scheduling: pushing
extrusion for larger batch sizes of items that are used a lot, so one
batch can span more orders (leftover-batch document §2, §3.3).

## 2. The floor reality this project answers

Two facts about current practice shape the whole design:

1. **Schedules are not followed on the floor today.** Operators jump
   between orders based on what is already hanging on the creel, picking
   whichever rolls need the fewest bobbin changes next. Their intent is
   exactly the project's objective — minimise downtime — but judging
   bobbin-change counts across the open order book by eye is close to
   impossible, so the result is approximate and the planned schedule and
   the actual schedule diverge. Every jump also rewrites batch
   assignments and makes progress hard to track.
2. **Planning is manual.** Batch assignment is done by planners opening
   workbooks and calculating by hand, with informal buffers.

The project's answer is to move that same optimisation from the floor to
planning time: compute the sequence, the batch assignments, and the
cross-order transitions that genuinely minimise downtime, and freeze them
into a schedule that is both better than eyeballing and stable enough to
follow.

## 3. The end-to-end picture

The pipeline, in dependency order, with implementation status:

| Stage | What it does | Where specified | Status |
|---|---|---|---|
| Extraction | Reads order workbooks into structured JSON (rolls, layouts, yarn lbs, SKUs) | root `README.md` | Implemented (`extract_turf_layout.py`) |
| Within-order sequencing | Orders one order's rolls to minimise setup change (inches re-threaded) | `optimisation_plan_Stage1.md` | Implemented through Phase 5 (`roll_sequencing.py`, `sequencer.py`, `evaluate.py`, `app.py`) |
| Item requirements | Per item: required lb and bobbins for an order | `batch_assignment_context.md` §4, §8 | Implemented (`item_requirements.py`) |
| Bobbin usage simulation | Per-bobbin depletion along the optimised sequence; planned swap points | `leftover_batch_utilisation_and_bobbin_planning.md` §6–§7, §11 | Implemented first cut (`bobbin_usage.py`, `data/item_bobbin_data.csv`) |
| Batch ledger | Assigns one batch per item from a batch inventory workbook; simulates the run and predicts the leftover end state | `leftover_batch_utilisation_and_bobbin_planning.md` §11 | Implemented interim shape (`batch_ledger.py`) |
| Order pool + batch availability | All open orders in one place; batch inventory from Business Central | `batch_assignment_context.md` §6 | Not built |
| Cross-order scheduling with planned batch sharing | Roll-level pooled sequencing on (colour, 5040 batch) identity with a tiered per-inch cost, each roll tagged with its order; batch assignment is chosen by the optimiser to minimise total bobbin changes (maximal sharing); multiple tufting stations each seeded from its last roll of the previous week | `leftover_batch_utilisation_and_bobbin_planning.md` §2–§3 | Not built — the current phase's brief |

## 4. What identifies a threaded position

Three levels of identity matter, and each document works at the level its
scope allows:

- **Colour code** (e.g. `FG`, `WHI`) — sufficient *within a single
  order*: every colour segment implies all three yarn types, and each
  item in an order carries exactly one batch, so colour fully determines
  what is threaded. Within-order sequencing works at this level.
- **Item number** — the yarn type + colour combination (e.g. 121051).
  This is the unit of demand, inventory, and batch assignment. Colour
  match alone is not sufficient across orders; the item numbers must
  match.
- **(colour, 5040 batch)** — the full cross-order identity. Only the
  5040 XP+ (6Pin) yarn is batch-sensitive; MF TXT 7200/10 and SXT 5400/6
  may mix across batches. A colour-matched position whose 5040 batch
  differs changes a third of its bobbins (just the 5040 ones); sharing
  the 5040 batch takes it to zero. Cross-order sequencing and batch
  sharing work at this level.

## 5. Document map

| Document | Purpose | Audience | Scope |
|---|---|---|---|
| `docs/README.md` (this file) | Objective, end-to-end picture, document map | Everyone; read first | Orientation only — no rules or specs live here |
| `docs/optimisation_plan_Stage1.md` | Agreed logic, cost model, and assumptions for within-order roll sequencing | Developers on the sequencing pipeline; planners checking assumptions | Single order only; implemented through Phase 5 |
| `docs/batch_assignment_context.md` | The domain model: products, creel geometry, items, batches, assignment rules | Everyone — the reference the other documents cite | Domain facts and per-order requirements; the root reference, depends on nothing |
| `docs/leftover_batch_utilisation_and_bobbin_planning.md` | Working brief for the current phase: cross-order batch sharing, per-bobbin planning, cost-model extensions | Planners agreeing the approach; developers building the order-pool phase | Everything downstream of within-order sequencing |
| root `README.md` | Code-level reference: usage, module-by-module behaviour | Developers and users running the tools | Follows the implementation; updated as code lands |

Dependencies between the documents:

- `batch_assignment_context.md` is the root: it depends on nothing and is
  cited by both other documents for domain rules (items §3, the two batch
  constraints §4, the planned order-pool phase §6).
- `optimisation_plan_Stage1.md` depends only on the extractor output. Its
  cost model (§3) is what the leftover-batch document generalises.
- `leftover_batch_utilisation_and_bobbin_planning.md` builds on both: it
  extends the batch model of `batch_assignment_context.md` (§4 → per-bobbin
  feasibility) and the cost model of `optimisation_plan_Stage1.md`
  (colour identity → (colour, 5040 batch) identity). Its open questions §10
  carry forward and sharpen `batch_assignment_context.md` §7.

Suggested reading order for someone new: this file →
`batch_assignment_context.md` → `optimisation_plan_Stage1.md` →
`leftover_batch_utilisation_and_bobbin_planning.md`.

## 6. Conventions

- Each planning document opens with a **Purpose / Audience / Scope /
  Depends on / Status** block.
- Cross-references use the repo-relative path plus section number
  (e.g. `docs/batch_assignment_context.md` §4.2) so they can be followed
  from anywhere in the repo, including code docstrings.
- Each rule lives in exactly one document; other documents cite it rather
  than restate it. Planning documents record *agreed logic*; the root
  `README.md` records *implemented behaviour*; "first step — implemented"
  sections in the planning documents bridge the two.
