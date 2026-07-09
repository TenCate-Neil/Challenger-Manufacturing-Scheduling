# Batch Assignment — Domain Context

Status: **context captured, not yet implemented**. Batch assignment is done
manually by the planners today. This document records the agreed domain model
so a later phase can build on it without re-deriving the rules.

## 1. Product structure

The majority product is the **Pivot** product. It is always tufted from three
yarn types simultaneously:

- 5040 XP+ (6Pin)
- MF TXT 7200/10
- SXT 5400/6

The three yarns are wound together: **every inch of a roll contains all three
yarn types**. A colour segment in the roll layout therefore implies that
colour in all three yarn types at those positions.

## 2. Creel geometry

The creel is a long square prism, with the square face closest to and feeding
the tufting machine. Bobbins (packages of wound yarn) hang at multiple spots
along it.

- **Vertical axis = yarn type.** The top section holds one of the three yarn
  types; the bottom section holds the other two.
- **Length axis = colour**, mirroring the roll layout left to right. For a
  roll of 5" white + 177" field green, the first stretch of creel carries
  white bobbins in all three yarn types (top and bottom), and the remaining
  long stretch carries field green bobbins in all three yarn types.

## 3. Items

A specific **yarn type + colour combination is an item** with a unique item
number (e.g. 5040 XP+ (6Pin) in field green `FG` = item 121051).

In the order workbook, the **SKU values in the Yarn SKUs block (rows
671–676) are these item numbers**. Because each colour segment implies all
three yarn types, one segment maps to exactly three items; the colour code
joins the roll layout table to the Yarn SKUs table. The full item demand of
an order is therefore derivable from data the extractor already produces.

## 4. Batches and the assignment rule

A **batch** is an inventory batch of an item received from the extrusion
manufacturer. Within a single order, **an item must be assigned exactly one
batch — never split across two**: extrusion runs of the same item are not
always identical, and mixing batches produces visible differences in the
finished field.

The assigned batch must satisfy two independent constraints. Together they
cover the width/length trade-off — bobbins cover breadth, pounds cover depth
(a narrow stripe on very long rolls needs few bobbins but a lot of yarn):

### 4.1 Weight (lb)

The batch must contain at least the pounds of yarn the order consumes for
that item. Consumption is driven by the **square feet of that item across
the order** (each roll contributes its item width × roll length), so it
combines width and length.

The required pounds per yarn are **already calculated inside the order
workbook, in rows 638–645**. The extractor does not currently read this
block; a later phase should extract it rather than recompute it.

### 4.2 Bobbins

The batch must contain at least:

```
bobbins_required(item) = max_width_in(item) × 3
```

where `max_width_in(item)` is the maximum, over all rolls in the order, of
the **total width in inches of that item's colour within a roll**. If the
same colour appears in two separate segments of one roll (e.g. 5" + 5"),
their widths are **added** (10") — split or adjacent, that full width must
be covered by bobbins simultaneously.

Worked example: three rolls contain item 121051 at widths 5", 3", 8" —
the requirement is 8 × 3 = **at least 24 bobbins**. Roll length is
irrelevant to this constraint.

## 5. Relationship to sequencing

Batch assignment is independent of roll sequencing: both the per-item square
footage and the per-item maximum width are properties of the order as a set
of rolls, unchanged by the manufacturing order. The existing optimisation
pipeline is unaffected.

## 6. Planned phase: order pool and automated assignment

The intended next phase moves from one workbook at a time to the full set of
open orders, so that batch assignment is computed across all of them at once
instead of planners opening documents and doing the calculations by hand.

Two inputs feed it:

1. **Order pool.** All open-order Excel workbooks live in one shared,
   centralized place — a local folder, OneDrive, SharePoint, or similar. The
   existing extractor runs over every workbook in the pool.
2. **Batch availability from Business Central.** The batches currently in
   inventory per item, with each batch's available lb and bobbin count —
   either via a live connection to Business Central or a recurring export
   from it.

With both sides known, the system computes each open order's per-item
requirements (required lb and required bobbins, per sections 4.1 and 4.2)
and **optimally assigns available batches to the items within the open
orders**, respecting the one-batch-per-item-per-order rule and both capacity
constraints. The goal is to automate the current manual procedure end to
end: no opening documents, no hand calculations, and a faster path from
open orders to assigned batches.

## 7. Open questions for the implementation phase

1. **Batch data shape.** Whether the Business Central side is a live
   connection or an export, and the exact fields available per batch
   (item number, batch id, available lb, bobbin count), is still to be
   decided.
2. **Assignment objective.** "Optimally assign" needs a defined objective
   when several feasible assignments exist — e.g. minimise leftover batch
   remnants, preserve large batches for large orders, or first-fit by ship
   date. To be agreed with the planners.
3. **Combined mode semantics.** When several orders are joined into one run,
   is "one batch per item" enforced per original order or across the whole
   combined run?

## 8. Likely first step

A per-item demand calculator computed straight from an extraction: item
number, yarn type, colour, total square feet, required lb (extracted from
rows 638–645), and required bobbins (max total width × 3). Planners can
sanity-check its output against their current manual process before any
batch-assignment automation is built on top.
