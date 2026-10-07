---
name: symbol-localization
description: Rank functions, classes, methods, or configuration blocks inside selected files.
---

# Symbol Localization

**Internal ID:** `S08`  
**Category:** `localization`  
**Tier:** `core`  
**Output:** `RankedSymbols`

## Activate when

- The workflow reaches the `localization` stage and the required inputs are present.

## Applicable roles

- Solver
- Localizer

## Required inputs

- `ranked_files`
- `issue_profile`

## Optional inputs

- `call_graph`
- `reproduction_trace`

## Procedure

1. Identify definitions and direct callers connected to the symptom.
2. Check state transformations, branches, validation, and error handling.
3. Trace inputs and outputs across the shortest causal path.
4. Rank symbols with expected failure mechanism and discriminating evidence.

## Verification

- The top symbol hypothesis is falsifiable.
- Relevant callers or callees are included when needed.

## Stop conditions

- A small set of symbols supports a reproduction or code-level check.

## Constraints

- Do not read every symbol in a file.
- Do not confuse frequently referenced code with faulty code.

## Allowed tools

- `symbol_search`
- `read_range`
- `text_search`

## Provenance

Synthesized from design patterns in `agentless`, `aider`.
This file is a normalized reusable abstraction, not a verbatim copy of an upstream skill.