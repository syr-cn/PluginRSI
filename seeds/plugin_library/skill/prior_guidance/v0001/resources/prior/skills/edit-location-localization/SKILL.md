---
name: edit-location-localization
description: Identify concrete line regions where a minimal repair could change the faulty mechanism.
---

# Edit-location Localization

**Internal ID:** `S09`  
**Category:** `localization`  
**Tier:** `core`  
**Output:** `EditLocations`

## Activate when

- The workflow reaches the `localization` stage and the required inputs are present.

## Applicable roles

- Solver
- Localizer
- Planner

## Required inputs

- `ranked_symbols`
- `diagnosis`
- `source_context`

## Optional inputs

- `public_contracts`

## Procedure

1. Locate the branch, state update, conversion, or contract check implicated by the diagnosis.
2. Distinguish primary edit points from supporting reads.
3. Estimate whether one location is sufficient or synchronized edits are required.
4. Record invariants and nearby tests for each edit region.

## Verification

- Each location is tied to the root-cause mechanism.
- Supporting files are not mislabeled as edit targets.

## Stop conditions

- One minimal edit set is identified or localization is declared inconclusive.

## Constraints

- Do not select locations solely because they mention the issue term.

## Allowed tools

- `read_range`
- `symbol_search`

## Provenance

Synthesized from design patterns in `agentless`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.